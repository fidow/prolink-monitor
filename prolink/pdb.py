"""Parser for export.pdb, the database rekordbox exports to USB/SD.

Format documented by the crate-digger project (Deep Symmetry), itself based on
the work of @henrybetts and @flesniak.

The file is a sequence of fixed-size pages. Each table is a linked list of
pages; within a page the rows are located through an index that grows backwards
from the end of the page.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Table types
TRACKS = 0
GENRES = 1
ARTISTS = 2
ALBUMS = 3
LABELS = 4
KEYS = 5
COLORS = 6
PLAYLIST_TREE = 7
PLAYLIST_ENTRIES = 8
ARTWORK = 13

HEAP_POS = 0x28          # rows live past the page header
GROUP_SIZE = 0x24        # size of each group of 16 row offsets


@dataclass
class Track:
    id: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    key: str = ""
    label: str = ""
    remixer: str = ""
    composer: str = ""
    original_artist: str = ""
    color: str = ""
    comment: str = ""
    mix_name: str = ""
    duration: int = 0            # seconds
    tempo: float = 0.0           # BPM
    rating: int = 0
    year: int = 0
    bitrate: int = 0
    sample_rate: int = 0
    sample_depth: int = 0
    file_size: int = 0
    track_number: int = 0
    disc_number: int = 0
    play_count: int = 0
    date_added: str = ""
    release_date: str = ""
    analyze_date: str = ""
    filename: str = ""
    file_path: str = ""
    analyze_path: str = ""
    artwork_id: int = 0
    artwork_path: str = ""
    # unresolved ids, handy for debugging
    ids: dict = field(default_factory=dict)


class PdbError(Exception):
    pass


def _sql_string(buf: bytes, pos: int) -> str:
    """Read a string in the database's variable-length format."""
    if pos <= 0 or pos >= len(buf):
        return ""
    kind = buf[pos]
    try:
        if kind == 0x40 or kind == 0x90:
            length = struct.unpack_from("<H", buf, pos + 1)[0]
            body = buf[pos + 4:pos + 4 + max(0, length - 4)]
            if kind == 0x90:
                return body.decode("utf-16-le", "replace").rstrip("\0")
            return body.decode("latin-1", "replace").rstrip("\0")
        length = kind >> 1
        if length < 1:
            return ""
        return buf[pos + 1:pos + length].decode("latin-1", "replace").rstrip("\0")
    except (struct.error, IndexError):
        return ""


