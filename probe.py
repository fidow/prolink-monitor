"""Protocol probe: reports every byte that changes on the network, live.

Use it to find out whether a control on the player travels over Pro DJ Link.
Start it, wait a few seconds while it learns the idle state, and from then on it
prints each byte that changes, naming the field it belongs to.

    python probe.py                 watch everything
    python probe.py --all           do not discard the fields that move on their own

To check whether the channel faders are transmitted: start it, wait for the
"ready" notice, and move the faders. If nothing shows up, that data does not
travel over the network.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import build_source                          # noqa: E402
from prolink import link, proto                       # noqa: E402

# A byte that was already moving during the learning phase is a counter, the
# beat, the pitch... and would drown out any finding. They are discarded by
# observation rather than with a hand-written list: a hand-written list can hide
# the very byte you are looking for.
MAX_STABLE_VALUES = 2

# Known fields: (offset, length, name). A lone byte says nothing; what matters is
# the field it belongs to, so we resolve it and show the whole value.
FIELDS = {
    0x0A: [
        # On the XDJ-AZ this field is not "activity" as on older gear: it reads
        # 0x0100 with the channel open and 0x0000 with it closed, even with the
        # track stopped. It mirrors the on-air bit.
        (0x26, 2, "channel state (0x0100=open, 0x0000=closed)"),
        (0x28, 1, "source player"), (0x29, 1, "slot"), (0x2A, 1, "track type"),
        (0x2C, 4, "track id"), (0x30, 4, "position in list"),
        (0x6A, 1, "USB active"), (0x6B, 1, "SD active"),
        (0x6C, 4, "USB state"), (0x70, 4, "SD state"),
        (0x78, 4, "play state"), (0x7C, 4, "firmware"),
        (0x84, 4, "master change count"),
        (0x88, 2, "FLAGS: 08=on air (channel fader) 10=sync 20=master 40=play"),
        (0x8C, 4, "PHYSICAL PITCH: tempo fader position"),
        (0x90, 2, "BPM source"), (0x92, 2, "track BPM"),
        (0x98, 4, "ACTUAL PITCH: playback speed (sync adjusts it)"),
        (0x9C, 2, "play state 3"), (0x9E, 1, "analysis"),
        (0xA0, 4, "beat number"), (0xA4, 2, "beats to the cue"),
        (0xA6, 1, "beat in bar"),
        (0xC0, 4, "physical pitch (copy)"), (0xC4, 4, "actual pitch (copy)"),
        (0xCC, 1, "device class"),
    ],
    0x28: [
        (0x24, 4, "ms to the next beat"), (0x28, 4, "ms to the 2nd beat"),
        (0x2C, 4, "ms to the next bar"), (0x30, 4, "ms to the 4th beat"),
        (0x34, 4, "ms to the 2nd bar"), (0x38, 4, "ms to the 8th beat"),
        (0x54, 4, "PITCH"), (0x5A, 2, "BPM"), (0x5C, 1, "beat in bar"),
    ],
    0x20: [(0x24, 4, "mixer counter")],
    0x06: [(0x24, 1, "device number"), (0x26, 6, "MAC"), (0x2C, 4, "IP")],
}

LEARNING_TIME = 5.0        # seconds of watching before reporting anything


def field(packet_type: int, off: int):
    for start, n, name in FIELDS.get(packet_type, ()):
        if start <= off < start + n:
            return start, n, name
    return None


def format_value(name: str, raw: int) -> str:
    if "PITCH" in name or "pitch" in name:
        return f"{(raw / 0x100000 - 1) * 100:+.2f} %"
    if "BPM" in name:
        return f"{raw / 100:.2f}"
    if "FLAGS" in name:
        on = [n for bit, n in ((0x08, "on air"), (0x10, "sync"),
                               (0x20, "master"), (0x40, "play")) if raw & bit]
        return f"0x{raw:04x} [{', '.join(on) or 'none'}]"
    return str(raw)


def main() -> int:
    p = argparse.ArgumentParser(description="Pro DJ Link protocol probe")
    p.add_argument("--host", default=None)
    p.add_argument("--mode", choices=["auto", "vcdj", "sniffer"], default="auto")
    p.add_argument("--number", type=int, default=5)
    p.add_argument("--name", default="monitor")
    p.add_argument("--tshark", default=None)
    p.add_argument("--iface", default=None)
    p.add_argument("--all", action="store_true",
                   help="do not discard the fields that move on their own")
    args = p.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # show output as it happens

    net = link.local_net(args.host or "255.255.255.255")
    source = build_source(args, net)

    seen: dict[tuple, dict[int, set]] = {}
    stable: dict[tuple, set[int]] = {}
    started = time.monotonic()
    changes = [0]
    phase = ["learning"]

    def on_packet(data: bytes, ip: str, ts: float) -> None:
        if not data.startswith(proto.MAGIC) or len(data) < 0x22:
            return
        packet_type = data[10]
        dev = data[0x24] if packet_type == 0x06 else data[0x21]
        key = (packet_type, dev, len(data))
        base = seen.setdefault(key, {})

        if phase[0] == "learning":
            for i, b in enumerate(data):
                base.setdefault(i, set()).add(b)
            return

        watched = stable.get(key)
        if watched is None:                       # a packet kind new since learning
            for i, b in enumerate(data):
                base.setdefault(i, set()).add(b)
            return

        reported = set()
        for i in watched:
            if i >= len(data):
                continue
            b = data[i]
            known = base[i]
            if b in known:
                continue
            known.add(b)
            f = field(packet_type, i)
            if f is None:                    # byte with no known field: report raw
                changes[0] += 1
                print(f"  dev {dev:<3} type 0x{packet_type:02x}  byte 0x{i:02x} "
                      f"UNIDENTIFIED -> 0x{b:02x} ({b})")
                continue
            start, n, name = f
            if (packet_type, dev, start) in reported:     # one field, one notice
                continue
            reported.add((packet_type, dev, start))
            raw = int.from_bytes(data[start:start + n], "big")
            changes[0] += 1
            print(f"  dev {dev:<3} type 0x{packet_type:02x}  {name}")
            print(f"           = {format_value(name, raw)}   "
                  f"(bytes 0x{start:02x}-0x{start + n - 1:02x} = "
                  f"{' '.join(f'{x:02x}' for x in data[start:start + n])})")

    source.start(on_packet)
    print(f"Probe running ({source.description})")
    print(f"Learning the idle state for {LEARNING_TIME:.0f}s, do not touch anything...")
    try:
        time.sleep(LEARNING_TIME)
        # watch every byte that stayed still: a fader at rest is constant, so it
        # shows up the moment it moves
        total = noisy = 0
        for key, base in seen.items():
            if args.all:
                stable[key] = set(base)
            else:
                stable[key] = {i for i, v in base.items()
                               if len(v) <= MAX_STABLE_VALUES}
            total += len(base)
            noisy += len(base) - len(stable[key])
        phase[0] = "watching"

        print(f"\nWatching {total - noisy} of {total} bytes across "
              f"{len(seen)} packet kinds ({noisy} discarded for moving on their own).")
        print("READY. Now move the faders, the crossfader, the EQ, whatever you like.")
        print("Every byte that changes shows up here. Ctrl+C to quit.\n")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{changes[0]} changes detected.")
        if not changes[0]:
            print("None: that control is not transmitted over Pro DJ Link.")
    finally:
        source.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
