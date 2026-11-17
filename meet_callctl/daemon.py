"""Daemon: owns the uhid device and serves a per-user control socket."""

from __future__ import annotations

import heapq
import json
import math
import logging
import os
import selectors
import signal
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from . import uhid
from .descriptor import PROFILES
from .device import CallControl, CommandError

log = logging.getLogger("meet-callctl")

# pid.codes test VID/PID: https://pid.codes/1209/0001/ ("pid.codes Test PID",
# for private testing only). Not a real product; override with --vid/--pid.
DEFAULT_VID = 0x1209
DEFAULT_PID = 0x0001
DEFAULT_NAME = "meet-callctl Virtual Call Control"
PHYS_PREFIX = "meet-callctl"
MAX_REQUEST = 4096
COMMANDS = ("status", "toggle", "mute", "unmute")
DEFAULT_WAIT = 1.5
MAX_WAIT = 10.0


def default_socket_path() -> str:
    env = os.environ.get("MEET_CALLCTL_SOCKET")
    if env:
        return env
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "meet-callctl", "control.sock")


@dataclass(order=True)
class _Timer:
    when: float
    seq: int
    action: object = field(compare=False)


@dataclass
class _Pending:
    conn: socket.socket
    command: str
    expected: bool | None
    updates_before: int
    deadline: float


