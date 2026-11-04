"""Call-control state machine, independent of the kernel interface.

Meet (the host) is the source of truth for mute state: it writes the LED
output report (Mute / Off-Hook) to the device. This class records that state
and turns user commands into input reports. It never keeps its own guess of
the mute state.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import uhid
from .descriptor import LED_MUTE, LED_OFF_HOOK, LED_RING, Bit, Profile

PULSE_SECONDS = 0.05  # press/release gap for the relative (toggle) profile


class CommandError(Exception):
    """A command cannot be carried out in the current state."""


@dataclass
class HostState:
    muted: bool | None = None      # Mute LED, None until Meet writes it
    off_hook: bool | None = None   # Off-Hook LED: Meet has the call "active"
    ring: bool | None = None
    host_open: bool = False        # a host (e.g. Chrome) has the hidraw node open
    started: bool = False
    led_updates: int = 0           # count of LED reports received

    def as_dict(self) -> dict:
        return {
            "muted": self.muted, "in_call": self.off_hook, "ring": self.ring,
            "host_open": self.host_open, "started": self.started,
            "led_updates": self.led_updates,
        }


def _set(buf: bytearray, bit: Bit, on: bool) -> None:
    if on:
        buf[bit.byte] |= bit.mask
    else:
        buf[bit.byte] &= ~bit.mask & 0xFF


def _get(payload: bytes, bit: Bit) -> bool:
    return bit.byte < len(payload) and bool(payload[bit.byte] & bit.mask)


class CallControl:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.state = HostState()
        self._mute_bit = profile.mute_input
        self._relative = profile.mute_is_relative
        self._hook_bit = profile.hook_input
        self._leds = {k: profile.led(k) for k in (LED_MUTE, LED_OFF_HOOK, LED_RING)}
        self._led_report_id = next(b.report_id for b in self._leds.values() if b)
        self._input_len = profile.input_report_len
        # Last value of the absolute mute field (what the device "reports").
        self._abs_mute_value = False

    # ---------------------------------------------------------------- host -> device

    def handle_output(self, data: bytes) -> bool:
        """Handle an output report (data[0] is the report ID).

        Returns True if it was an LED report.
        """
        if not data or data[0] != self._led_report_id:
            return False
        payload = data[1:]
        st = self.state
        if self._leds[LED_MUTE]:
            st.muted = _get(payload, self._leds[LED_MUTE])
        if self._leds[LED_OFF_HOOK]:
            st.off_hook = _get(payload, self._leds[LED_OFF_HOOK])
        if self._leds[LED_RING]:
            st.ring = _get(payload, self._leds[LED_RING])
        st.led_updates += 1
        if st.muted is not None:
            self._abs_mute_value = st.muted
        return True

    def handle_set_report(self, rnum: int, rtype: int, data: bytes) -> int:
        """SET_REPORT from the kernel; returns an errno (0 = success)."""
        if rtype != uhid.UHID_OUTPUT_REPORT:
            return uhid.EIO
        if not data or data[0] != rnum:
            data = bytes([rnum]) + data
        return 0 if self.handle_output(data) else uhid.EIO

    def handle_get_report(self, rnum: int, rtype: int) -> tuple[int, bytes]:
        if rtype == uhid.UHID_INPUT_REPORT and rnum == self.profile.input_report_id:
            return 0, self._input_report(mute=self._abs_mute_value and not self._relative)
        return uhid.EIO, b""

    # ---------------------------------------------------------------- device -> host

    def _input_report(self, *, mute: bool) -> bytes:
        payload = bytearray(self._input_len)
        _set(payload, self._mute_bit, mute)
        if self._hook_bit:
            # Mirror the host's Off-Hook LED, as a real headset does. Never
            # report on-hook while the host says a call is active.
            _set(payload, self._hook_bit, bool(self.state.off_hook))
        return bytes([self.profile.input_report_id]) + bytes(payload)

    def plan(self, command: str) -> list[tuple[float, bytes]]:
        """Return [(delay_seconds, input_report), ...] for a command."""
        st = self.state
        if command not in ("toggle", "mute", "unmute"):
            raise CommandError(f"unknown command {command!r}")

        if command == "toggle":
            if self._relative:
                return self._pulse()
            if st.muted is None:
                raise CommandError(
                    "Meet has not reported its mute state yet. Make sure the "
                    "device is connected in Meet (Settings > Audio > Call "
                    "control) and that you are in a meeting.")
            want = not st.muted
        else:
            want = command == "mute"

        if self._relative:
            if st.muted is None:
                raise CommandError(
                    "Meet has not reported its mute state yet, so "
                    f"'{command}' cannot be done with a toggle-only device.")
            if st.muted == want:
                return []
            return self._pulse()

        self._abs_mute_value = want
        return [(0.0, self._input_report(mute=want))]

    def _pulse(self) -> list[tuple[float, bytes]]:
        return [(0.0, self._input_report(mute=True)),
                (PULSE_SECONDS, self._input_report(mute=False))]

    def expected_after(self, command: str, before: bool | None) -> bool | None:
        if command == "mute":
            return True
        if command == "unmute":
            return False
        return None if before is None else not before
