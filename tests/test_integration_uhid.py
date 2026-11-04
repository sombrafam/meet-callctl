"""End-to-end through the real kernel: daemon -> /dev/uhid -> /dev/hidrawN.

The test plays the browser's role on the hidraw node (what Chrome's WebHID
does on Linux): it opens the node, writes LED output reports as Meet would,
and reads the input reports the daemon sends. It cannot prove that Meet
accepts the device; it proves the kernel-facing half works.

Skipped unless /dev/uhid is usable (see README.md, "Install").
"""

import fcntl
import glob
import os
import select
import subprocess
import sys
import time
import uuid

import pytest

from meet_callctl.descriptor import PROFILES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    not os.access("/dev/uhid", os.R_OK | os.W_OK),
    reason="/dev/uhid not accessible (install the udev rule)")


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


def HIDIOCGINPUT(length):   # _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x0A, len)
    return _ioc(3, 0x0A, length)


def HIDIOCSFEATURE(length):  # _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, len)
    return _ioc(3, 0x06, length)


def find_hidraw(name, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for node in glob.glob("/sys/class/hidraw/hidraw*"):
            try:
                with open(os.path.join(node, "device", "uevent")) as f:
                    if f"HID_NAME={name}\n" in f.read():
                        dev = "/dev/" + os.path.basename(node)
                        if os.path.exists(dev):
                            return node, dev
            except OSError:
                pass
        time.sleep(0.05)
    raise AssertionError(f"hidraw node for {name!r} did not appear")


def ctl(sock, *args):
    r = subprocess.run([sys.executable, "-m", "meet_callctl.cli", "--socket", sock, *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=10)
    return r.returncode, r.stdout + r.stderr


def read_report(fd, timeout=2.0):
    r, _, _ = select.select([fd], [], [], timeout)
    assert r, "no input report from device"
    return os.read(fd, 64)


@pytest.fixture
def daemon(tmp_path):
    procs = []

    def start(profile):
        os.chmod(tmp_path, 0o700)
        name = f"meet-callctl test {uuid.uuid4().hex[:8]}"
        sock = str(tmp_path / profile / "control.sock")
        p = subprocess.Popen(
            [sys.executable, "-m", "meet_callctl.cli", "--socket", sock, "daemon",
             "--profile", profile, "--name", name, "-v"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(p)
        sysnode, dev = find_hidraw(name)
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        if not os.access(dev, os.R_OK | os.W_OK):
            pytest.skip(f"{dev} not accessible; udev rule not installed?")
        return sock, sysnode, dev

    yield start
    for p in procs:
        p.terminate()
        out, _ = p.communicate(timeout=5)
        assert p.returncode == 0, out


def test_kernel_view_matches_profile(daemon):
    sock, sysnode, dev = daemon("phone")
    with open(os.path.join(sysnode, "device", "report_descriptor"), "rb") as f:
        assert f.read() == PROFILES["phone"].descriptor   # what Chrome reads
    with open(os.path.join(sysnode, "device", "uevent")) as f:
        assert "HID_ID=0006:00001209:00000001" in f.read()
    # Telephony-only collection: hid-input must not create an evdev device
    # (otherwise Phone Mute would also emit KEY_MICMUTE to the desktop).
    assert not glob.glob(os.path.join(sysnode, "device", "input", "input*"))


def test_phone_profile_end_to_end(daemon):
    sock, _, dev = daemon("phone")
    code, out = ctl(sock, "toggle")
    assert code == 1 and "No browser" in out          # nobody has it open yet

    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        time.sleep(0.1)
        code, out = ctl(sock, "toggle")
        assert code == 1 and "not reported its mute state" in out

        os.write(fd, b"\x02\x01")                    # Meet: in call, unmuted
        time.sleep(0.1)
        code, out = ctl(sock, "status")
        assert code == 0 and "unmuted" in out and "in call" in out

        p = subprocess.Popen([sys.executable, "-m", "meet_callctl.cli", "--socket", sock,
                              "toggle", "--timeout", "3"], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert read_report(fd) == b"\x01\x01"        # device asks: mute
        os.write(fd, b"\x02\x03")                    # Meet confirms muted
        out, _ = p.communicate(timeout=5)
        assert p.returncode == 0 and "Meet mic muted" in out

        # GET_REPORT must be answered (otherwise the kernel blocks ~5 s).
        buf = bytearray(b"\x01\x00")
        t0 = time.monotonic()
        fcntl.ioctl(fd, HIDIOCGINPUT(len(buf)), buf)
        assert bytes(buf) == b"\x01\x01" and time.monotonic() - t0 < 1

        # Feature reports are not supported: rejected quickly, not hung.
        t0 = time.monotonic()
        with pytest.raises(OSError):
            fcntl.ioctl(fd, HIDIOCSFEATURE(2), bytearray(b"\x03\x00"))
        assert time.monotonic() - t0 < 1
    finally:
        os.close(fd)


def test_headset_profile_end_to_end(daemon):
    sock, _, dev = daemon("headset")
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(fd, b"\x02\x02")                    # Meet: off-hook, unmuted
        time.sleep(0.1)
        code, out = ctl(sock, "toggle", "--no-wait")
        assert code == 0
        assert read_report(fd) == b"\x01\x03"        # hook held, mute pressed
        assert read_report(fd) == b"\x01\x01"        # mute released
        os.write(fd, b"\x02\x03")
        time.sleep(0.1)
        code, out = ctl(sock, "mute")                # already muted: no report
        assert code == 0
        r, _, _ = select.select([fd], [], [], 0.3)
        assert not r
    finally:
        os.close(fd)
