"""Archive file layout, text block storage and opening.

An archive is one SQLite database with this logical content:

``meta``
    format marker, normalization form, Unicode version, block sizes, the
    working-memory limit used at build time.
``unicode_tables``
    a serialized copy of the Unicode data used to normalize the text, so
    reopening never depends on the interpreter's Unicode version.
``documents``
    per-document summary: fragment count, original byte count, NFC code point
    count and SHA-256 digests of both.
``fragments``
    ordered fragments of each document: identifier, original file name, byte
    size, SHA-256.
``text_blocks``
    the NFC text, in blocks of ``text_block_size`` code points (UTF-8 BLOB per
    block), so a range query reads only the blocks it touches.
``cp_spans``
    the exact original byte spans for every normalized code point, one row per
    span: ``(doc, cp, idx, frag, off, len)``.
``rev``
    the reverse index: every span is split at block boundaries so that a byte
    range query touches only the blocks it intersects.  Indexed by
    ``(doc, frag, block, s)``.

All writes happen in one transaction and the database file is only moved to its
final path after commit, so a failed build never leaves an openable archive.
"""

import hashlib
import os
import sqlite3
import tempfile

from . import ARCHIVE_FORMAT

TEXT_BLOCK_SHIFT = 10          # 1024 code points per NFC text block
TEXT_BLOCK_SIZE = 1 << TEXT_BLOCK_SHIFT
REV_BLOCK_SHIFT = 10           # spans are split every 1024 source bytes
REV_BLOCK_SIZE = 1 << REV_BLOCK_SHIFT


SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE unicode_tables (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    data BLOB NOT NULL
);

CREATE TABLE documents (
    doc             INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    fragment_count  INTEGER NOT NULL,
    byte_count      INTEGER NOT NULL,
    cp_count        INTEGER NOT NULL,
    sha256_bytes    TEXT NOT NULL,
    sha256_text     TEXT NOT NULL
);

CREATE TABLE fragments (
    frag       INTEGER PRIMARY KEY,
    doc        INTEGER NOT NULL REFERENCES documents(doc),
    ordinal    INTEGER NOT NULL,
    frag_id    TEXT NOT NULL,
    file_name  TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    UNIQUE (doc, ordinal),
    UNIQUE (doc, frag_id)
);

CREATE TABLE text_blocks (
    doc    INTEGER NOT NULL REFERENCES documents(doc),
    block  INTEGER NOT NULL,
    n      INTEGER NOT NULL,
    data   BLOB NOT NULL,
    PRIMARY KEY (doc, block)
) WITHOUT ROWID;

CREATE TABLE cp_spans (
    doc  INTEGER NOT NULL,
    cp   INTEGER NOT NULL,
    idx  INTEGER NOT NULL,
    frag INTEGER NOT NULL,
    off  INTEGER NOT NULL,
    len  INTEGER NOT NULL,
    PRIMARY KEY (doc, cp, idx)
) WITHOUT ROWID;

CREATE TABLE rev (
    doc    INTEGER NOT NULL,
    frag   INTEGER NOT NULL,
    block  INTEGER NOT NULL,
    s      INTEGER NOT NULL,
    e      INTEGER NOT NULL,
    cp     INTEGER NOT NULL
);

