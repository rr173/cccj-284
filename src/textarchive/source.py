"""Byte-stream access over an ordered list of fragment files.

A document is the concatenation of its fragments' byte files, with no separator.
This module exposes that concatenation as one byte stream that can be walked in
bounded chunks, while keeping track of which fragment (and offset inside it) each
byte came from.
"""

import os

DEFAULT_CHUNK = 1 << 16


class FragmentError(Exception):
    """Raised when a fragment cannot be read."""


class FragmentSource:
    def __init__(self, fragments, chunk_size=DEFAULT_CHUNK):
        """``fragments``: sequence of ``(fragment_id, path)`` in document order."""
        self.fragments = list(fragments)
        self.chunk_size = max(1, int(chunk_size))
        self._sizes = []
        self._offsets = []  # start offset of each fragment in the concatenation
        total = 0
        for _fid, path in self.fragments:
            size = _file_size(path)
            self._sizes.append(size)
            self._offsets.append(total)
            total += size
        self.total = total

    # -- position mapping ---------------------------------------------------
    def locate(self, absolute):
        """Absolute byte offset -> ``(fragment_ordinal, offset_in_fragment)``."""
        if absolute < 0 or absolute > self.total:
            raise IndexError("byte offset %d out of range" % absolute)
        lo, hi = 0, len(self._offsets) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._offsets[mid] <= absolute:
                lo = mid
            else:
                hi = mid - 1
        return lo, absolute - self._offsets[lo]

    def fragment_size(self, ordinal):
        return self._sizes[ordinal]

    def fragment_id(self, ordinal):
        return self.fragments[ordinal][0]

    def offset_of(self, ordinal):
        return self._offsets[ordinal]

    # -- iteration ----------------------------------------------------------
    def iter_chunks(self):
        """Yield ``(fragment_ordinal, offset_in_fragment, bytes)`` tuples."""
        for ordinal, (_fid, path) in enumerate(self.fragments):
            try:
                fp = open(path, "rb")
            except OSError as exc:
                raise FragmentError("cannot open fragment %r: %s" % (path, exc)) from exc
            with fp:
                offset = 0
                while True:
                    data = fp.read(self.chunk_size)
                    if not data:
                        break
                    yield ordinal, offset, data
                    offset += len(data)


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError as exc:
        raise FragmentError("cannot stat fragment %r: %s" % (path, exc)) from exc
