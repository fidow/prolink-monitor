"""Pro DJ Link monitor: a web server showing what is playing on each deck, live.

Typical use:

    python app.py                       auto-detect the mode and open a browser
    python app.py --mode sniffer        passive capture, coexists with rekordbox
    python app.py --mode vcdj           virtual device (rekordbox closed)
    python app.py --host 192.168.2.100  player address, to read its media
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prolink import anlz, link, proto                     # noqa: E402
from prolink.library import Library                       # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

OVERVIEW_COLUMNS = 1600          # resolution of the track overview we send


class Monitor:
    """Joins the network engine with the library and exposes resolved state."""

    def __init__(self, host: str | None, source, cache_dir: str | None = None,
                 local_ip: str | None = None):
        self.host = host
        self.local_ip = local_ip      # to ignore our own announcement
        self.engine = link.ProLink(source)
        self.library = Library(cache_dir)
        self.engine.on_track_change = self._on_track_change
        self._waveforms: dict[int, bytes] = {}
        self._meta: dict[int, dict] = {}
        # which player each track was read from: with separate CDJs the track
        # loaded on one deck often lives on another deck's USB drive
        self._track_host: dict[int, str] = {}
        self._lock = threading.RLock()

    def start(self) -> None:
        self.engine.start()
        threading.Thread(target=self._preload, daemon=True).start()

    def stop(self) -> None:
        self.engine.stop()
        self.library.close()

    def _preload(self) -> None:
        """Open the NFS connection in the background so the first track is instant."""
        for _ in range(30):
            if self.host:
                break
            players = self.engine.players(exclude_ip=self.local_ip)
            if players:
                self.host = players[0].ip
                print(f"  player found at {self.host}")
                break
            time.sleep(0.5)
        if not self.host:
            return
        try:
            self.library.get(self.host)
        except Exception:
            pass

    # -- tracks -------------------------------------------------------------
    def _host_for(self, status) -> str | None:
        """Address of the player holding the medium a track was loaded from.

        On an all-in-one every deck reports itself, but with separate players
        `loaded_from` points at whichever one has the USB drive in it.
        """
        if status is not None and status.loaded_from:
            with self.engine.lock:
                device = self.engine.devices.get(status.loaded_from)
            if device is not None:
                return device.ip
        return self.host

    def _on_track_change(self, deck: link.Deck, track_id: int) -> None:
        """A track was loaded: fetch its beat grid for the playhead and its waveform."""
        if not track_id:
            return
        status = deck.status
        if status is not None and status.slot == "rekordbox":
            return                       # streamed from rekordbox, not on any medium
        host = self._host_for(status)
        if not host:
            return
        media = self.library.get(host)
        if media is None:
            return
        with self._lock:
            self._track_host[track_id] = host
        try:
            a = media.analysis(track_id)
        except Exception:
            return
        if a is None:
            return
        t = media.track(track_id)
        length = a.duration_ms or ((t.duration * 1000) if t else 0)
        deck.set_beat_grid([b.time for b in a.beats], length)
        self._build(track_id, a, t)

    def _build(self, track_id: int, a: anlz.Analysis, t) -> None:
        """Prepare and cache the binary waveform and metadata of a track."""
        with self._lock:
            if track_id in self._waveforms:
                return

        if a.color_detail:
            heights, rgb = anlz.decode_color_detail(a.color_detail)
        elif a.band3_detail:
            heights, rgb = anlz.decode_3band(a.band3_detail)
        elif a.detail:
            h, _ = anlz.decode_blue(a.detail)
            heights = bytearray(h)
            rgb = bytearray(len(h) * 3)
            for i in range(len(h)):
                rgb[i * 3:i * 3 + 3] = b"\x38\x8c\xd8"
        else:
            heights, rgb = bytearray(), bytearray()

        ov_h, ov_rgb = anlz.downsample(heights, rgb, OVERVIEW_COLUMNS) if heights \
            else (bytearray(), bytearray())

        length = a.duration_ms or ((t.duration * 1000) if t else 0)
        payload = (_pack_wave(heights, rgb, anlz.DETAIL_COLUMNS_PER_SECOND, length)
                   + _pack_wave(ov_h, ov_rgb, 0.0, length))

        meta = {
            "id": track_id,
            "title": (t.title if t else "") or "",
            "artist": t.artist if t else "",
            "album": t.album if t else "",
            "genre": t.genre if t else "",
            "key": t.key if t else "",
            "label": t.label if t else "",
            "comment": t.comment if t else "",
            "year": t.year if t else 0,
            "rating": t.rating if t else 0,
            "track_bpm": t.tempo if t else 0.0,
            "duration_ms": length,
            "bitrate": t.bitrate if t else 0,
            "has_artwork": bool(t and t.artwork_path),
            "beats": [[b.time, b.number] for b in a.beats],
            "cues": [{"hot": c.hot_cue, "type": c.type, "t": c.time,
                      "end": c.loop_time, "color": c.color, "text": c.comment}
                     for c in a.cues],
            "phrases": [{"beat": p.beat, "text": p.label} for p in a.phrases],
            "detail_columns": len(heights),
            "overview_columns": len(ov_h),
        }
        with self._lock:
            self._waveforms[track_id] = payload
            self._meta[track_id] = meta
            while len(self._waveforms) > 12:
                oldest = next(iter(self._waveforms))
                self._waveforms.pop(oldest, None)
                self._meta.pop(oldest, None)

    def _media_for(self, track_id: int):
        """The medium a track lives on, from whichever player supplied it."""
        with self._lock:
            host = self._track_host.get(track_id) or self.host
        return self.library.get(host) if host else None

    def ensure(self, track_id: int) -> None:
        """Load a track on demand when it has not been prepared yet."""
        with self._lock:
            if track_id in self._meta:
                return
        media = self._media_for(track_id)
        if media is None:
            return
        a = media.analysis(track_id)
        if a is not None:
            self._build(track_id, a, media.track(track_id))

    def waveform(self, track_id: int) -> bytes | None:
        self.ensure(track_id)
        with self._lock:
            return self._waveforms.get(track_id)

    def meta(self, track_id: int) -> dict | None:
        self.ensure(track_id)
        with self._lock:
            return self._meta.get(track_id)

    def artwork(self, track_id: int) -> bytes | None:
        media = self._media_for(track_id)
        return media.artwork(track_id) if media else None

    # -- state --------------------------------------------------------------
    def state(self) -> dict:
        decks = []
        for d in self.engine.active_decks():
            s = d.status
            if s is None:
                continue
            decks.append({
                "number": d.number,
                "name": s.name,
                "firmware": s.firmware,
                "track_id": s.track_id,
                "slot": s.slot,
                "track_type": s.track_type,
                "loaded_from": s.loaded_from,
                "state": s.play_state,
                "playing": d.is_playing,
                "bpm": round(s.effective_bpm, 2),
                "track_bpm": round(s.bpm, 2),
                "pitch": round(s.pitch_percent, 2),
                "speed": round(s.speed, 6),
                "position_ms": round(d.position_ms, 1),
                "duration_ms": d.track_length_ms,
                "beat": s.beat_count,
                "bar": s.beat_in_bar,
                "beat_packets": d.beat_packets,
                "cue_in": s.cue_distance if s.cue_distance != 0x1FF else None,
                "master": s.master,
                "sync": s.sync,
                "on_air": s.on_air,
            })
        with self.engine.lock:
            devices = [{"number": a.device_number, "name": a.name,
                        "kind": a.kind, "ip": a.ip}
                       for a in sorted(self.engine.devices.values(),
                                       key=lambda x: x.device_number)]
        # totals across every medium we have opened: with separate players there
        # can be a USB drive in more than one of them
        totals: dict[str, int] = {}
        with self.library.lock:
            media_list = list(self.library.media.values())
            first_error = next(iter(self.library.errors.values()), None)
        for m in media_list:
            if m.db is None:
                continue
            for k, v in m.db.counts().items():
                totals[k] = totals.get(k, 0) + v
        return {
            "t": time.time(),
            "decks": decks,
            "devices": devices,
            "packets": self.engine.packets,
            "mode": getattr(self.engine.source, "description", ""),
            "mode_kind": getattr(self.engine.source, "kind", ""),
            "mode_detail": getattr(self.engine.source, "detail", ""),
            "host": self.host or "",
            # counts, not a sentence: the interface writes its own wording
            "library": totals or None,
            "library_error": None if totals else first_error,
        }


def _pack_wave(heights, rgb, columns_per_second: float, duration_ms: int) -> bytes:
    """Pack a waveform: a 24-byte header, then the heights, then the RGB."""
    n = len(heights)
    header = struct.pack("<4sIIfII", b"PLWF", 1, n, columns_per_second, duration_ms, 0)
    return header + bytes(heights) + bytes(rgb)


# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    monitor: Monitor = None            # injected at start-up
    protocol_version = "HTTP/1.1"

    TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".js": "text/javascript; charset=utf-8", ".png": "image/png",
             ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".woff2": "font/woff2"}

    def log_message(self, fmt, *args):    # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(self.monitor.state())
            if path == "/api/events":
                return self._events()
            if path.startswith("/api/track/"):
                track_id = int(path.rsplit("/", 1)[1])
                meta = self.monitor.meta(track_id)
                return self._json(meta or {"error": "no_analysis"}, 200 if meta else 404)
            if path.startswith("/api/waveform/"):
                track_id = int(path.rsplit("/", 1)[1])
                data = self.monitor.waveform(track_id)
                if not data:
                    return self._json({"error": "no_waveform"}, 404)
                return self._send(200, data, "application/octet-stream", "max-age=3600")
            if path.startswith("/api/artwork/"):
                track_id = int(path.rsplit("/", 1)[1])
                art = self.monitor.artwork(track_id)
                if not art:
                    return self._json({"error": "no_artwork"}, 404)
                return self._send(200, art, "image/jpeg", "max-age=3600")
            if not path.startswith("/api/"):
                return self._static(path)
            return self._json({"error": "not_found"}, 404)
        except (ValueError, KeyError):
            self._json({"error": "bad_request"}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _static(self, path: str) -> None:
        """Serve files out of web/, without letting anyone escape that folder."""
        rel = os.path.normpath(path.lstrip("/")).replace("\\", "/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._json({"error": "not_found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        self._file(os.path.relpath(full, WEB_DIR),
                   self.TYPES.get(ext, "application/octet-stream"))

    def _file(self, name: str, ctype: str) -> None:
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._json({"error": "not_found"}, 404)

    def _events(self) -> None:
        """Server-sent events: state at 20 Hz. The client interpolates between them."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(self.monitor.state(), ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class QuietServer(ThreadingHTTPServer):
    """A browser closing an SSE stream is normal, not something to dump a stack for."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


# -------------------------------------------------------------------- start-up

def build_source(args, net: link.NetInfo):
    """Pick the packet source according to the requested mode."""
    if args.mode == "sniffer":
        return link.SnifferSource(net, args.tshark, args.iface)
    if args.mode == "vcdj":
        return link.SocketSource(net, args.number, args.name)
    # auto: prefer the virtual device; fall back to passive capture when the
    # ports are taken
    src = link.SocketSource(net, args.number, args.name)
    probe = link.socket.socket(link.socket.AF_INET, link.socket.SOCK_DGRAM)
    probe.setsockopt(link.socket.SOL_SOCKET, link.socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", proto.PORT_STATUS))
        probe.close()
        return src
    except OSError:
        probe.close()
        print("  ! UDP ports 50000-50002 are busy (rekordbox running?),\n"
              "    switching to passive capture. Close rekordbox if you want the "
              "virtual device mode.")
        return link.SnifferSource(net, args.tshark, args.iface)


def main() -> int:
    p = argparse.ArgumentParser(description="Pro DJ Link monitor for AlphaTheta gear")
    p.add_argument("--host", default=None,
                   help="player address to read the library from "
                        "(auto-detected from the network when omitted)")
    p.add_argument("--mode", choices=["auto", "vcdj", "sniffer"], default="auto")
    p.add_argument("--port", type=int, default=8777, help="web server port")
    p.add_argument("--number", type=int, default=5,
                   help="virtual device number (1-6, avoid the ones in use)")
    p.add_argument("--name", default="monitor", help="name to announce ourselves with")
    p.add_argument("--tshark", default=None, help="path to tshark")
    p.add_argument("--iface", default=None,
                   help="capture interface for sniffer mode (see \"tshark -D\")")
    p.add_argument("--cache", default=None, help="cache folder")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = p.parse_args()

    print("Pro DJ Link monitor")
    host = args.host
    iface = args.iface
    if not host:
        print("  looking for a player on the network...")
        host = link.discover_player(4.0)
        if not host and args.mode != "vcdj":
            # port 50000 is taken (rekordbox), so listen through the capture driver
            # instead: it also tells us which interface the traffic is really on
            print("  port 50000 is busy, looking through the capture driver...")
            try:
                host, found_iface = link.discover_by_capture(args.tshark)
            except RuntimeError:
                host, found_iface = None, None
            if found_iface and not iface:
                iface = found_iface
        if host:
            print(f"  player found at {host}")
        else:
            print("  ! no player answered. Pass --host if auto-detection misses it.")
    args.iface = iface
    net = link.local_net(host or "255.255.255.255")
    print(f"  local network: {net.ip}/{net.prefix} via \"{net.alias}\" "
          f"(broadcast {net.broadcast})")

    try:
        source = build_source(args, net)
    except RuntimeError as e:
        print(f"\nERROR: {e}")
        return 1

    monitor = Monitor(host, source, args.cache, local_ip=net.ip)
    Handler.monitor = monitor
    try:
        monitor.start()
    except RuntimeError as e:
        print(f"\nERROR: {e}")
        return 1
    print(f"  mode: {source.description}")

    server = QuietServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"  panel: {url}\n")

    if monitor.engine.wait_for_devices(6.0):
        for d in monitor.engine.active_decks():
            s = d.status
            print(f"  deck {d.number}: {s.play_state}, track {s.track_id or '-'}, "
                  f"{s.effective_bpm:.2f} BPM")
    else:
        print("  ! no deck is reporting status yet.")
        if isinstance(source, link.SocketSource):
            print("    Check the player is powered on and on the same network.")
        else:
            print("    In passive mode status is only visible while another program\n"
                  "    (rekordbox) is linked to the player.")

    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        monitor.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
