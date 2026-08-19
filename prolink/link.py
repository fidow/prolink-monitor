"""Pro DJ Link engine: discovers devices and tracks the live state of each deck.

Two ways of receiving packets:

  SocketSource   Announces us as one more device on the network. The player then
                 starts sending us its status by unicast. It needs UDP ports
                 50000-50002, so rekordbox must be closed (it claims them
                 exclusively).

  SnifferSource  Passive capture through Npcap/tshark. It opens no port and
                 announces nothing, so it coexists with rekordbox: it reads the
                 very packets the player is sending to it.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import proto

Handler = Callable[[bytes, str, float], None]


# --------------------------------------------------------------- local network

@dataclass
class NetInfo:
    ip: str
    prefix: int
    alias: str

    @property
    def broadcast(self) -> str:
        net = ipaddress.ip_network(f"{self.ip}/{self.prefix}", strict=False)
        return str(net.broadcast_address)


def local_net(target: str) -> NetInfo:
    """Work out which local interface reaches the player, with mask and name."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        ip = s.getsockname()[0]
    finally:
        s.close()

    prefix, alias = 24, ""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetIPAddress -IPAddress {ip} -AddressFamily IPv4 | "
                 "Select-Object -First 1 -Property PrefixLength,InterfaceAlias | "
                 "ForEach-Object { \"$($_.PrefixLength)|$($_.InterfaceAlias)\" }"],
                capture_output=True, text=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            if "|" in out:
                p, alias = out.split("|", 1)
                prefix = int(p)
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    return NetInfo(ip, prefix, alias.strip())


def local_mac() -> bytes:
    node = uuid.getnode()
    return node.to_bytes(6, "big")