class Daemon:
    def __init__(self, *, profile: str, socket_path: str, bus: int,
                 vendor: int, product: int, name: str,
                 uhid_path: str = "/dev/uhid", uhid_factory=None):
        self.profile = PROFILES[profile]
        self.ctl = CallControl(self.profile)
        self.socket_path = socket_path
        self.create_args = dict(
            # Non-empty, stable uniq => HID serial number. Chromium only
            # persists WebHID permissions for devices with a serial number.
            name=name, phys=f"{PHYS_PREFIX}/{profile}",
            uniq=f"{PHYS_PREFIX}-{profile}",
            descriptor=self.profile.descriptor, bus=bus, vendor=vendor,
            product=product, version=0x0100)
        self.uhid_path = uhid_path
        self.uhid_factory = uhid_factory or uhid.UHIDDevice
        self._wake_r, self._wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.sel = selectors.DefaultSelector()
        self.timers: list[_Timer] = []
        self._seq = 0
        self.pending: list[_Pending] = []
        self.running = True
        self.dev: uhid.UHIDDevice | None = None
        self.listener: socket.socket | None = None

    # ------------------------------------------------------------------ setup

    def _bind_socket(self) -> socket.socket:
        d = os.path.dirname(self.socket_path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.stat(d)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise SystemExit(f"{d} must be owned by you with mode 0700")
        if os.path.exists(self.socket_path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(self.socket_path)
            except OSError:
                os.unlink(self.socket_path)  # stale socket from a crash
            else:
                raise SystemExit(f"another daemon is running ({self.socket_path})")
            finally:
                probe.close()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        old = os.umask(0o177)
        try:
            s.bind(self.socket_path)
        finally:
            os.umask(old)
        s.listen(8)
        s.setblocking(False)
        return s

    def start(self) -> None:
        self.listener = self._bind_socket()
        try:
            self.dev = self.uhid_factory(self.uhid_path)
        except PermissionError:
            self._cleanup_socket()
            raise SystemExit(
                f"permission denied opening {self.uhid_path}; install the udev "
                "rule (see README.md, 'Install') and make sure your session is "
                "the active local one")
        self.dev.create(**self.create_args)
        self.sel.register(self.dev.fd, selectors.EVENT_READ, "uhid")
        self.sel.register(self.listener, selectors.EVENT_READ, "listen")
        self.sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        log.info("created HID device %r (profile=%s, %04x:%04x, bus=%#x); "
                 "control socket %s", self.create_args["name"], self.profile.name,
                 self.create_args["vendor"], self.create_args["product"],
                 self.create_args["bus"], self.socket_path)

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, self.stop)
        try:
            while self.running:
                timeout = self._next_timeout()
                for key, _ in self.sel.select(timeout):
                    if key.data == "uhid":
                        self._drain_uhid()
                    elif key.data == "listen":
                        self._accept()
                    elif key.data == "wake":
                        self._drain_wake()
                    else:
                        self._client_readable(key.fileobj)
                self._run_timers()
                self._expire_pending()
        finally:
            self.close()

    def stop(self, *_):
        """Ask the loop to exit (safe from signal handlers and other threads)."""
        self.running = False
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def _drain_wake(self) -> None:
        try:
            while os.read(self._wake_r, 64):
                pass
        except BlockingIOError:
            pass

    def close(self) -> None:
        for p in self.pending:
            self._reply(p.conn, {"ok": False, "error": "daemon stopping"})
        self.pending.clear()
        if self.dev:
            self.dev.close()
            self.dev = None
            log.info("HID device destroyed")
        self._cleanup_socket()
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake_r = self._wake_w = -1

    def _cleanup_socket(self) -> None:
        if self.listener:
            self.listener.close()
            self.listener = None
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass

    def _next_timeout(self) -> float | None:
        deadlines = [t.when for t in self.timers[:1]] + [p.deadline for p in self.pending]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - time.monotonic())

    def _schedule(self, delay: float, action) -> None:
        self._seq += 1
        heapq.heappush(self.timers, _Timer(time.monotonic() + delay, self._seq, action))

    def _run_timers(self) -> None:
        now = time.monotonic()
        while self.timers and self.timers[0].when <= now:
            heapq.heappop(self.timers).action()

    # ------------------------------------------------------------------ uhid

    def _drain_uhid(self) -> None:
        while self.dev and (ev := self.dev.read_event()) is not None:
            self._handle_uhid(ev)

    def _handle_uhid(self, ev: uhid.Event) -> None:
        st = self.ctl.state
        name = uhid.EVENT_NAMES.get(ev.type, str(ev.type))
        if ev.type == uhid.UHID_START:
            st.started = True
            log.info("kernel started device (flags=%#x)", ev.dev_flags or 0)
        elif ev.type == uhid.UHID_STOP:
            st.started = False
            log.info("kernel stopped device")
        elif ev.type == uhid.UHID_OPEN:
            st.host_open = True
            log.info("host opened the device (e.g. Chrome/Meet connected)")
        elif ev.type == uhid.UHID_CLOSE:
            st.host_open = False
            log.info("host closed the device")
        elif ev.type == uhid.UHID_OUTPUT:
            self._led_report(ev.data, "OUTPUT")
        elif ev.type == uhid.UHID_SET_REPORT:
            err = self.ctl.handle_set_report(ev.rnum, ev.rtype, ev.data)
            self.dev.write(uhid.set_report_reply(ev.req_id, err))
            log.debug("SET_REPORT rnum=%s rtype=%s data=%s -> err=%d",
                      ev.rnum, ev.rtype, ev.data.hex(), err)
            if err == 0:
                self._after_led("SET_REPORT")
        elif ev.type == uhid.UHID_GET_REPORT:
            err, data = self.ctl.handle_get_report(ev.rnum, ev.rtype)
            self.dev.write(uhid.get_report_reply(ev.req_id, err, data))
            log.debug("GET_REPORT rnum=%s rtype=%s -> err=%d %s",
                      ev.rnum, ev.rtype, err, data.hex())
        else:
            log.debug("ignored uhid event %s", name)

    def _led_report(self, data: bytes, via: str) -> None:
        if self.ctl.handle_output(data):
            self._after_led(via)
        else:
            # debug only: a web page with device access could flood the journal
            log.debug("ignored output report %s", data[:16].hex())

    def _after_led(self, via: str) -> None:
        st = self.ctl.state
        log.info("host LED report (%s): muted=%s in_call=%s ring=%s",
                 via, st.muted, st.off_hook, st.ring)
        still = []
        for p in self.pending:
            if st.led_updates > p.updates_before and (
                    p.expected is None or st.muted == p.expected):
                self._reply(p.conn, self._result(True, p.command, confirmed=True))
            else:
                still.append(p)
        self.pending = still

    def _send_input(self, report: bytes) -> None:
        if self.dev:
            self.dev.send_input(report)
            log.debug("input report %s", report.hex())

    # ------------------------------------------------------------------ clients

    def _accept(self) -> None:
        try:
            conn, _ = self.listener.accept()
        except BlockingIOError:
            return
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", creds)
        if uid != os.getuid():
            log.warning("rejected control connection from uid %d", uid)
            conn.close()
            return
        conn.setblocking(False)
        self.sel.register(conn, selectors.EVENT_READ, {"buf": b""})

    def _client_readable(self, conn: socket.socket) -> None:
        key = self.sel.get_key(conn)
        try:
            chunk = conn.recv(MAX_REQUEST)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._drop(conn)
            return
        key.data["buf"] += chunk
        if b"\n" not in key.data["buf"]:
            if len(key.data["buf"]) > MAX_REQUEST:
                self._drop(conn)
            return
        line = key.data["buf"].split(b"\n", 1)[0]
        self.sel.unregister(conn)
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
        except ValueError as e:
            self._reply(conn, {"ok": False, "error": f"bad request: {e}"})
            return
        try:
            self._handle_request(conn, req)
        except Exception:  # never let one client request stop the daemon
            log.exception("error handling request")
            self._reply(conn, {"ok": False, "error": "internal error"})

    def _drop(self, conn: socket.socket) -> None:
        try:
            self.sel.unregister(conn)
        except (KeyError, ValueError):
            pass
        conn.close()

    def _handle_request(self, conn: socket.socket, req: dict) -> None:
        cmd = req.get("cmd")
        wait = req.get("wait", DEFAULT_WAIT)
        if cmd not in COMMANDS:
            self._reply(conn, {"ok": False, "error": "bad request: unknown cmd"})
            return
        if (isinstance(wait, bool) or not isinstance(wait, (int, float))
                or not math.isfinite(wait)):
            self._reply(conn, {"ok": False, "error": "bad request: wait must be a number"})
            return
        wait = min(max(float(wait), 0.0), MAX_WAIT)
        if cmd == "status":
            self._reply(conn, self._result(True, cmd))
            return
        st = self.ctl.state
        if not st.host_open:
            self._reply(conn, self._result(
                False, cmd, error="No browser has the device open. In Meet, "
                "connect it under Settings > Audio > Call control (and keep "
                "the Meet tab open)."))
            return
        before = st.muted
        try:
            steps = self.ctl.plan(cmd)
        except CommandError as e:
            self._reply(conn, self._result(False, cmd, error=str(e)))
            return
        if not steps:
            self._reply(conn, self._result(True, cmd, confirmed=True,
                                           note="already in that state"))
            return
        log.info("command %s: sending %d input report(s)", cmd, len(steps))
        for delay, report in steps:
            if delay <= 0:
                self._send_input(report)
            else:
                self._schedule(delay, lambda r=report: self._send_input(r))
        if wait <= 0:
            self._reply(conn, self._result(True, cmd, confirmed=None))
            return
        self.pending.append(_Pending(
            conn, cmd, self.ctl.expected_after(cmd, before),
            st.led_updates, time.monotonic() + wait))

    def _expire_pending(self) -> None:
        now = time.monotonic()
        still = []
        for p in self.pending:
            if p.deadline <= now:
                self._reply(p.conn, self._result(
                    True, p.command, confirmed=False,
                    note="report sent, but Meet did not confirm a mute change"))
            else:
                still.append(p)
        self.pending = still

    def _result(self, ok: bool, cmd, **extra) -> dict:
        return {"ok": ok, "cmd": cmd, "profile": self.profile.name,
                "state": self.ctl.state.as_dict(), **extra}

    def _reply(self, conn: socket.socket, obj: dict) -> None:
        try:
            conn.setblocking(True)
            conn.settimeout(1.0)
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass
        finally:
            conn.close()