CREATE INDEX rev_lookup ON rev (doc, frag, block, s);
CREATE INDEX rev_cp ON rev (doc, cp);
"""


class ArchiveError(Exception):
    """The file is not a usable archive."""


# ---------------------------------------------------------------------------
# text blocks
# ---------------------------------------------------------------------------

class TextBlockWriter:
    """Buffers normalized code points and writes fixed-size text blocks."""

    def __init__(self, db, doc, block_shift=TEXT_BLOCK_SHIFT):
        self.db = db
        self.doc = doc
        self.size = 1 << block_shift
        self.block = 0
        self.buffer = []
        self.pending = []
        self.count = 0
        self.sha256 = hashlib.sha256()

    def add(self, cp):
        self.buffer.append(chr(cp))
        self.count += 1
        if len(self.buffer) >= self.size:
            self._emit()

    def _emit(self):
        if not self.buffer:
            return
        text = "".join(self.buffer)
        data = text.encode("utf-8")
        self.sha256.update(data)
        self.pending.append((self.doc, self.block, len(self.buffer), data))
        self.block += 1
        self.buffer = []
        if len(self.pending) >= 128:
            self.flush()

    def flush(self):
        if self.buffer:
            self._emit()
        if self.pending:
            self.db.executemany(
                "INSERT INTO text_blocks (doc, block, n, data) VALUES (?, ?, ?, ?)",
                self.pending)
            self.pending = []


def read_text(db, doc, start, end, block_shift=TEXT_BLOCK_SHIFT):
    """NFC text of ``doc`` for code points ``[start, end)``."""
    if start < 0 or end < start:
        raise ValueError("invalid code point range")
    if start == end:
        return ""
    size = 1 << block_shift
    first = start >> block_shift
    last = (end - 1) >> block_shift
    parts = []
    rows = db.execute(
        "SELECT block, data FROM text_blocks WHERE doc = ? AND block BETWEEN ? AND ?"
        " ORDER BY block", (doc, first, last))
    for block, data in rows:
        base = block << block_shift
        text = data.decode("utf-8")
        lo = max(start - base, 0)
        hi = min(end - base, len(text))
        parts.append(text[lo:hi])
    return "".join(parts)


def iter_text(db, doc, chunk_blocks=128, block_shift=TEXT_BLOCK_SHIFT):
    """Yield the whole NFC text of ``doc`` in bounded pieces."""
    buffer = []
    last = None
    for block, data in db.execute(
            "SELECT block, data FROM text_blocks WHERE doc = ? ORDER BY block", (doc,)):
        if last is not None and block != last + 1:
            raise ArchiveError("text blocks are not contiguous for document %d" % doc)
        last = block
        buffer.append(data.decode("utf-8"))
        if len(buffer) >= chunk_blocks:
            yield "".join(buffer)
            buffer = []
    if buffer:
        yield "".join(buffer)


# ---------------------------------------------------------------------------
# opening
# ---------------------------------------------------------------------------

def connect(path, mode="ro"):
    """Open the database.

    ``mode``: ``"ro"`` (default), ``"rw"`` or ``"rwc"``.  Writable connections
    disable ``trusted_schema`` so a hostile archive cannot run code through
    views or triggers even if someone wrote one.
    """
    if mode not in ("ro", "rw", "rwc"):
        raise ValueError("mode must be 'ro', 'rw' or 'rwc'")
    absolute = os.path.abspath(path)
    if mode == "ro":
        if not os.path.exists(absolute):
            raise ArchiveError("archive does not exist: %s" % path)
        db = sqlite3.connect("file:%s?mode=ro" % _uri_path(absolute),
                             uri=True, check_same_thread=False)
    else:
        db = sqlite3.connect(absolute, check_same_thread=False)
        db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    try:
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        db.close()
        raise ArchiveError("cannot open archive %r: %s" % (path, exc)) from exc
    return db


def _uri_path(path):
    out = []
    for ch in path:
        if ch.isalnum() or ch in "/-._~":
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out)


def check_format(db):
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ArchiveError("not a textarchive file: %s" % exc) from exc
    if row is None:
        raise ArchiveError("not a textarchive file: missing format marker")
    if row[0] != ARCHIVE_FORMAT:
        raise ArchiveError("unsupported archive format %r" % row[0])


def meta_dict(db):
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}


def check_integrity(db):
    rows = db.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise ArchiveError("integrity check failed: %s" % (rows[:5],))


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

class ArchiveWriter:
    """Builds an archive at ``path`` and moves it into place on commit."""

    def __init__(self, path, settings):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path) or "."
        if not os.path.isdir(directory):
            raise ArchiveError("output directory does not exist: %s" % directory)
        # A unique, non-predictable temporary file in the destination directory
        # (same filesystem, so os.replace is atomic).  Building here means a
        # crash or a concurrent build never touches the destination.
        fd, self.tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(self.path) + ".building-", dir=directory)
        os.close(fd)
        os.unlink(self.tmp_path)  # sqlite creates it itself
        self.db = sqlite3.connect(self.tmp_path)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("PRAGMA cache_size=-16000")
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.db.executescript(SCHEMA)
        self.db.execute("BEGIN")
        self.set_meta("format", ARCHIVE_FORMAT)
        self.set_meta("normalization", "NFC")
        for key, value in settings.items():
            self.set_meta(key, str(value))

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))

    def set_tables(self, blob):
        self.db.execute("INSERT INTO unicode_tables (id, data) VALUES (1, ?)",
                        (sqlite3.Binary(blob),))

    def commit(self):
        self.db.commit()
        self.db.close()
        _fsync(self.tmp_path)
        os.replace(self.tmp_path, self.path)

    def abort(self):
        try:
            self.db.close()
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.unlink(self.tmp_path + suffix)
                except OSError:
                    pass


def _fsync(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            data = fp.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()
