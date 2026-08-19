"""Player library: ties NFS access, export.pdb and the analysis files together.

Downloads the exported database once and, on demand, the analysis and artwork of
each track as it gets loaded on a deck. Everything is cached on disk so that
restarting the program is instant.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from . import anlz, pdb
from .nfs import NfsClient

PDB_PATH = "PIONEER/rekordbox/export.pdb"


class Media:
    """One medium (USB/SD) mounted in a player, reachable over NFS."""

    def __init__(self, host: str, export: str | None = None, cache_dir: str | None = None,
                 max_analysis: int = 32):
        self.host = host
        self.cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".prolink-cache", host.replace(":", "_"))
        os.makedirs(self.cache_dir, exist_ok=True)
        self.lock = threading.RLock()
        self._analysis: OrderedDict[int, anlz.Analysis] = OrderedDict()
        self._artwork: OrderedDict[int, bytes] = OrderedDict()
        self._max_analysis = max_analysis
        self.db: pdb.PdbDatabase | None = None
        self.export = export
        self.loaded_at = 0.0

        self.nfs = NfsClient(host)
        exports = self.nfs.exports()
        if not exports:
            raise RuntimeError(f"{host} exports no media over NFS "
                               "(is a USB drive or SD card inserted?)")
        self.export = export or exports[0]
        self.available_exports = exports
        self.root = self.nfs.mount(self.export)

    # -- database -----------------------------------------------------------
    def load_database(self, force: bool = False) -> pdb.PdbDatabase:
        """Download and parse export.pdb. Reuses the cached copy when it matches."""
        with self.lock:
            if self.db is not None and not force:
                return self.db
            local = os.path.join(self.cache_dir, "export.pdb")
            _fh, attr = self.nfs.resolve(self.root, PDB_PATH)
            reuse = (not force and os.path.exists(local)
                     and os.path.getsize(local) == attr.size)
            if not reuse:
                self.nfs.download(self.root, PDB_PATH, local)
            with open(local, "rb") as f:
                self.db = pdb.PdbDatabase(f.read())
            self.loaded_at = time.time()
            return self.db

    def track(self, track_id: int) -> pdb.Track | None:
        return self.load_database().get(track_id)

    # -- analysis -----------------------------------------------------------
    def analysis(self, track_id: int) -> anlz.Analysis | None:
        """Beat grid, cues and waveforms for a track."""
        with self.lock:
            cached = self._analysis.get(track_id)
            if cached is not None:
                self._analysis.move_to_end(track_id)
                return cached

        t = self.track(track_id)
        if not t or not t.analyze_path:
            return None

        result: anlz.Analysis | None = None
        base = t.analyze_path.lstrip("/")
        # the .DAT holds the grid and the overview; the .EXT and .2EX the big waveforms
        for ext in (".DAT", ".EXT", ".2EX"):
            path = base[:-4] + ext if base.upper().endswith(".DAT") else base
            data = self._fetch(path, f"anlz_{track_id}{ext}")
            if data is None:
                continue
            try:
                result = anlz.parse(data, result)
            except ValueError:
                continue

        if result is not None:
            with self.lock:
                self._analysis[track_id] = result
                while len(self._analysis) > self._max_analysis:
                    self._analysis.popitem(last=False)
        return result

    def artwork(self, track_id: int) -> bytes | None:
        """Album art as JPEG, or None when the track has none."""
        with self.lock:
            cached = self._artwork.get(track_id)
            if cached is not None:
                self._artwork.move_to_end(track_id)
                return cached
        t = self.track(track_id)
        if not t or not t.artwork_path:
            return None
        data = self._fetch(t.artwork_path.lstrip("/"), f"art_{track_id}.jpg")
        if data:
            with self.lock:
                self._artwork[track_id] = data
                while len(self._artwork) > 64:
                    self._artwork.popitem(last=False)
        return data

    # -- helpers ------------------------------------------------------------
    def _fetch(self, remote: str, cache_name: str) -> bytes | None:
        """Read a file off the medium, serving it from the disk cache if present."""
        local = os.path.join(self.cache_dir, cache_name)
        if os.path.exists(local):
            try:
                with open(local, "rb") as f:
                    return f.read()
            except OSError:
                pass
        try:
            fh, attr = self.nfs.resolve(self.root, remote)
            data = self.nfs.read(fh, attr.size)
        except Exception:
            return None
        try:
            with open(local, "wb") as f:
                f.write(data)
        except OSError:
            pass
        return data

    def close(self) -> None:
        self.nfs.close()


class Library:
    """A set of media, indexed by player address.

    An all-in-one such as the XDJ-AZ serves every deck from the same IP, so in
    practice there is usually a single shared medium.
    """

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = cache_dir
        self.media: dict[str, Media] = {}
        self.errors: dict[str, str] = {}
        self.lock = threading.RLock()

    def get(self, host: str) -> Media | None:
        with self.lock:
            if host in self.media:
                return self.media[host]
            if host in self.errors:
                return None
        try:
            m = Media(host, cache_dir=self.cache_dir)
            m.load_database()
        except Exception as e:                    # no medium, no NFS, drive removed
            with self.lock:
                self.errors[host] = str(e)
            return None
        with self.lock:
            self.media[host] = m
        return m

    def retry(self, host: str) -> None:
        """Forget a previous failure so the connection is attempted again."""
        with self.lock:
            self.errors.pop(host, None)

    def close(self) -> None:
        with self.lock:
            for m in self.media.values():
                m.close()
            self.media.clear()
