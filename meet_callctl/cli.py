"""Command-line entry point: ``meet-callctl daemon|toggle|mute|unmute|status``."""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import shutil
import socket
import subprocess
import sys

from . import __version__, uhid
from .daemon import (DEFAULT_NAME, DEFAULT_PID, DEFAULT_VID, PHYS_PREFIX,
                     Daemon, default_socket_path)
from .descriptor import DEFAULT_PROFILE, PROFILES

EXIT_OK, EXIT_ERROR, EXIT_NO_DAEMON, EXIT_UNCONFIRMED = 0, 1, 2, 3


def _int(value: str) -> int:
    return int(value, 0)


def request(cmd: str, *, wait: float, path: str) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(wait + 3.0)
    try:
        s.connect(path)
        s.sendall((json.dumps({"cmd": cmd, "wait": wait}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf)


def describe(resp: dict) -> str:
    st = resp.get("state", {})
    muted = st.get("muted")
    mic = {True: "muted", False: "unmuted", None: "unknown"}[muted]
    call = {True: "in call", False: "not in call", None: "call state unknown"}[st.get("in_call")]
    host = "browser connected" if st.get("host_open") else "no browser connected"
    return f"Meet mic {mic} ({call}, {host})"


def notify(summary: str, body: str = "") -> None:
    exe = shutil.which("notify-send")
    if exe:
        subprocess.run([exe, "-a", "meet-callctl", "-t", "2000", summary, body],
                       check=False)


def cmd_client(args) -> int:
    wait = 0.0 if getattr(args, "no_wait", False) else args.timeout
    try:
        resp = request(args.command, wait=wait, path=args.socket)
    except (FileNotFoundError, ConnectionRefusedError):
        msg = "meet-callctl daemon is not running"
        print(msg, file=sys.stderr)
        if args.notify:
            notify("Meet mute: failed", msg)
        return EXIT_NO_DAEMON
    except (OSError, ValueError) as e:
        print(f"error talking to daemon: {e}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(resp))
    if not resp.get("ok"):
        err = resp.get("error", "unknown error")
        if not args.json:
            print(err, file=sys.stderr)
        if args.notify:
            notify("Meet mute: failed", err)
        return EXIT_ERROR

    text = describe(resp)
    confirmed = resp.get("confirmed")
    if args.command != "status" and confirmed is False:
        text = f"{resp.get('note', 'not confirmed')}; {text}"
    if not args.json:
        print(text)
    if args.notify and args.command != "status":
        notify("Meet: " + ({True: "muted", False: "unmuted"}.get(
            resp["state"].get("muted"), "?") if confirmed else "no confirmation"),
               text)
    if args.command != "status" and confirmed is False:
        return EXIT_UNCONFIRMED
    return EXIT_OK


def find_devices() -> list[tuple[str, str, str]]:
    found = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                env = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
        except OSError:
            continue
        if env.get("HID_PHYS", "").startswith(PHYS_PREFIX + "/"):
            found.append(("/dev/" + os.path.basename(node), env.get("HID_NAME", ""),
                          env.get("HID_PHYS", "")))
    return found


def cmd_find(args) -> int:
    devs = find_devices()
    for path, name, phys in devs:
        print(f"{path}\t{name}\t{phys}")
    if not devs:
        print("no meet-callctl device found (is the daemon running?)", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_profiles(args) -> int:
    for name, p in PROFILES.items():
        mark = " (default)" if name == DEFAULT_PROFILE else ""
        print(f"{name}{mark}: {p.summary}\n  descriptor: {p.descriptor.hex(' ')}")
    return EXIT_OK


def cmd_daemon(args) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    bus = {"virtual": uhid.BUS_VIRTUAL, "usb": uhid.BUS_USB}[args.bus]
    d = Daemon(profile=args.profile, socket_path=args.socket, bus=bus,
               vendor=args.vid, product=args.pid, name=args.name,
               uhid_path=args.uhid)
    d.start()
    d.run()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="meet-callctl",
        description="Virtual HID call-control device for Google Meet.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--socket", default=default_socket_path(),
                    help="control socket path (default: %(default)s)")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daemon", help="create the virtual device and serve commands")
    d.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE)
    d.add_argument("--bus", choices=("virtual", "usb"), default="virtual",
                   help="HID bus type to report (default: %(default)s)")
    d.add_argument("--vid", type=_int, default=DEFAULT_VID)
    d.add_argument("--pid", type=_int, default=DEFAULT_PID)
    d.add_argument("--name", default=DEFAULT_NAME)
    d.add_argument("--uhid", default="/dev/uhid")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=cmd_daemon)

    for name, helptext in (("toggle", "toggle Meet's microphone mute"),
                           ("mute", "mute Meet's microphone"),
                           ("unmute", "unmute Meet's microphone"),
                           ("status", "show the state Meet last reported")):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("--timeout", type=float, default=1.5,
                       help="seconds to wait for Meet to confirm (default: %(default)s)")
        if name != "status":
            c.add_argument("--no-wait", action="store_true",
                           help="do not wait for Meet's confirmation")
        c.add_argument("--notify", action="store_true",
                       help="show a desktop notification with the result")
        c.add_argument("--json", action="store_true")
        c.set_defaults(func=cmd_client)

    sub.add_parser("find-device", help="print the hidraw node of the device"
                   ).set_defaults(func=cmd_find)
    sub.add_parser("profiles", help="list descriptor profiles"
                   ).set_defaults(func=cmd_profiles)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
