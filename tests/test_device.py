import pytest

from meet_callctl import uhid
from meet_callctl.descriptor import PROFILES
from meet_callctl.device import PULSE_SECONDS, CallControl, CommandError

# LED output reports as Meet would write them (report ID 2).
PHONE_LED = {"in_call_muted": b"\x02\x03", "in_call_unmuted": b"\x02\x01", "idle": b"\x02\x00"}
HEADSET_LED = {"in_call_muted": b"\x02\x03", "in_call_unmuted": b"\x02\x02", "idle": b"\x02\x00"}


def phone():
    return CallControl(PROFILES["phone"])


def headset():
    return CallControl(PROFILES["headset"])


def test_phone_toggle_needs_host_state():
    c = phone()
    with pytest.raises(CommandError):
        c.plan("toggle")


def test_phone_toggle_sends_desired_state_from_led():
    c = phone()
    assert c.handle_output(PHONE_LED["in_call_unmuted"])
    assert (c.state.muted, c.state.off_hook) == (False, True)
    assert c.plan("toggle") == [(0.0, b"\x01\x01")]   # ask to mute
    c.handle_output(PHONE_LED["in_call_muted"])
    assert c.plan("toggle") == [(0.0, b"\x01\x00")]   # ask to unmute


def test_phone_follows_mute_changed_in_meet_ui():
    c = phone()
    c.handle_output(PHONE_LED["in_call_unmuted"])
    c.plan("toggle")                                  # we asked to mute
    c.handle_output(PHONE_LED["in_call_unmuted"])     # ...user unmuted in the UI
    assert c.plan("toggle") == [(0.0, b"\x01\x01")]   # so toggle mutes again


def test_phone_explicit_mute_unmute_without_state():
    c = phone()
    assert c.plan("mute") == [(0.0, b"\x01\x01")]
    assert c.plan("unmute") == [(0.0, b"\x01\x00")]


def test_headset_toggle_is_pulse_with_hook_mirrored():
    c = headset()
    # Before Meet reports anything: on-hook, pulse still allowed.
    assert c.plan("toggle") == [(0.0, b"\x01\x02"), (PULSE_SECONDS, b"\x01\x00")]
    c.handle_output(HEADSET_LED["in_call_unmuted"])
    assert c.plan("toggle") == [(0.0, b"\x01\x03"), (PULSE_SECONDS, b"\x01\x01")]


def test_headset_mute_unmute_need_state_and_skip_noop():
    c = headset()
    with pytest.raises(CommandError):
        c.plan("mute")
    c.handle_output(HEADSET_LED["in_call_muted"])
    assert c.plan("mute") == []
    assert len(c.plan("unmute")) == 2


def test_other_output_reports_ignored():
    c = phone()
    assert not c.handle_output(b"\x05\x01")
    assert not c.handle_output(b"")
    assert c.state.led_updates == 0


def test_set_report_output_with_and_without_report_id():
    c = phone()
    assert c.handle_set_report(2, uhid.UHID_OUTPUT_REPORT, b"\x02\x03") == 0
    assert c.state.muted is True
    assert c.handle_set_report(2, uhid.UHID_OUTPUT_REPORT, b"\x01") == 0
    assert c.state.muted is False
    assert c.handle_set_report(2, uhid.UHID_FEATURE_REPORT, b"\x02\x00") == uhid.EIO


def test_get_report():
    c = phone()
    c.handle_output(PHONE_LED["in_call_muted"])
    assert c.handle_get_report(1, uhid.UHID_INPUT_REPORT) == (0, b"\x01\x01")
    assert c.handle_get_report(3, uhid.UHID_FEATURE_REPORT)[0] == uhid.EIO
    h = headset()
    h.handle_output(HEADSET_LED["in_call_muted"])
    # Relative mute idles at 0; hook mirrors the Off-Hook LED.
    assert h.handle_get_report(1, uhid.UHID_INPUT_REPORT) == (0, b"\x01\x01")


def test_unknown_command():
    with pytest.raises(CommandError):
        phone().plan("dance")


def test_expected_after():
    c = phone()
    assert c.expected_after("toggle", True) is False
    assert c.expected_after("toggle", None) is None
    assert c.expected_after("mute", None) is True
