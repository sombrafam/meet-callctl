import struct

from meet_callctl import uhid


def test_event_size_matches_kernel_struct():
    # sizeof(struct uhid_event): u32 type + struct uhid_create2_req (packed)
    assert uhid.EVENT_SIZE == 4 + 128 + 64 + 64 + 2 + 2 + 4 * 4 + 4096 == 4376


def test_create2_layout():
    ev = uhid.create2("name", "phys", "", b"\x05\x0b", bus=uhid.BUS_VIRTUAL,
                      vendor=0x1209, product=0x0001, version=0x0100)
    assert len(ev) == uhid.EVENT_SIZE
    assert struct.unpack_from("<I", ev, 0)[0] == uhid.UHID_CREATE2
    assert ev[4:8] == b"name"
    assert ev[4 + 128:4 + 132] == b"phys"
    rd_size, bus, vendor, product, version, country = struct.unpack_from("<HHIIII", ev, 4 + 256)
    assert (rd_size, bus, vendor, product, version, country) == (2, 6, 0x1209, 1, 0x100, 0)
    assert ev[4 + 256 + 20:4 + 256 + 22] == b"\x05\x0b"


def test_input2_layout():
    ev = uhid.input2(b"\x01\x02")
    assert struct.unpack_from("<IH", ev, 0) == (uhid.UHID_INPUT2, 2)
    assert ev[6:8] == b"\x01\x02"


def _raw(ev_type, payload):
    return (struct.pack("<I", ev_type) + payload).ljust(uhid.EVENT_SIZE, b"\0")


def test_parse_output():
    payload = bytearray(uhid.UHID_DATA_MAX + 3)
    payload[0:2] = b"\x02\x03"
    struct.pack_into("<HB", payload, uhid.UHID_DATA_MAX, 2, uhid.UHID_OUTPUT_REPORT)
    ev = uhid.parse_event(_raw(uhid.UHID_OUTPUT, bytes(payload)))
    assert ev.type == uhid.UHID_OUTPUT
    assert ev.data == b"\x02\x03"
    assert ev.rtype == uhid.UHID_OUTPUT_REPORT


def test_parse_get_and_set_report():
    ev = uhid.parse_event(_raw(uhid.UHID_GET_REPORT, struct.pack("<IBB", 7, 1, 2)))
    assert (ev.type, ev.req_id, ev.rnum, ev.rtype) == (uhid.UHID_GET_REPORT, 7, 1, 2)
    ev = uhid.parse_event(_raw(uhid.UHID_SET_REPORT,
                               struct.pack("<IBBH", 9, 2, 1, 2) + b"\x02\x01"))
    assert (ev.req_id, ev.rnum, ev.rtype, ev.data) == (9, 2, 1, b"\x02\x01")


def test_replies():
    r = uhid.get_report_reply(5, 0, b"\x01\x00")
    assert struct.unpack_from("<IIHH", r, 0) == (uhid.UHID_GET_REPORT_REPLY, 5, 0, 2)
    r = uhid.set_report_reply(6, uhid.EIO)
    assert struct.unpack_from("<IIH", r, 0) == (uhid.UHID_SET_REPORT_REPLY, 6, uhid.EIO)