class PdbDatabase:
    """An exported rekordbox database, indexed in memory."""

    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 28:
            raise PdbError("file too short to be an export.pdb")
        _, self.len_page, self.num_tables, self.next_unused, _, self.sequence = \
            struct.unpack_from("<IIIIII", data, 0)
        if self.len_page == 0 or self.len_page > len(data):
            raise PdbError(f"invalid page size: {self.len_page}")

        self.tables: dict[int, tuple[int, int]] = {}
        off = 28
        for _ in range(self.num_tables):
            if off + 16 > len(data):
                break
            table_type, _empty, first, last = struct.unpack_from("<IIII", data, off)
            self.tables[table_type] = (first, last)
            off += 16

        self.artists: dict[int, str] = {}
        self.albums: dict[int, str] = {}
        self.genres: dict[int, str] = {}
        self.keys: dict[int, str] = {}
        self.labels: dict[int, str] = {}
        self.colors: dict[int, str] = {}
        self.artwork: dict[int, str] = {}
        self.tracks: dict[int, Track] = {}
        self._load()

    # -- walking pages ------------------------------------------------------
    def _pages(self, table_type: int):
        entry = self.tables.get(table_type)
        if not entry:
            return
        first, last = entry
        seen = set()
        index = first
        while index not in seen:
            seen.add(index)
            base = index * self.len_page
            if base + HEAP_POS > len(self.data):
                return
            page_type = struct.unpack_from("<I", self.data, base + 8)[0]
            next_page = struct.unpack_from("<I", self.data, base + 12)[0]
            if page_type != table_type:
                return
            yield base
            if index == last:
                return
            index = next_page

    def _rows(self, table_type: int):
        """Yield the absolute offset of every present row in the table."""
        for base in self._pages(table_type):
            v = int.from_bytes(self.data[base + 24:base + 27], "little")
            num_row_offsets = v & 0x1FFF
            page_flags = self.data[base + 27]
            if page_flags & 0x40:                     # not a data page
                continue
            if num_row_offsets == 0:
                continue
            num_groups = (num_row_offsets - 1) // 16 + 1
            for g in range(num_groups):
                group_base = self.len_page - g * GROUP_SIZE
                flags_pos = base + group_base - 4
                if flags_pos < base or flags_pos + 2 > len(self.data):
                    continue
                present = struct.unpack_from("<H", self.data, flags_pos)[0]
                for i in range(16):
                    if g * 16 + i >= num_row_offsets:
                        break
                    if not (present >> i) & 1:
                        continue
                    offset_pos = base + group_base - (6 + 2 * i)
                    if offset_pos < base or offset_pos + 2 > len(self.data):
                        continue
                    row_offset = struct.unpack_from("<H", self.data, offset_pos)[0]
                    row = base + HEAP_POS + row_offset
                    if row < len(self.data):
                        yield row

    # -- simple tables ------------------------------------------------------
    def _load_simple(self, table_type: int, id_fmt: str, id_off: int, str_off: int,
                     target: dict) -> None:
        d = self.data
        for row in self._rows(table_type):
            try:
                ident = struct.unpack_from(id_fmt, d, row + id_off)[0]
            except struct.error:
                continue
            target[ident] = _sql_string(d, row + str_off)

    def _load_named(self, table_type: int, near_off: int, far_off: int,
                    id_off: int, target: dict) -> None:
        """Artist and album tables, with either a short or a long name offset."""
        d = self.data
        for row in self._rows(table_type):
            try:
                subtype = struct.unpack_from("<H", d, row)[0]
                ident = struct.unpack_from("<I", d, row + id_off)[0]
            except struct.error:
                continue
            if subtype & 0x04:
                try:
                    offset = struct.unpack_from("<H", d, row + far_off)[0]
                except struct.error:
                    continue
            else:
                offset = d[row + near_off]
            target[ident] = _sql_string(d, row + offset)

    def _load(self) -> None:
        self._load_named(ARTISTS, near_off=0x09, far_off=0x0A, id_off=0x04,
                         target=self.artists)
        self._load_named(ALBUMS, near_off=0x15, far_off=0x16, id_off=0x0C,
                         target=self.albums)
        self._load_simple(GENRES, "<I", 0x00, 0x04, self.genres)
        self._load_simple(LABELS, "<I", 0x00, 0x04, self.labels)
        self._load_simple(KEYS, "<I", 0x00, 0x08, self.keys)
        self._load_simple(COLORS, "<H", 0x05, 0x08, self.colors)
        self._load_simple(ARTWORK, "<I", 0x00, 0x04, self.artwork)
        self._load_tracks()

    # -- tracks -------------------------------------------------------------
    def _load_tracks(self) -> None:
        d = self.data
        for row in self._rows(TRACKS):
            if row + 0x88 > len(d):
                continue
            try:
                (sample_rate, composer_id, file_size) = struct.unpack_from("<III", d,
                                                                          row + 0x08)
                (artwork_id, key_id, original_artist_id, label_id, remixer_id, bitrate,
                 track_number, tempo, genre_id, album_id, artist_id,
                 track_id) = struct.unpack_from("<IIIIIIIIIIII", d, row + 0x1C)
                (disc_number, play_count, year, sample_depth,
                 duration) = struct.unpack_from("<HHHHH", d, row + 0x4C)
                color_id = d[row + 0x58]
                rating = d[row + 0x59]
                offsets = struct.unpack_from("<21H", d, row + 0x5E)
            except struct.error:
                continue
            if track_id == 0:
                continue

            def s(i: int) -> str:
                return _sql_string(d, row + offsets[i])

            self.tracks[track_id] = Track(
                id=track_id,
                title=s(17),
                artist=self.artists.get(artist_id, ""),
                album=self.albums.get(album_id, ""),
                genre=self.genres.get(genre_id, ""),
                key=self.keys.get(key_id, ""),
                label=self.labels.get(label_id, ""),
                remixer=self.artists.get(remixer_id, ""),
                composer=self.artists.get(composer_id, ""),
                original_artist=self.artists.get(original_artist_id, ""),
                color=self.colors.get(color_id, ""),
                comment=s(16),
                mix_name=s(12),
                duration=duration,
                tempo=tempo / 100.0,
                rating=rating,
                year=year,
                bitrate=bitrate,
                sample_rate=sample_rate,
                sample_depth=sample_depth,
                file_size=file_size,
                track_number=track_number,
                disc_number=disc_number,
                play_count=play_count,
                date_added=s(10),
                release_date=s(11),
                analyze_date=s(15),
                filename=s(19),
                file_path=s(20),
                analyze_path=s(14),
                artwork_id=artwork_id,
                artwork_path=self.artwork.get(artwork_id, ""),
                ids={"artist": artist_id, "album": album_id, "genre": genre_id,
                     "key": key_id, "label": label_id, "artwork": artwork_id,
                     "color": color_id},
            )

    # -- queries ------------------------------------------------------------
    def get(self, track_id: int) -> Track | None:
        return self.tracks.get(track_id)

    def __len__(self) -> int:
        return len(self.tracks)

    def summary(self) -> str:
        return (f"{len(self.tracks)} tracks, {len(self.artists)} artists, "
                f"{len(self.albums)} albums, {len(self.genres)} genres, "
                f"{len(self.keys)} keys, {len(self.artwork)} artwork files")

    def counts(self) -> dict[str, int]:
        """Library totals, for interfaces that render their own wording."""
        return {"tracks": len(self.tracks), "artists": len(self.artists),
                "albums": len(self.albums), "genres": len(self.genres),
                "keys": len(self.keys), "artwork": len(self.artwork)}


def load(path: str) -> PdbDatabase:
    with open(path, "rb") as f:
        return PdbDatabase(f.read())
