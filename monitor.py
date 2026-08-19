"""Console monitor: deck state in the terminal, no browser needed.

Handy for checking at a glance that the connection works, especially when
testing the virtual device mode.

    python monitor.py                  auto-detect the mode
    python monitor.py --mode vcdj      force the virtual device
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import build_source                          # noqa: E402
from prolink import link                              # noqa: E402
from prolink.library import Library                   # noqa: E402

GREEN, AMBER, CYAN, PINK, RED, GREY, RESET = (
    "\033[92m", "\033[93m", "\033[96m", "\033[95m", "\033[91m", "\033[90m", "\033[0m")
DECK_COLOR = {1: AMBER, 2: CYAN, 3: PINK, 4: GREEN}

STATE_TEXT = {
    "no_track": "no track", "loading": "loading", "playing": "playing",
    "looping": "looping", "paused": "paused", "cued": "cued",
    "cue_play": "cue play", "cue_scratch": "cue scratch", "searching": "searching",
    "unplayable": "unplayable", "end_of_track": "end of track",
    "emergency": "emergency", "unknown": "unknown",
}


def mmss(ms: float) -> str:
    ms = max(0, ms)
    return f"{int(ms // 60000):02d}:{int(ms // 1000) % 60:02d}"


def bar(fraction: float, width: int = 34) -> str:
    filled = max(0, min(width, int(fraction * width)))
    return "#" * filled + GREY + "-" * (width - filled) + RESET


def main() -> int:
    p = argparse.ArgumentParser(description="Pro DJ Link console monitor")
    p.add_argument("--host", default=None)
    p.add_argument("--mode", choices=["auto", "vcdj", "sniffer"], default="auto")
    p.add_argument("--number", type=int, default=5)
    p.add_argument("--name", default="monitor")
    p.add_argument("--tshark", default=None)
    p.add_argument("--iface", default=None)
    args = p.parse_args()

    if os.name == "nt":
        # enable ENABLE_VIRTUAL_TERMINAL_PROCESSING so cmd renders the colours
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)

    net = link.local_net(args.host or "255.255.255.255")
    source = build_source(args, net)
    engine = link.ProLink(source)
    library = Library()
    tracks: dict[int, object] = {}
    host = [args.host]

    def on_track_change(deck: link.Deck, track_id: int) -> None:
        if not track_id:
            return
        if not host[0]:
            players = engine.players()
            if not players:
                return
            host[0] = players[0].ip
        media = library.get(host[0])
        if media is None:
            return
        a = media.analysis(track_id)
        if a:
            deck.set_beat_grid([b.time for b in a.beats], a.duration_ms)
        tracks[track_id] = media.track(track_id)

    engine.on_track_change = on_track_change
    engine.start()
    print(f"Mode: {source.description}\nCtrl+C to quit\n")

    try:
        while True:
            decks = engine.active_decks()
            lines = [f"\033[H\033[J{GREY}-- Pro DJ Link -- {len(engine.devices)} "
                     f"devices -- {engine.packets} packets --{RESET}\n"]
            if not decks:
                lines.append("  waiting for player status...\n")
            for d in decks:
                s = d.status
                c = DECK_COLOR.get(d.number, "")
                t = tracks.get(s.track_id)
                title = f"{t.artist} - {t.title}" if t else (
                    f"track {s.track_id}" if s.track_id else "no track")
                flags = " ".join(filter(None, [
                    f"{AMBER}MASTER{RESET}" if s.master else "",
                    f"{GREY}SYNC{RESET}" if s.sync else "",
                    f"{RED}ON AIR{RESET}" if s.on_air else ""]))
                pos = d.position_ms
                dur = d.track_length_ms
                lines.append(f"{c}|{d.number}{RESET} {title[:62]:<62} {flags}")
                lines.append(
                    f"   {c}{s.effective_bpm:7.2f}{RESET} BPM  "
                    f"{s.pitch_percent:+6.2f}%  "
                    f"{mmss(pos)} / {mmss(dur)}  "
                    f"{GREY}-{mmss(dur - pos)}{RESET}  "
                    f"beat {s.beat_count}.{s.beat_in_bar}/4  "
                    f"{(t.key if t else ''):<4} "
                    f"{STATE_TEXT.get(s.play_state, s.play_state)}")
                lines.append(f"   {bar(pos / dur if dur else 0)}\n")
            sys.stdout.write("\n".join(lines))
            sys.stdout.flush()
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        engine.stop()
        library.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
