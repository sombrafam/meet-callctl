"""HID report descriptors for the virtual call-control device.

Two profiles are offered. Each copies the report layout of an open-source
device whose author reports it working with Google Meet's "Call control"
feature. ``phone`` has been tested with Meet in Chrome; ``headset`` has not.

``phone`` (default)
    Layout of jfedor2/meet-mute-button (Raspberry Pi Pico firmware,
    ``code/src/our_descriptor.cc``). Telephony "Phone" collection with a single
    *absolute* Phone Mute input bit: the value sent is the desired mute state.
    Output report 2 carries the Off-Hook (bit 0) and Mute (bit 1) LEDs.
    No Hook Switch, so the device can never signal "hang up".

``headset``
    Layout of xav-ie/virtual-headset (``hid_descriptor.rs``) without its
    private report 3. Telephony "Headset" collection with Hook Switch
    (absolute) and a *relative* Phone Mute bit: a 1 pulse means "toggle".
    Output report 2 carries Mute (bit 0), Off-Hook (bit 1) and Ring (bit 2).

The module also contains a small descriptor parser, used to derive bit
positions from the descriptor bytes instead of hard-coding them twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Usage pages / usages (HID Usage Tables 1.5).
UP_TELEPHONY = 0x0B
UP_LED = 0x08
TEL_PHONE = 0x01
TEL_HEADSET = 0x05
TEL_HOOK_SWITCH = 0x20
TEL_PHONE_MUTE = 0x2F
LED_MUTE = 0x09
LED_OFF_HOOK = 0x17
LED_RING = 0x18


def usage(page: int, uid: int) -> int:
    return (page << 16) | uid


PHONE_DESCRIPTOR = bytes([
    0x05, 0x0B,        # Usage Page (Telephony)
    0x09, 0x01,        # Usage (Phone)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x09, 0x2F,        #   Usage (Phone Mute)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x01,        #   Report Size (1)
    0x81, 0x22,        #   Input (Data,Var,Abs,No Preferred State)
    0x95, 0x07,        #   Report Count (7)
    0x81, 0x03,        #   Input (Const) padding
    0x05, 0x08,        #   Usage Page (LEDs)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x17,        #   Usage (Off-Hook)
    0x95, 0x01,        #   Report Count (1)
    0x91, 0x02,        #   Output (Data,Var,Abs)
    0x09, 0x09,        #   Usage (Mute)
    0x91, 0x02,        #   Output (Data,Var,Abs)
    0x95, 0x06,        #   Report Count (6)
    0x91, 0x03,        #   Output (Const) padding
    0xC0,              # End Collection
])

HEADSET_DESCRIPTOR = bytes([
    0x05, 0x0B,        # Usage Page (Telephony)
    0x09, 0x05,        # Usage (Headset)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x09, 0x20,        #   Usage (Hook Switch)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data,Var,Abs)
    0x09, 0x2F,        #   Usage (Phone Mute)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x06,        #   Input (Data,Var,Rel)
    0x75, 0x06,        #   Report Size (6)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x03,        #   Input (Const) padding
    0x85, 0x02,        #   Report ID (2)
    0x05, 0x08,        #   Usage Page (LEDs)
    0x09, 0x09,        #   Usage (Mute)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x91, 0x02,        #   Output (Data,Var,Abs)
    0x09, 0x17,        #   Usage (Off-Hook)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x91, 0x02,        #   Output (Data,Var,Abs)
    0x09, 0x18,        #   Usage (Ring)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x91, 0x02,        #   Output (Data,Var,Abs)
    0x75, 0x05,        #   Report Size (5)
    0x95, 0x01,        #   Report Count (1)
    0x91, 0x03,        #   Output (Const) padding
    0xC0,              # End Collection
])


# --------------------------------------------------------------------------
# Minimal parser (short items only; enough for the descriptors above and for
# checking what the kernel exposes in sysfs).
# --------------------------------------------------------------------------

INPUT, OUTPUT, FEATURE = "input", "output", "feature"
_MAIN_KINDS = {0x80: INPUT, 0x90: OUTPUT, 0xB0: FEATURE}

FLAG_CONSTANT = 0x01
FLAG_VARIABLE = 0x02
FLAG_RELATIVE = 0x04


@dataclass(frozen=True)
class Field:
    kind: str
    report_id: int
    bit_offset: int       # offset inside the report payload (after report ID)
    bit_size: int
    usage: int | None     # (page << 16) | id, None for constant padding
    flags: int
    logical_min: int
    logical_max: int

    @property
    def relative(self) -> bool:
        return bool(self.flags & FLAG_RELATIVE)

    @property
    def constant(self) -> bool:
        return bool(self.flags & FLAG_CONSTANT)


@dataclass
class ParsedDescriptor:
    fields: list[Field] = field(default_factory=list)
    application_usages: list[int] = field(default_factory=list)
    report_bits: dict[tuple[str, int], int] = field(default_factory=dict)

    def find(self, kind: str, usage_value: int) -> Field:
        for f in self.fields:
            if f.kind == kind and f.usage == usage_value:
                return f
        raise KeyError(f"{kind} usage {usage_value:#x} not in descriptor")

    def has(self, kind: str, usage_value: int) -> bool:
        return any(f.kind == kind and f.usage == usage_value for f in self.fields)

    def report_length(self, kind: str, report_id: int) -> int:
        """Payload length in bytes, excluding the report ID byte."""
        return (self.report_bits.get((kind, report_id), 0) + 7) // 8


def _signed(value: int, size: int) -> int:
    bits = size * 8
    if size and value & (1 << (bits - 1)):
        return value - (1 << bits)
    return value


def parse(desc: bytes) -> ParsedDescriptor:
    out = ParsedDescriptor()
    g = {"page": 0, "lmin": 0, "lmax": 0, "rsize": 0, "rcount": 0, "rid": 0}
    usages: list[int] = []
    umin: int | None = None
    i = 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:
            raise ValueError("long items are not supported")
        size = (0, 1, 2, 4)[prefix & 0x03]
        if i + 1 + size > len(desc):
            raise ValueError(f"truncated item at offset {i}")
        raw = int.from_bytes(desc[i + 1:i + 1 + size], "little")
        tag = prefix & 0xFC
        i += 1 + size

        def full_usage(v: int) -> int:
            return v if size == 4 else (g["page"] << 16) | v

        if tag in _MAIN_KINDS:
            kind = _MAIN_KINDS[tag]
            key = (kind, g["rid"])
            offset = out.report_bits.get(key, 0)
            for n in range(g["rcount"]):
                if raw & FLAG_CONSTANT or not usages:
                    u = None
                else:
                    u = usages[n] if n < len(usages) else usages[-1]
                out.fields.append(Field(kind, g["rid"], offset, g["rsize"], u,
                                        raw, g["lmin"], g["lmax"]))
                offset += g["rsize"]
            out.report_bits[key] = offset
            usages, umin = [], None
        elif tag == 0xA0:  # Collection
            if raw == 0x01 and usages:
                out.application_usages.append(usages[0])
            usages, umin = [], None
        elif tag == 0xC0:  # End Collection
            pass
        elif tag == 0x04:
            g["page"] = raw
        elif tag == 0x14:
            g["lmin"] = _signed(raw, size)
        elif tag == 0x24:
            g["lmax"] = _signed(raw, size)
        elif tag == 0x74:
            g["rsize"] = raw
        elif tag == 0x84:
            g["rid"] = raw
        elif tag == 0x94:
            g["rcount"] = raw
        elif tag == 0x08:
            usages.append(full_usage(raw))
        elif tag == 0x18:
            umin = full_usage(raw)
        elif tag == 0x28:
            if umin is None:
                raise ValueError("Usage Maximum without Usage Minimum")
            usages.extend(range(umin, full_usage(raw) + 1))
        # Other globals/locals (units, push/pop, strings...) are not used here.
    return out


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Bit:
    report_id: int
    byte: int   # index in the report payload (report ID excluded)
    mask: int


def _bit(f: Field) -> Bit:
    if f.bit_size != 1:
        raise ValueError(f"expected a 1-bit field, got {f}")
    return Bit(f.report_id, f.bit_offset // 8, 1 << (f.bit_offset % 8))


@dataclass(frozen=True)
class Profile:
    name: str
    descriptor: bytes
    summary: str

    @property
    def parsed(self) -> ParsedDescriptor:
        return parse(self.descriptor)

    @property
    def mute_input(self) -> Bit:
        return _bit(self.parsed.find(INPUT, usage(UP_TELEPHONY, TEL_PHONE_MUTE)))

    @property
    def mute_is_relative(self) -> bool:
        return self.parsed.find(INPUT, usage(UP_TELEPHONY, TEL_PHONE_MUTE)).relative

    @property
    def hook_input(self) -> Bit | None:
        p = self.parsed
        u = usage(UP_TELEPHONY, TEL_HOOK_SWITCH)
        return _bit(p.find(INPUT, u)) if p.has(INPUT, u) else None

    def led(self, led_usage: int) -> Bit | None:
        p = self.parsed
        u = usage(UP_LED, led_usage)
        return _bit(p.find(OUTPUT, u)) if p.has(OUTPUT, u) else None

    @property
    def input_report_id(self) -> int:
        return self.mute_input.report_id

    @property
    def input_report_len(self) -> int:
        return self.parsed.report_length(INPUT, self.input_report_id)


PROFILES: dict[str, Profile] = {
    "phone": Profile(
        "phone", PHONE_DESCRIPTOR,
        "Telephony Phone, absolute Phone Mute (sends desired state); "
        "layout from jfedor2/meet-mute-button"),
    "headset": Profile(
        "headset", HEADSET_DESCRIPTOR,
        "Telephony Headset, Hook Switch + relative Phone Mute (toggle pulse); "
        "layout from xav-ie/virtual-headset"),
}
DEFAULT_PROFILE = "phone"