def discover_player(timeout: float = 4.0) -> str | None:
    """Listen for keep-alive broadcasts and return the first player's address.

    Worth doing before anything else when no host was given: picking an
    interface by routing towards the broadcast address lands on a virtual
    adapter on machines with VMware or Hyper-V installed. Knowing the player's
    real address first makes the choice unambiguous.

    Returns None when the port is busy (rekordbox running) or nothing answers.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(("0.0.0.0", proto.PORT_ANNOUNCE))
    except OSError:
        s.close()
        return None
    s.settimeout(0.5)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                data, _addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            pkt = proto.parse(data)
            if isinstance(pkt, proto.Announce) and pkt.kind == "player":
                return pkt.ip
    finally:
        s.close()
    return None


def discover_by_capture(tshark: str | None = None,
                        seconds: int = 5) -> tuple[str | None, str | None]:
    """Find the player *and* the right capture interface by listening on all of them.

    This is the fallback for when rekordbox holds port 50000 and we cannot listen
    with a socket. Guessing the interface by routing towards the broadcast address
    lands on a VMware or Hyper-V adapter on many machines, and then we capture
    where nothing is happening. Listening everywhere at once and seeing which
    interface actually carries Pro DJ Link removes the guesswork.

    Returns (player address, capture interface), either of which may be None.
    """
    exe = tshark or SnifferSource._find_tshark()
    interfaces: list[str] = []
    try:
        listing = subprocess.run([exe, "-D"], capture_output=True, text=True,
                                 timeout=30).stdout
    except (subprocess.SubprocessError, OSError):
        return None, None
    for line in listing.splitlines():
        m = re.match(r"\s*(\d+)\.\s+(\S+)", line)
        if not m:
            continue
        name = m.group(2)
        if "Loopback" in name or "etwdump" in name:
            continue                      # never carries player traffic
        interfaces.append(name)
    if not interfaces:
        return None, None

    fields = ["-T", "fields", "-e", "frame.interface_name", "-e", "udp.payload"]
    attempts = [interfaces]                     # all at once...
    attempts += [[i] for i in interfaces]       # ...and one by one if that fails
    for group in attempts:
        cmd = [exe]
        for i in group:
            cmd += ["-i", i]
        cmd += ["-a", f"duration:{seconds}", "-n", "-Q",
                "-f", f"udp port {proto.PORT_ANNOUNCE}"] + fields
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=seconds + 25).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for line in out.splitlines():
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2 or not parts[1]:
                continue
            try:
                data = bytes.fromhex(parts[1].replace(":", ""))
            except ValueError:
                continue
            pkt = proto.parse(data)
            if isinstance(pkt, proto.Announce) and pkt.kind == "player":
                return pkt.ip, (parts[0] or None)
    return None, None


# --------------------------------------------------------------- packet sources

class SocketSource:
    """Virtual device: opens the ports and announces itself on the network."""

    def __init__(self, net: NetInfo, device_number: int = 5,
                 device_name: str = "monitor", announce_interval: float = 1.5):
        self.net = net
        self.device_number = device_number
        self.device_name = device_name
        self.announce_interval = announce_interval
        self._socks: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._running = False
        self.kind = "vcdj"
        self.detail = f"#{device_number} \"{device_name}\""
        self.description = f"virtual device {self.detail}"

    def start(self, on_packet: Handler) -> None:
        self._running = True
        for port in (proto.PORT_ANNOUNCE, proto.PORT_BEAT, proto.PORT_STATUS):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            try:
                s.bind(("0.0.0.0", port))
            except OSError as e:
                for done in self._socks:
                    done.close()
                self._socks.clear()
                raise RuntimeError(
                    f"could not open UDP port {port}: {e}\n"
                    "This is almost always rekordbox, which claims 50000-50002 "
                    "exclusively. Close it, or start in passive mode with "
                    "--mode sniffer."
                ) from e
            s.settimeout(0.5)
            self._socks.append(s)
            t = threading.Thread(target=self._recv_loop, args=(s, on_packet), daemon=True)
            t.start()
            self._threads.append(t)

        t = threading.Thread(target=self._announce_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def _recv_loop(self, sock: socket.socket, on_packet: Handler) -> None:
        while self._running:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            on_packet(data, addr[0], time.monotonic())

    def _announce_loop(self) -> None:
        pkt = proto.build_keepalive(self.device_name, self.device_number,
                                    local_mac(), self.net.ip)
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        targets = {self.net.broadcast, "255.255.255.255"}
        while self._running:
            for dst in targets:
                try:
                    out.sendto(pkt, (dst, proto.PORT_ANNOUNCE))
                except OSError:
                    pass
            time.sleep(self.announce_interval)
        out.close()

    def stop(self) -> None:
        self._running = False
        for s in self._socks:
            s.close()
        self._socks.clear()


class SnifferSource:
    """Passive capture through tshark (Npcap). Does not interfere with rekordbox."""

    TSHARK_PATHS = [
        r"C:\Program Files\Wireshark\tshark.exe",
        r"C:\Program Files (x86)\Wireshark\tshark.exe",
        "/usr/bin/tshark",
        "/usr/local/bin/tshark",
        "tshark",
    ]

    def __init__(self, net: NetInfo, tshark: str | None = None, iface: str | None = None):
        self.net = net
        self.tshark = tshark or self._find_tshark()
        self.iface = iface
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.kind = "sniffer"
        self.detail = iface or net.alias or net.ip
        self.description = f"passive capture on \"{self.detail}\" via Npcap"

    @classmethod
    def _find_tshark(cls) -> str:
        import os
        import shutil
        for p in cls.TSHARK_PATHS:
            if p == "tshark":
                found = shutil.which(p)
                if found:
                    return found
            elif os.path.exists(p):
                return p
        raise RuntimeError(
            "tshark not found. Passive mode needs Wireshark/Npcap installed "
            "(https://www.wireshark.org). Otherwise close rekordbox and use the "
            "virtual device mode."
        )

    def _interface(self) -> str:
        """Find the capture index matching our interface."""
        if self.iface:
            return self.iface
        out = subprocess.run([self.tshark, "-D"], capture_output=True, text=True,
                             timeout=30).stdout
        alias = self.net.alias
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\.\s+(\S+)\s*(?:\((.*)\))?", line)
            if not m:
                continue
            number, _device, friendly = m.groups()
            if alias and friendly and friendly.strip().lower() == alias.lower():
                return number
        raise RuntimeError(
            f"could not find interface \"{alias}\" among the ones tshark lists. "
            f"Pass it by hand with --iface. Available:\n{out}"
        )

    def start(self, on_packet: Handler) -> None:
        iface = self._interface()
        # frame.time_epoch is essential: tshark delivers its output in batches, so
        # the moment we read a line says nothing about when the packet arrived.
        # Without it the playhead jumps on every beat.
        cmd = [self.tshark, "-i", iface, "-l", "-n", "-Q",
               "-f", "udp portrange 50000-50002",
               "-T", "fields", "-e", "frame.time_epoch", "-e", "ip.src",
               "-e", "udp.payload"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, bufsize=1)
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, args=(on_packet,), daemon=True)
        self._thread.start()

    def _read_loop(self, on_packet: Handler) -> None:
        assert self._proc and self._proc.stdout
        offset: float | None = None       # wall clock to monotonic
        for line in self._proc.stdout:
            if not self._running:
                return
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3 or not parts[2]:
                continue
            try:
                data = bytes.fromhex(parts[2].replace(":", ""))
                epoch = float(parts[0])
            except ValueError:
                continue
            if offset is None:
                offset = time.monotonic() - epoch
            on_packet(data, parts[1], epoch + offset)

    def stop(self) -> None:
        self._running = False
        if self._proc:
            self._proc.terminate()
            self._proc = None


# ----------------------------------------------------------------------- state

# Correction applied on every beat. The observed instant arrives with a variable
# delay (status only comes ~6 times a second), so correcting it all at once makes
# the playhead jump back and forth. Correcting a fraction of the error absorbs
# that noise without losing track.
SMOOTHING = 0.2
# Above this error it is not noise: it is a real jump (cue, scratch, seek).
JUMP_MS = 400.0


@dataclass
class Deck:
    """Live state of one deck, with the playhead interpolated between beats.

    Position is kept as a continuous model (base position, base time, speed)
    that is only nudged, instead of being re-anchored on every beat.
    """
    number: int
    status: proto.Status | None = None
    last_seen: float = 0.0
    last_beat_packet: float = 0.0
    beat_in_bar: int = 0
    beat_packets: int = 0

    # (position in ms, monotonic instant, speed) -- replaced as a tuple so the
    # web server threads always read a consistent model
    _model: tuple[float, float, float] = (0.0, 0.0, 0.0)
    _last_beat: int = -1
    _beat_times: list[int] = field(default_factory=list, repr=False)
    track_length_ms: int = 0

    def set_beat_grid(self, times: list[int], length_ms: int = 0) -> None:
        self._beat_times = times
        self.track_length_ms = length_ms or (times[-1] if times else 0)
        self._last_beat = -1          # force a re-fix now the grid is loaded

    def _beat_time(self, beat: int) -> int:
        """Millisecond a beat starts at, according to the analysed grid."""
        if beat < 1:
            return 0            # the deck has not crossed the first beat yet
        if self._beat_times:
            if beat <= len(self._beat_times):
                return self._beat_times[beat - 1]
            return self._beat_times[-1]
        s = self.status
        if s and s.bpm:                      # no grid: estimate from the tempo
            return int((beat - 1) * 60000.0 / s.bpm)
        return 0

    def _project(self, now: float) -> float:
        """Position of the continuous model at a given instant."""
        pos, t, speed = self._model
        return pos + (now - t) * 1000.0 * speed

    def on_status(self, s: proto.Status, now: float) -> None:
        prev = self.status
        self.status = s
        self.last_seen = now
        if s.beat_in_bar:
            self.beat_in_bar = s.beat_in_bar

        new_track = prev is None or prev.track_id != s.track_id
        if new_track:
            self._beat_times = []
            self.track_length_ms = 0
            self._last_beat = -1
            self._model = (0.0, now, 0.0)

        # advance the model up to now at the speed it carried, then continue at
        # the new one
        self._model = (self._project(now), now, s.speed if s.has_track else 0.0)

        if s.beat_count == self._last_beat:
            return
        self._last_beat = s.beat_count

        # when the beat happened: the beat packet marks it precisely, while the
        # status arrives with up to ~150 ms of variable delay
        delay = now - self.last_beat_packet
        beat_time = self.last_beat_packet if delay < 0.25 else now
        target = self._beat_time(s.beat_count) + (now - beat_time) * 1000.0 * s.speed

        pos, _, speed = self._model
        error = target - pos
        if new_track or not s.speed or abs(error) > JUMP_MS:
            self._model = (target, now, speed)          # real jump: re-place it
        else:
            self._model = (pos + error * SMOOTHING, now, speed)

    def on_beat(self, b: proto.Beat, now: float) -> None:
        self.last_beat_packet = now
        self.beat_packets += 1
        self.beat_in_bar = b.beat_in_bar

    @property
    def position_ms(self) -> float:
        """Estimated playhead position, in milliseconds."""
        s = self.status
        if not s or not s.has_track:
            return 0.0
        pos = self._project(time.monotonic())
        if self.track_length_ms:
            pos = min(pos, self.track_length_ms)
        return max(0.0, pos)

    @property
    def is_playing(self) -> bool:
        s = self.status
        return bool(s and s.play_state_raw in proto.PLAYING_STATES)


class ProLink:
    """Discovers the Pro DJ Link network and keeps every deck's state."""

    def __init__(self, source):
        self.source = source
        self.devices: dict[int, proto.Announce] = {}
        self.decks: dict[int, Deck] = {}
        self.lock = threading.RLock()
        self.on_track_change: Callable[[Deck, int], None] | None = None
        self._last_track: dict[int, int] = {}
        self.packets = 0
        self.started_at = 0.0

    def start(self) -> None:
        self.started_at = time.monotonic()
        self.source.start(self._on_packet)

    def stop(self) -> None:
        self.source.stop()

    def _deck(self, number: int) -> Deck:
        d = self.decks.get(number)
        if d is None:
            d = Deck(number)
            self.decks[number] = d
        return d

    def _on_packet(self, data: bytes, src_ip: str, now: float) -> None:
        """`now` is when the packet arrived, not when we got round to it."""
        pkt = proto.parse(data)
        if pkt is None:
            return
        with self.lock:
            self.packets += 1
            if isinstance(pkt, proto.Announce):
                self.devices[pkt.device_number] = pkt
            elif isinstance(pkt, proto.Beat):
                self._deck(pkt.device_number).on_beat(pkt, now)
            elif isinstance(pkt, proto.Status):
                deck = self._deck(pkt.device_number)
                deck.on_status(pkt, now)
                before = self._last_track.get(pkt.device_number)
                if before != pkt.track_id:
                    self._last_track[pkt.device_number] = pkt.track_id
                    if self.on_track_change:
                        threading.Thread(target=self.on_track_change,
                                         args=(deck, pkt.track_id), daemon=True).start()

    def active_decks(self, timeout: float = 5.0) -> list[Deck]:
        """Decks we have heard from recently, ordered by number."""
        now = time.monotonic()
        with self.lock:
            return sorted((d for d in self.decks.values()
                           if d.status and now - d.last_seen < timeout),
                          key=lambda d: d.number)

    def players(self, exclude_ip: str | None = None) -> list[proto.Announce]:
        """Announced devices that are actual players (device numbers 1-6).

        `exclude_ip` drops our own announcement: in virtual device mode we take a
        number in that same range and hear our own broadcast back.
        """
        with self.lock:
            return sorted((a for a in self.devices.values()
                           if a.kind == "player" and a.ip != exclude_ip),
                          key=lambda a: a.device_number)

    def wait_for_devices(self, seconds: float = 5.0) -> bool:
        """Wait until some deck reports status. True if anything arrived."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.active_decks():
                return True
            time.sleep(0.2)
        return False
