"""Pro DJ Link packet encoding and decoding.

Three UDP ports carry everything:

    50000  keep-alive / device announcements (broadcast)
    50001  beats and position (broadcast)
    50002  detailed player status (unicast, only to announced devices)

Field offsets are verified against real captures from an XDJ-AZ (firmware 1.30).
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field

MAGIC = b"Qspt1WmJOL"

PORT_ANNOUNCE = 50000
PORT_BEAT = 50001
PORT_STATUS = 50002
PORT_MIXER = 50004

# Packet types (byte 0x0a)
TYPE_KEEPALIVE = 0x06
TYPE_BEAT = 0x28
TYPE_ABSOLUTE_POSITION = 0x0B
TYPE_MIXER_STATUS = 0x03
TYPE_CDJ_STATUS = 0x0A
TYPE_DJM_STATUS = 0x29
TYPE_MIXER_CLOCK = 0x20

# These are stable keys, not user-facing text: they travel through the API as-is
# and each interface translates them (see web/index.html).
SLOTS = {0: "empty", 1: "cd", 2: "sd", 3: "usb", 4: "rekordbox"}
TRACK_TYPES = {0: "unknown", 1: "rekordbox", 2: "file", 5: "cd"}
STORAGE = {0: "loaded", 2: "stopping", 3: "unmounting", 4: "no_media"}

PLAY_STATES = {
    0x00: "no_track", 0x02: "loading", 0x03: "playing", 0x04: "looping",
    0x05: "paused", 0x06: "cued", 0x07: "cue_play", 0x08: "cue_scratch",
    0x09: "searching", 0x0E: "unplayable", 0x11: "end_of_track",
    0x12: "emergency",
}
PLAYING_STATES = {0x03, 0x04, 0x07}


def _name(data: bytes, off: int, length: int = 20) -> str:
    return data[off:off + length].split(b"\0")[0].decode("ascii", "replace")


def _pitch(raw: int) -> float:
    """0x100000 means normal speed; returns the playback rate multiplier."""
    return raw / 0x100000


@dataclass
class Announce:
    """Keep-alive: a device announcing its presence."""
    name: str
    device_number: int
    device_type: int
    mac: str
    ip: str

    @property
    def kind(self) -> str:
        """The type byte reads 3 on every device seen, so classify by number.

        An all-in-one such as the XDJ-AZ announces itself five times: once per
        deck (1-4) plus the mixer section (33).
        """
        n = self.device_number
        if 1 <= n <= 6:
            return "player"
        if n == 17:
            return "rekordbox"
        if 33 <= n <= 47:
            return "mixer"
        return "device"


@dataclass
class Beat:
    """Beat packet, sent exactly on the beat."""
    device_number: int
    name: str
    next_beat: int         # ms until the next beat
    next_bar: int          # ms until the next bar
    pitch: float           # rate multiplier (1.0 = 0 %)
    bpm: float             # track tempo at this point
    beat_in_bar: int       # 1..4

    @property
    def effective_bpm(self) -> float:
        return self.bpm * self.pitch


@dataclass
class Status:
    """Full player status (port 50002)."""
    device_number: int = 0
    name: str = ""
    firmware: str = ""

    track_id: int = 0
    track_number: int = 0
    slot: str = ""
    track_type: str = ""
    loaded_from: int = 0          # device number the track was loaded from

    play_state: str = ""
    play_state_raw: int = 0
    bpm: float = 0.0              # track tempo (0 when no track)
    pitch: float = 1.0            # actual playback rate
    physical_pitch: float = 1.0   # tempo fader position
    beat_count: int = 0           # absolute beat within the track
    beat_in_bar: int = 0          # 1..4
    cue_distance: int = 0         # beats to the next cue (0x1ff = none)

    on_air: bool = False
    sync: bool = False
    master: bool = False
    playing: bool = False

    usb_state: str = ""
    sd_state: str = ""
    raw: bytes = field(default=b"", repr=False)

    @property
    def effective_bpm(self) -> float:
        """Tempo the deck is set to play at: track tempo times the fader."""
        return self.bpm * self.physical_pitch

    @property
    def pitch_percent(self) -> float:
        """Tempo fader position. This is the number a DJ expects to read."""
        return (self.physical_pitch - 1.0) * 100.0

    @property
    def speed(self) -> float:
        """Real advance rate: 0 when stopped. Used to move the playhead."""
        return self.pitch

    @property
    def has_track(self) -> bool:
        return self.track_id != 0


def parse(data: bytes) -> Announce | Beat | Status | None:
    """Decode any Pro DJ Link packet; returns None if it is not one."""
    if len(data) < 0x24 or not data.startswith(MAGIC):
        return None
    packet_type = data[10]
    if packet_type == TYPE_KEEPALIVE:
        return _parse_announce(data)
    if packet_type == TYPE_BEAT:
        return _parse_beat(data)
    if packet_type == TYPE_CDJ_STATUS:
        return _parse_status(data)
    return None


def _parse_announce(data: bytes) -> Announce | None:
    if len(data) < 0x36:
        return None
    return Announce(
        name=_name(data, 0x0C),
        device_number=data[0x24],
        device_type=data[0x21],
        mac=":".join(f"{b:02x}" for b in data[0x26:0x2C]),
        ip=".".join(str(b) for b in data[0x2C:0x30]),
    )


def _parse_beat(data: bytes) -> Beat | None:
    if len(data) < 0x60:
        return None
    next_beat, _second, next_bar = struct.unpack_from(">III", data, 0x24)
    pitch = struct.unpack_from(">I", data, 0x54)[0]
    bpm = struct.unpack_from(">H", data, 0x5A)[0]
    return Beat(
        device_number=data[0x21],
        name=_name(data, 0x0B),
        next_beat=next_beat,
        next_bar=next_bar,
        pitch=_pitch(pitch),
        bpm=bpm / 100.0 if bpm != 0xFFFF else 0.0,
        beat_in_bar=data[0x5C],
    )


def _parse_status(data: bytes) -> Status | None:
    if len(data) < 0xA7:
        return None
    s = Status(raw=data)
    s.device_number = data[0x21]
    s.name = _name(data, 0x0B)
    s.firmware = _name(data, 0x7C, 4)

    s.loaded_from = data[0x28]
    s.slot = SLOTS.get(data[0x29], "unknown")
    s.track_type = TRACK_TYPES.get(data[0x2A], "unknown")
    s.track_id = struct.unpack_from(">I", data, 0x2C)[0]
    s.track_number = struct.unpack_from(">I", data, 0x30)[0]

    s.usb_state = STORAGE.get(struct.unpack_from(">I", data, 0x6C)[0], "unknown")
    s.sd_state = STORAGE.get(struct.unpack_from(">I", data, 0x70)[0], "unknown")

    s.play_state_raw = struct.unpack_from(">I", data, 0x78)[0]
    s.play_state = PLAY_STATES.get(s.play_state_raw, "unknown")

    flags = struct.unpack_from(">H", data, 0x88)[0]
    s.on_air = bool(flags & 0x08)
    s.sync = bool(flags & 0x10)
    s.master = bool(flags & 0x20)
    s.playing = bool(flags & 0x40)

    s.physical_pitch = _pitch(struct.unpack_from(">I", data, 0x8C)[0])
    bpm = struct.unpack_from(">H", data, 0x92)[0]
    s.bpm = bpm / 100.0 if bpm != 0xFFFF else 0.0
    s.pitch = _pitch(struct.unpack_from(">I", data, 0x98)[0])

    beat_count = struct.unpack_from(">I", data, 0xA0)[0]
    s.beat_count = 0 if beat_count == 0xFFFFFFFF else beat_count   # -1 = no track
    s.cue_distance = struct.unpack_from(">H", data, 0xA4)[0]
    s.beat_in_bar = data[0xA6]
    return s


def build_keepalive(name: str, device_number: int, mac: bytes, ip: str,
                    device_count: int = 1) -> bytes:
    """Build the keep-alive that announces us as a virtual device.

    Sending it is mandatory: players send their detailed status by unicast, and
    only to addresses that have announced themselves on the network.

    The template is copied byte for byte from the keep-alive rekordbox emits,
    captured on the same network: a client the player accepts and does send
    status to. Only the name, device number, MAC and IP differ.
    """
    pkt = bytearray(0x36)
    pkt[0:10] = MAGIC
    pkt[10] = TYPE_KEEPALIVE
    pkt[11] = 0x00
    encoded = name.encode("ascii", "replace")[:19]
    pkt[0x0C:0x0C + len(encoded)] = encoded
    pkt[0x20] = 0x01
    pkt[0x21] = 0x03          # every device seen so far uses 3 here
    struct.pack_into(">H", pkt, 0x22, 0x36)
    pkt[0x24] = device_number
    pkt[0x25] = 0x01
    pkt[0x26:0x2C] = mac
    pkt[0x2C:0x30] = socket.inet_aton(ip)
    pkt[0x30] = device_count & 0xFF   # devices we know about
    pkt[0x31] = 0x01
    pkt[0x32] = 0x00
    pkt[0x33] = 0x00
    pkt[0x34] = 0x04          # capability flags, same as rekordbox
    pkt[0x35] = 0x08
    return bytes(pkt)
