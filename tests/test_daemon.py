"""Daemon tests against a fake kernel (a SOCK_SEQPACKET socketpair)."""

import json
import os
import socket
import struct
import threading

import pytest

from meet_callctl import uhid
from meet_callctl.cli import request
from meet_callctl.daemon import Daemon


class FakeKernel:
    """Plays the kernel side of /dev/uhid."""

    def __init__(self):
        self.ours, self.theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.ours.settimeout(2.0)

    def factory(self, _path):
        dev = uhid.UHIDDevice.__new__(uhid.UHIDDevice)
        dev.fd = self.theirs.detach()
        os.set_blocking(dev.fd, False)
        dev.created = False
        return dev

    def recv(self):
        buf = self.ours.recv(uhid.EVENT_SIZE)
        return struct.unpack_from("<I", buf)[0], buf[4:]

    def recv_input(self):
        ev_type, p = self.recv()
        assert ev_type == uhid.UHID_INPUT2
        (size,) = struct.unpack_from("<H", p)
        return bytes(p[2:2 + size])

    def send(self, ev_type, payload=b""):
        self.ours.send((struct.pack("<I", ev_type) + payload).ljust(uhid.EVENT_SIZE, b"\0"))

    def send_output(self, data):
        payload = bytearray(uhid.UHID_DATA_MAX + 3)
        payload[:len(data)] = data
        struct.pack_into("<HB", payload, uhid.UHID_DATA_MAX, len(data), uhid.UHID_OUTPUT_REPORT)
        self.send(uhid.UHID_OUTPUT, bytes(payload))


@pytest.fixture
def setup(tmp_path):
    def make(profile="phone"):
        os.chmod(tmp_path, 0o700)
        kernel = FakeKernel()
        sock = str(tmp_path / "ctl" / "control.sock")
        d = Daemon(profile=profile, socket_path=sock, bus=uhid.BUS_VIRTUAL,
                   vendor=0x1209, product=0x0001, name="test", uhid_factory=kernel.factory)
        d.start()
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        ev_type, p = kernel.recv()
        assert ev_type == uhid.UHID_CREATE2
        made.append((d, t))
        return d, kernel, sock
    made = []
    yield make
    for d, t in made:
        d.stop()
        t.join(2)
        assert not t.is_alive()


def status(sock):
    return request("status", wait=0, path=sock)


def wait_for(pred, sock, tries=50):
    import time
    for _ in range(tries):
        st = status(sock)["state"]
        if pred(st):
            return st
        time.sleep(0.02)
    raise AssertionError(f"condition not met: {st}")


def test_socket_permissions(setup):
    d, kernel, sock = setup()
    assert os.stat(sock).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(sock)).st_mode & 0o777 == 0o700


def test_refuses_without_host(setup):
    d, kernel, sock = setup()
    r = request("toggle", wait=0.2, path=sock)
    assert not r["ok"] and "No browser" in r["error"]


def test_phone_toggle_confirmed_by_led(setup):
    d, kernel, sock = setup()
    kernel.send(uhid.UHID_START, struct.pack("<Q", 0))
    kernel.send(uhid.UHID_OPEN)
    kernel.send_output(b"\x02\x01")                 # in call, unmuted
    wait_for(lambda s: s["host_open"] and s["muted"] is False, sock)

    result = {}
    t = threading.Thread(target=lambda: result.update(request("toggle", wait=2, path=sock)))
    t.start()
    assert kernel.recv_input() == b"\x01\x01"       # desired: muted
    kernel.send_output(b"\x02\x03")                 # Meet confirms
    t.join(3)
    assert result["ok"] and result["confirmed"] is True
    assert result["state"]["muted"] is True


def test_toggle_unconfirmed_times_out(setup):
    d, kernel, sock = setup()
    kernel.send(uhid.UHID_OPEN)
    kernel.send_output(b"\x02\x01")
    wait_for(lambda s: s["muted"] is False, sock)
    r = request("toggle", wait=0.2, path=sock)
    assert r["ok"] and r["confirmed"] is False
    assert kernel.recv_input() == b"\x01\x01"


def test_headset_pulse_and_get_report(setup):
    d, kernel, sock = setup("headset")
    kernel.send(uhid.UHID_OPEN)
    kernel.send_output(b"\x02\x02")                 # in call, unmuted
    wait_for(lambda s: s["in_call"] is True, sock)
    r = request("toggle", wait=0, path=sock)
    assert r["ok"]
    assert kernel.recv_input() == b"\x01\x03"       # hook + mute pressed
    assert kernel.recv_input() == b"\x01\x01"       # mute released
    kernel.send(uhid.UHID_GET_REPORT, struct.pack("<IBB", 42, 1, uhid.UHID_INPUT_REPORT))
    ev_type, p = kernel.recv()
    assert ev_type == uhid.UHID_GET_REPORT_REPLY
    req_id, err, size = struct.unpack_from("<IHH", p)
    assert (req_id, err, bytes(p[8:8 + size])) == (42, 0, b"\x01\x01")


def test_set_report_feature_rejected(setup):
    d, kernel, sock = setup()
    kernel.send(uhid.UHID_SET_REPORT,
                struct.pack("<IBBH", 7, 3, uhid.UHID_FEATURE_REPORT, 1) + b"\x00")
    ev_type, p = kernel.recv()
    assert ev_type == uhid.UHID_SET_REPORT_REPLY
    assert struct.unpack_from("<IH", p) == (7, uhid.EIO)


def test_bad_request(setup):
    d, kernel, sock = setup()
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.sendall(b"not json\n")
    r = json.loads(s.recv(4096))
    assert not r["ok"]


def test_second_daemon_refused(setup):
    d, kernel, sock = setup()
    other = Daemon(profile="phone", socket_path=sock, bus=6, vendor=1, product=1,
                   name="x", uhid_factory=FakeKernel().factory)
    with pytest.raises(SystemExit):
        other.start()


@pytest.mark.parametrize("line", [
    b'{"cmd": "toggle", "wait": "x"}\n',
    b'{"cmd": "toggle", "wait": NaN}\n',
    b'{"cmd": "toggle", "wait": [1]}\n',
    b'{"cmd": ["toggle"]}\n',
    b'[1, 2]\n',
])
def test_malformed_requests_do_not_break_daemon(setup, line):
    d, kernel, sock = setup()
    kernel.send(uhid.UHID_OPEN)
    kernel.send_output(b"\x02\x01")
    wait_for(lambda s: s["muted"] is False, sock)
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    s.connect(sock)
    s.sendall(line)
    r = json.loads(s.recv(4096))
    assert not r["ok"]
    assert status(sock)["ok"]          # daemon still serving
