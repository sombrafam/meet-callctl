"""Linux /dev/uhid ABI (include/uapi/linux/uhid.h), packed structs."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

# enum uhid_event_type
UHID_DESTROY = 1
UHID_START = 2
UHID_STOP = 3
UHID_OPEN = 4
UHID_CLOSE = 5
UHID_OUTPUT = 6
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14

# enum uhid_report_type
UHID_FEATURE_REPORT = 0
UHID_OUTPUT_REPORT = 1
UHID_INPUT_REPORT = 2

BUS_USB = 0x03
BUS_VIRTUAL = 0x06

UHID_DATA_MAX = 4096
HID_MAX_DESCRIPTOR_SIZE = 4096

# struct uhid_create2_req: name[128] phys[64] uniq[64] rd_size bus vendor
# product version country rd_data[4096]  -> the largest union member.
_CREATE2 = struct.Struct(f"<128s64s64sHHIIII{HID_MAX_DESCRIPTOR_SIZE}s")
EVENT_SIZE = 4 + _CREATE2.size  # sizeof(struct uhid_event) == 4376

EIO = 5


def _event(ev_type: int, payload: bytes) -> bytes:
    body = struct.pack("<I", ev_type) + payload
    return body.ljust(EVENT_SIZE, b"\0")


def create2(name: str, phys: str, uniq: str, descriptor: bytes, *, bus: int,
            vendor: int, product: int, version: int = 0, country: int = 0) -> bytes:
    if len(descriptor) > HID_MAX_DESCRIPTOR_SIZE:
        raise ValueError("descriptor too large")
    return _event(UHID_CREATE2, _CREATE2.pack(
        name.encode()[:127], phys.encode()[:63], uniq.encode()[:63],
        len(descriptor), bus, vendor, product, version, country, descriptor))


def input2(data: bytes) -> bytes:
    if len(data) > UHID_DATA_MAX:
        raise ValueError("report too large")
    return _event(UHID_INPUT2, struct.pack("<H", len(data)) + data)


def destroy() -> bytes:
    return _event(UHID_DESTROY, b"")


def get_report_reply(req_id: int, err: int, data: bytes = b"") -> bytes:
    return _event(UHID_GET_REPORT_REPLY,
                  struct.pack("<IHH", req_id, err, len(data)) + data)


def set_report_reply(req_id: int, err: int) -> bytes:
    return _event(UHID_SET_REPORT_REPLY, struct.pack("<IH", req_id, err))


@dataclass(frozen=True)
class Event:
    type: int
    data: bytes = b""        # OUTPUT / SET_REPORT payload (starts with report ID)
    rtype: int | None = None
    req_id: int | None = None
    rnum: int | None = None
    dev_flags: int | None = None


def parse_event(buf: bytes) -> Event:
    if len(buf) < 4:
        raise ValueError("short uhid event")
    (ev_type,) = struct.unpack_from("<I", buf, 0)
    p = buf[4:]
    if ev_type == UHID_OUTPUT:
        size, rtype = struct.unpack_from("<HB", p, UHID_DATA_MAX)
        return Event(ev_type, bytes(p[:min(size, UHID_DATA_MAX)]), rtype=rtype)
    if ev_type == UHID_GET_REPORT:
        req_id, rnum, rtype = struct.unpack_from("<IBB", p, 0)
        return Event(ev_type, req_id=req_id, rnum=rnum, rtype=rtype)
    if ev_type == UHID_SET_REPORT:
        req_id, rnum, rtype, size = struct.unpack_from("<IBBH", p, 0)
        data = bytes(p[8:8 + min(size, UHID_DATA_MAX)])
        return Event(ev_type, data, rtype=rtype, req_id=req_id, rnum=rnum)
    if ev_type == UHID_START:
        (flags,) = struct.unpack_from("<Q", p, 0) if len(p) >= 8 else (0,)
        return Event(ev_type, dev_flags=flags)
    return Event(ev_type)


EVENT_NAMES = {
    UHID_START: "START", UHID_STOP: "STOP", UHID_OPEN: "OPEN",
    UHID_CLOSE: "CLOSE", UHID_OUTPUT: "OUTPUT", UHID_GET_REPORT: "GET_REPORT",
    UHID_SET_REPORT: "SET_REPORT",
}


class UHIDDevice:
    """Owns an open /dev/uhid file descriptor and one created device."""

    def __init__(self, path: str = "/dev/uhid"):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK)
        self.created = False

    def create(self, **kwargs) -> None:
        self.write(create2(**kwargs))
        self.created = True

    def write(self, event: bytes) -> None:
        n = os.write(self.fd, event)
        if n != len(event):
            raise OSError(f"short write to uhid ({n} of {len(event)})")

    def send_input(self, report: bytes) -> None:
        self.write(input2(report))

    def read_event(self) -> Event | None:
        try:
            buf = os.read(self.fd, EVENT_SIZE)
        except BlockingIOError:
            return None
        return parse_event(buf)

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            if self.created:
                self.write(destroy())
        finally:
            os.close(self.fd)
            self.fd = -1
