"""Bounded-memory buffers that spill to disk.

Two structures are needed by the normalizer:

``RunBuffer``
    Collects the decomposed, not-yet-canonically-ordered non-starter run of a
    combining sequence.  Canonical ordering is a *stable* sort by combining
    class, so the structure must preserve arrival order among equal classes.
    If the run outgrows the configured working memory it is written to
    per-combining-class bucket files on disk; iteration then reads buckets in
    class order, which is exactly the stable sort and is O(run size) I/O.

``SpillQueue``
    A plain FIFO used for normalized marks that must be emitted after their
    starter.  Memory is bounded; overflow is appended to a spool file.

Records are ``(codepoint, combining_class, spans)`` where ``spans`` is a
sequence of ``(fragment_ordinal, byte_offset, byte_length)`` triples: the
original bytes that contributed to the codepoint.  Both structures only ever
hold a bounded number of records in memory, regardless of document size.
"""

import os
import shutil
import struct
import tempfile
from collections import OrderedDict

_REC = struct.Struct("<IBB")
_SPAN = struct.Struct("<IqI")
MAX_SPANS = 255  # a canonical decomposition has <= 4 components, each from <= 4 fragments


def encode_record(cp, ccc, spans):
    n = len(spans)
    if n > MAX_SPANS:
        raise ValueError("too many provenance spans for one code point")
    parts = [_REC.pack(cp, ccc, n)]
    for fid, start, length in spans:
        parts.append(_SPAN.pack(fid, start, length))
    return b"".join(parts)


def read_records(fp):
    """Yield decoded records from a binary spool file positioned at 0."""
    fp.seek(0)
    head = _REC.size
    span = _SPAN.size
    while True:
        blob = fp.read(head)
        if not blob:
            return
        if len(blob) != head:
            raise ValueError("truncated spill record")
        cp, ccc, n = _REC.unpack(blob)
        raw = fp.read(n * span)
        if len(raw) != n * span:
            raise ValueError("truncated spill record")
        spans = []
        for i in range(n):
            spans.append(_SPAN.unpack_from(raw, i * span))
        yield cp, ccc, tuple(spans)


class _SpillSession:
    """Holds temporary files for one build; removed on close."""

    def __init__(self, directory=None):
        self.directory = directory
        self.root = None
        self._counter = 0

    def path(self, prefix):
        if self.root is None:
            self.root = tempfile.mkdtemp(prefix="textarchive-spill-", dir=self.directory)
        self._counter += 1
        return os.path.join(self.root, "%s-%d.tmp" % (prefix, self._counter))

    def close(self):
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None


class _HandleCache:
    """Keeps a bounded number of append handles open."""

    def __init__(self, limit=32):
        self.limit = limit
        self._handles = OrderedDict()

    def get(self, path):
        fp = self._handles.get(path)
        if fp is not None:
            self._handles.move_to_end(path)
            return fp
        fp = open(path, "ab")
        self._handles[path] = fp
        if len(self._handles) > self.limit:
            _, old = self._handles.popitem(last=False)
            old.close()
        return fp

    def close(self):
        for fp in self._handles.values():
            fp.close()
        self._handles.clear()


class RunBuffer:
    """Stable canonical-order buffer for one combining run.

    ``capacity_bytes`` bounds the approximate in-memory footprint.  When it is
    exceeded the buffer switches to disk buckets and memory use drops to O(1).
    """

    def __init__(self, session, capacity_bytes):
        self._session = session
        self.capacity = max(64, int(capacity_bytes))
        self._memory = []
        self._bytes = 0
        self._buckets = None          # ccc -> path
        self._handles = None
        self._records = 0

    # -- writing ------------------------------------------------------------
    def add(self, cp, ccc, spans):
        self._records += 1
        if self._buckets is None:
            record = (cp, ccc, spans)
            self._memory.append(record)
            self._bytes += 8 + 16 * len(spans)
            if self._bytes > self.capacity:
                self._switch_to_disk()
            return
        self._append_to_bucket(cp, ccc, spans)

    def _switch_to_disk(self):
        self._buckets = {}
        self._handles = _HandleCache()
        for cp, ccc, spans in self._memory:
            self._append_to_bucket(cp, ccc, spans)
        self._memory = []
        self._bytes = 0

    def _append_to_bucket(self, cp, ccc, spans):
        path = self._buckets.get(ccc)
        if path is None:
            path = self._session.path("run-ccc-%d" % ccc)
            self._buckets[ccc] = path
        self._handles.get(path).write(encode_record(cp, ccc, spans))

    # -- reading ------------------------------------------------------------
    def iter_sorted(self):
        """Yield records in stable canonical order (ascending combining class)."""
        if self._buckets is None:
            for record in sorted(self._memory, key=lambda r: r[1]):
                yield record
            return
        if self._handles is not None:
            self._handles.close()
            self._handles = None
        for ccc in sorted(self._buckets):
            path = self._buckets[ccc]
            with open(path, "rb") as fp:
                for record in read_records(fp):
                    yield record

    # -- lifecycle ----------------------------------------------------------
    def __len__(self):
        return self._records

    def reset(self):
        self._memory = []
        self._bytes = 0
        self._records = 0
        if self._handles is not None:
            self._handles.close()
            self._handles = None
        if self._buckets is not None:
            for path in self._buckets.values():
                try:
                    os.unlink(path)
                except OSError:
                    pass
            self._buckets = None


class SpillQueue:
    """FIFO that spills to a scratch file when memory is exceeded."""

    def __init__(self, session, capacity_bytes):
        self._session = session
        self.capacity = max(64, int(capacity_bytes))
        self._memory = []
        self._bytes = 0
        self._path = None
        self._fp = None

    def add(self, cp, spans):
        self._memory.append((cp, spans))
        self._bytes += 8 + 16 * len(spans)
        if self._bytes > self.capacity:
            self._flush_to_file()

    def _flush_to_file(self):
        if self._path is None:
            self._path = self._session.path("queue")
        if self._fp is None:
            self._fp = open(self._path, "ab")
        for cp, spans in self._memory:
            self._fp.write(encode_record(cp, 0, spans))
        self._fp.flush()
        self._memory = []
        self._bytes = 0

    def __iter__(self):
        if self._path is not None:
            if self._fp is not None:
                self._fp.flush()
            with open(self._path, "rb") as fp:
                for cp, _ccc, spans in read_records(fp):
                    yield cp, spans
        for cp, spans in self._memory:
            yield cp, spans

    def reset(self):
        self._memory = []
        self._bytes = 0
        if self._fp is not None:
            self._fp.close()
            self._fp = None
        if self._path is not None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self._path = None


class SpoolContext:
    """Convenience wrapper owning a spill session and closing it later."""

    def __init__(self, directory=None):
        self.session = _SpillSession(directory)

    def run_buffer(self, capacity_bytes):
        return RunBuffer(self.session, capacity_bytes)

    def queue(self, capacity_bytes):
        return SpillQueue(self.session, capacity_bytes)

    def close(self):
        self.session.close()
