"""Parser for the rekordbox analysis files (ANLZ0000.DAT / .EXT / .2EX).

They hold the beat grid, the cues and the several waveform flavours:

    .DAT   PQTZ beat grid, PCOB cues, PWAV overview waveform (400 columns),
           PWV2 tiny preview
    .EXT   PWV3 blue detail waveform, PWV4 colour overview, PWV5 colour detail,
           PCO2 extended cues with names and colours, PSSI song structure
    .2EX   PWV6/PWV7 three-band waveform (CDJ-3000 and newer)

Format documented by the crate-digger project (Deep Symmetry).
Every integer is big-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Detail waveform columns per second of audio at normal speed
DETAIL_COLUMNS_PER_SECOND = 150


@dataclass
class Beat:
    number: int          # position within the bar, 1..4
    tempo: float         # BPM at this point
    time: int            # milliseconds from the start


@dataclass
class Cue:
    hot_cue: int         # 0 = memory cue, 1..8 = hot cue A..H
    type: str            # "cue" or "loop"
    time: int            # ms
    loop_time: int       # ms (loop end)
    comment: str = ""
    color: tuple[int, int, int] | None = None


@dataclass
class Phrase:
    beat: int
    kind: int
    label: str = ""


@dataclass
class Analysis:
    beats: list[Beat] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    phrases: list[Phrase] = field(default_factory=list)
    path: str = ""
    # raw waveforms, exactly as stored in the file
    preview: bytes = b""          # PWAV  400 columns, 1 byte each
    tiny: bytes = b""             # PWV2  100 columns
    detail: bytes = b""           # PWV3  1 byte per column, 150 columns/s
    color_preview: bytes = b""    # PWV4  6 bytes per column
    color_detail: bytes = b""     # PWV5  2 bytes per column
    band3_preview: bytes = b""    # PWV6  3 bytes per column
    band3_detail: bytes = b""     # PWV7  3 bytes per column

    @property
    def duration_ms(self) -> int:
        return self.beats[-1].time if self.beats else 0


def _u4(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def parse(data: bytes, into: Analysis | None = None) -> Analysis:
    """Parse an ANLZ file. .DAT + .EXT + .2EX can accumulate into one Analysis."""
    a = into or Analysis()
    if len(data) < 12 or data[:4] != b"PMAI":
        raise ValueError("not a rekordbox analysis file (missing PMAI)")
    len_header = _u4(data, 4)
    pos = len_header
    while pos + 12 <= len(data):
        fourcc = data[pos:pos + 4]
        tag_header = _u4(data, pos + 4)
        tag_len = _u4(data, pos + 8)
        if tag_len < 12 or pos + tag_len > len(data):
            break
        body = data[pos + 12:pos + tag_len]
        try:
            _section(a, fourcc, body, tag_header)
        except (struct.error, IndexError, ValueError):
            pass                      # one broken section must not sink the rest
        pos += tag_len
    return a


def _section(a: Analysis, fourcc: bytes, body: bytes, tag_header: int) -> None:
    if fourcc == b"PQTZ":
        count = _u4(body, 8)
        for i in range(count):
            off = 12 + i * 8
            if off + 8 > len(body):
                break
            number, tempo, time = struct.unpack_from(">HHI", body, off)
            a.beats.append(Beat(number, tempo / 100.0, time))

    elif fourcc == b"PPTH":
        n = _u4(body, 0)
        if n > 2:
            a.path = body[4:4 + n - 2].decode("utf-16-be", "replace")

    elif fourcc == b"PWAV":
        a.preview = body[8:8 + _u4(body, 0)]
    elif fourcc == b"PWV2":
        a.tiny = body[8:8 + _u4(body, 0)]
    elif fourcc == b"PWV3":
        entry, count = struct.unpack_from(">II", body, 0)
        a.detail = body[12:12 + count * entry]
    elif fourcc == b"PWV4":
        entry, count = struct.unpack_from(">II", body, 0)
        a.color_preview = body[12:12 + count * entry]
    elif fourcc == b"PWV5":
        entry, count = struct.unpack_from(">II", body, 0)
        a.color_detail = body[12:12 + count * entry]
    elif fourcc == b"PWV6":
        entry, count = struct.unpack_from(">II", body, 0)
        a.band3_preview = body[8:8 + count * entry]
    elif fourcc == b"PWV7":
        entry, count = struct.unpack_from(">II", body, 0)
        a.band3_detail = body[12:12 + count * entry]

    elif fourcc == b"PCOB":
        _cues_basic(a, body)
    elif fourcc == b"PCO2":
        _cues_extended(a, body)
    elif fourcc == b"PSSI":
        _phrases(a, body)


def _cues_basic(a: Analysis, body: bytes) -> None:
    if any(c.comment or c.color for c in a.cues):
        return                                    # extended cues already read, better
    count = struct.unpack_from(">H", body, 6)[0]
    pos = 12
    for _ in range(count):
        if pos + 12 > len(body) or body[pos:pos + 4] != b"PCPT":
            break
        entry_len = _u4(body, pos + 8)
        hot = _u4(body, pos + 12)
        cue_type = body[pos + 28]
        time, loop_time = struct.unpack_from(">II", body, pos + 32)
        a.cues.append(Cue(hot, "loop" if cue_type == 2 else "cue", time, loop_time))
        pos += max(entry_len, 12)


def _cues_extended(a: Analysis, body: bytes) -> None:
    count = struct.unpack_from(">H", body, 4)[0]
    pos = 8
    new: list[Cue] = []
    for _ in range(count):
        if pos + 12 > len(body) or body[pos:pos + 4] != b"PCP2":
            break
        entry_len = _u4(body, pos + 8)
        hot = _u4(body, pos + 12)
        cue_type = body[pos + 16]
        time, loop_time = struct.unpack_from(">II", body, pos + 20)
        comment = ""
        color = None
        len_comment = 0
        if entry_len > 43:
            len_comment = _u4(body, pos + 40)
            if len_comment:
                raw = body[pos + 44:pos + 44 + len_comment]
                comment = raw.decode("utf-16-be", "replace").rstrip("\0")
        color_pos = pos + 44 + len_comment
        if entry_len - len_comment > 47 and color_pos + 4 <= len(body):
            color = (body[color_pos + 1], body[color_pos + 2], body[color_pos + 3])
        new.append(Cue(hot, "loop" if cue_type == 2 else "cue", time, loop_time,
                       comment, color))
        pos += max(entry_len, 12)
    if new:
        # extended cues replace the basic ones of the same kind
        keep = [c for c in a.cues if (c.hot_cue > 0) != (new[0].hot_cue > 0)]
        a.cues = keep + new


_PHRASE_LABELS = {
    1: {1: "Intro", 2: "Verse 1", 3: "Verse 1", 4: "Verse 1", 5: "Verse 2",
        6: "Verse 2", 7: "Verse 2", 8: "Bridge", 9: "Chorus", 10: "Outro"},
    2: {1: "Intro", 2: "Verse 1", 3: "Verse 2", 4: "Verse 3", 5: "Verse 4",
        6: "Verse 5", 7: "Verse 6", 8: "Bridge", 9: "Chorus", 10: "Outro"},
    3: {1: "Intro 1", 2: "Intro 2", 3: "Up 1", 4: "Up 2", 5: "Up 3", 6: "Down",
        7: "Chorus 1", 8: "Chorus 2", 9: "Outro 1", 10: "Outro 2"},
}


def _phrases(a: Analysis, body: bytes) -> None:
    mood = _u4(body, 4)
    count = struct.unpack_from(">H", body, 8)[0]
    labels = _PHRASE_LABELS.get(mood, {})
    pos = 16
    for _ in range(count):
        if pos + 4 > len(body):
            break
        beat, kind = struct.unpack_from(">HH", body, pos)
        a.phrases.append(Phrase(beat, kind, labels.get(kind, f"Phrase {kind}")))
        pos += 0x10


# ------------------------------------------------------------------- decoding

def decode_blue(data: bytes) -> tuple[bytes, bytes]:
    """Classic blue waveform (PWAV/PWV3): returns (heights 0-31, whiteness 0-7)."""
    heights = bytes(b & 0x1F for b in data)
    whiteness = bytes((b >> 5) & 0x07 for b in data)
    return heights, whiteness


def decode_color_detail(data: bytes) -> tuple[bytearray, bytearray]:
    """Colour detail waveform (PWV5, 2 big-endian bytes per column).

    Each column packs the height in bits 2-6 plus three 3-bit frequency bands.
    The band layout was determined by correlating these fields against PWV7,
    which does label them: bits 13-15 are the lows, 10-12 the mids and 7-9 the
    highs. They are painted the way rekordbox does: lows to blue, mids to green
    and highs to red, so a kick drum tints the waveform blue and the highs wash
    it towards white.

    Returns (heights 0-31, interleaved RGB 0-255).
    """
    n = len(data) // 2
    heights = bytearray(n)
    rgb = bytearray(n * 3)
    for i in range(n):
        x = (data[i * 2] << 8) | data[i * 2 + 1]
        heights[i] = (x >> 2) & 0x1F
        rgb[i * 3] = ((x >> 7) & 0x07) * 36          # red   <- highs
        rgb[i * 3 + 1] = ((x >> 10) & 0x07) * 36     # green <- mids
        rgb[i * 3 + 2] = ((x >> 13) & 0x07) * 36     # blue  <- lows
    return heights, rgb


def decode_color_preview(data: bytes) -> tuple[bytearray, bytearray]:
    """Colour overview waveform (PWV4, 6 bytes per column).

    Only byte 0 is confirmed: checked against PWV5 it correlates 0.985 with the
    height, on a 0-127 scale. How colour is spread across bytes 2-5 is unclear,
    so this returns grey. In practice the overview is better generated by
    reducing PWV5 with `downsample()`, which is verified; this function is the
    fallback for tracks that ship no PWV5.
    """
    n = len(data) // 6
    heights = bytearray(n)
    rgb = bytearray(n * 3)
    for i in range(n):
        heights[i] = min(31, data[i * 6] * 31 // 127)
        rgb[i * 3] = rgb[i * 3 + 1] = rgb[i * 3 + 2] = 190
    return heights, rgb


def decode_3band(data: bytes) -> tuple[bytearray, bytearray]:
    """Three-band waveform (PWV6/PWV7, 3 bytes per column: low, mid, high).

    Coloured like PWV5 -- lows to blue, mids to green, highs to red -- so both
    waveforms look the same when a track only ships one of them.
    """
    n = len(data) // 3
    heights = bytearray(n)
    rgb = bytearray(n * 3)
    for i in range(n):
        low, mid, high = data[i * 3], data[i * 3 + 1], data[i * 3 + 2]
        peak = max(low, mid, high)
        heights[i] = min(31, peak * 31 // 127)
        scale = 255 / peak if peak else 0
        rgb[i * 3] = min(255, int(high * scale))
        rgb[i * 3 + 1] = min(255, int(mid * scale))
        rgb[i * 3 + 2] = min(255, int(low * scale))
    return heights, rgb


def downsample(heights, rgb, width: int) -> tuple[bytearray, bytearray]:
    """Reduce a detail waveform to the requested number of columns.

    Takes the peak of each window rather than the mean, so the overview keeps
    the real shape of the track, and averages the colour over that same window.
    """
    n = len(heights)
    out_h = bytearray(width)
    out_rgb = bytearray(width * 3)
    if n == 0:
        return out_h, out_rgb
    step = n / width
    for i in range(width):
        lo = int(i * step)
        hi = max(lo + 1, min(n, int((i + 1) * step)))
        peak = 0
        r = g = b = 0
        for j in range(lo, hi):
            if heights[j] > peak:
                peak = heights[j]
            r += rgb[j * 3]
            g += rgb[j * 3 + 1]
            b += rgb[j * 3 + 2]
        count = hi - lo
        out_h[i] = peak
        out_rgb[i * 3] = r // count
        out_rgb[i * 3 + 1] = g // count
        out_rgb[i * 3 + 2] = b // count
    return out_h, out_rgb
