import pytest

from meet_callctl.descriptor import (HEADSET_DESCRIPTOR, INPUT, LED_MUTE,
                                     LED_OFF_HOOK, LED_RING, OUTPUT,
                                     PHONE_DESCRIPTOR, PROFILES, TEL_HEADSET,
                                     TEL_PHONE, TEL_PHONE_MUTE, UP_TELEPHONY,
                                     Bit, parse, usage)


def test_phone_layout():
    p = PROFILES["phone"]
    parsed = parse(PHONE_DESCRIPTOR)
    assert parsed.application_usages == [usage(UP_TELEPHONY, TEL_PHONE)]
    assert p.mute_input == Bit(1, 0, 0x01)
    assert not p.mute_is_relative
    assert p.hook_input is None          # cannot signal hang-up
    assert p.led(LED_OFF_HOOK) == Bit(2, 0, 0x01)
    assert p.led(LED_MUTE) == Bit(2, 0, 0x02)
    assert p.led(LED_RING) is None
    assert parsed.report_length(INPUT, 1) == 1
    assert parsed.report_length(OUTPUT, 2) == 1


def test_headset_layout():
    p = PROFILES["headset"]
    parsed = parse(HEADSET_DESCRIPTOR)
    assert parsed.application_usages == [usage(UP_TELEPHONY, TEL_HEADSET)]
    assert p.hook_input == Bit(1, 0, 0x01)
    assert p.mute_input == Bit(1, 0, 0x02)
    assert p.mute_is_relative
    assert p.led(LED_MUTE) == Bit(2, 0, 0x01)
    assert p.led(LED_OFF_HOOK) == Bit(2, 0, 0x02)
    assert p.led(LED_RING) == Bit(2, 0, 0x04)


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_reports_are_whole_bytes_and_single_collection(name):
    parsed = parse(PROFILES[name].descriptor)
    assert all(bits % 8 == 0 for bits in parsed.report_bits.values())
    assert len(parsed.application_usages) == 1
    # Everything sits on the Telephony page (Meet's call-control devices);
    # no keyboard/consumer collection that the kernel would turn into keys.
    assert parsed.application_usages[0] >> 16 == UP_TELEPHONY
    mute = parsed.find(INPUT, usage(UP_TELEPHONY, TEL_PHONE_MUTE))
    assert (mute.logical_min, mute.logical_max) == (0, 1)


def test_parser_rejects_truncated():
    with pytest.raises(ValueError):
        parse(bytes([0x05]))
