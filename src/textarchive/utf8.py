"""Strict UTF-8 decoding with per-code-point byte provenance.

The decoder walks the fragment byte stream produced by
:class:`textarchive.source.FragmentSource`.  For every decoded code point it
returns the exact set of source byte ranges that formed it -- a UTF-8 sequence
may be split across fragment boundaries, so one code point can have several
spans, each ``(fragment_ordinal, offset_in_fragment, length)``.

Invalid UTF-8 is never replaced with U+FFFD.  It raises :class:`Utf8Error`
carrying the document, fragment id and in-fragment byte offset of the first
offending byte, so the problem can be found in the original material.

The decoder is incremental: it keeps at most three undecoded bytes (plus the
fragment segments those bytes belong to) between chunks, so neither fragment
size nor document size affects memory use.
"""

from .spans import merge_spans

#: a single code point is at most four bytes, hence at most four spans
MAX_SPANS = 8


class Utf8Error(Exception):
    """An invalid UTF-8 sequence, located in the original fragments."""

    def __init__(self, document, fragment_id, fragment_offset, absolute_offset,
                 reason, byte_value=None, context=b""):
        self.document = document
        self.fragment_id = fragment_id
        self.fragment_offset = fragment_offset
        self.absolute_offset = absolute_offset
        self.reason = reason
        self.byte_value = byte_value
        self.context = context
        super().__init__(self.message)

    @property
    def message(self):
        where = "document %r, fragment %r, byte offset %d" % (
            self.document, self.fragment_id, self.fragment_offset)
        if self.byte_value is None:
            return "invalid UTF-8 in %s: %s" % (where, self.reason)
        return "invalid UTF-8 in %s: %s (byte 0x%02X)" % (
            where, self.reason, self.byte_value)

    def to_dict(self):
        return {
            "error": "invalid-utf8",
            "document": self.document,
            "fragment": self.fragment_id,
            "fragment_offset": self.fragment_offset,
            "absolute_offset": self.absolute_offset,
            "reason": self.reason,
            "byte": None if self.byte_value is None else "0x%02X" % self.byte_value,
            "context_hex": self.context.hex(),
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# UTF-8 validation
# ---------------------------------------------------------------------------

def _sequence_info(lead):
    """``(length, min_second, max_second)`` for a lead byte, or ``None``.

    The ranges implement all UTF-8 well-formedness constraints: no overlong
    forms, no UTF-16 surrogates and nothing above U+10FFFF.
    """
    if 0xC2 <= lead <= 0xDF:
        return 2, 0x80, 0xBF
    if lead == 0xE0:
        return 3, 0xA0, 0xBF
    if 0xE1 <= lead <= 0xEC:
        return 3, 0x80, 0xBF
    if lead == 0xED:
        return 3, 0x80, 0x9F
    if 0xEE <= lead <= 0xEF:
        return 3, 0x80, 0xBF
    if lead == 0xF0:
        return 4, 0x90, 0xBF
    if 0xF1 <= lead <= 0xF3:
        return 4, 0x80, 0xBF
    if lead == 0xF4:
        return 4, 0x80, 0x8F
    return None


def find_error(buf):
    """Find the first ill-formed byte in ``buf``.

    Returns ``(index, reason, byte_value)`` or ``None``.  ``reason`` is
    ``"truncated sequence"`` when the only problem is an incomplete sequence
    at the very end of the buffer; ``index`` then points at its first byte.
    """
    n = len(buf)
    i = 0
    while i < n:
        lead = buf[i]
        if lead < 0x80:
            i += 1
            continue
        info = _sequence_info(lead)
        if info is None:
            return i, "invalid start byte", lead
        length, lo, hi = info
        if i + 1 >= n:
            return i, "truncated sequence", lead
        second = buf[i + 1]
        if not (lo <= second <= hi):
            return i + 1, "invalid continuation byte", second
        for k in range(2, length):
            if i + k >= n:
                return i, "truncated sequence", lead
            cont = buf[i + k]
            if not (0x80 <= cont <= 0xBF):
                return i + k, "invalid continuation byte", cont
        i += length
    return None


def char_length(buf, index):
    lead = buf[index]
    if lead < 0x80:
        return 1
    if lead < 0xE0:
        return 2
    if lead < 0xF0:
        return 3
    return 4


def clip_segments(segments, start, end):
    """Restrict ``(buf_start, frag, frag_off, length)`` segments to ``[start, end)``."""
    out = []
    for bstart, frag, foff, length in segments:
        bend = bstart + length
        lo = max(start, bstart)
        hi = min(end, bend)
        if lo < hi:
            out.append((lo, frag, foff + (lo - bstart), hi - lo))
    return out


def spans_of(segments, start, end):
    """Exact provenance spans for the buffer range ``[start, end)``."""
    return merge_spans([(frag, foff, length)
                        for _b, frag, foff, length in clip_segments(segments, start, end)])


class Utf8Decoder:
    """Incremental strict UTF-8 decoder that tracks byte provenance."""

    def __init__(self, document, source):
        self.document = document
        self.source = source
        self._pending = b""
        self._segments = []  # segments covering self._pending

    # -- public API ---------------------------------------------------------
    def feed(self, ordinal, base_offset, data):
        """Feed one chunk of one fragment; yields ``(codepoint, spans)``."""
        if self._pending:
            buffer = self._pending + data
            segments = list(self._segments)
            if data:
                segments.append((len(self._pending), ordinal, base_offset, len(data)))
        else:
            if not data:
                return
            buffer = data
            segments = [(0, ordinal, base_offset, len(data))]
        found = find_error(buffer)
        if found is not None and found[1] != "truncated sequence":
            self._raise(buffer, segments, *found)
        cut = found[0] if found is not None else len(buffer)

        index = 0
        for ch in buffer[:cut].decode("utf-8"):
            length = char_length(buffer, index)
            spans = spans_of(segments, index, index + length)
            if len(spans) > MAX_SPANS:  # pragma: no cover - impossible for UTF-8
                raise ValueError("code point split across too many fragments")
            yield ord(ch), spans
            index += length

        if cut < len(buffer):
            self._pending = buffer[cut:]
            # Keep the segments' buffer offsets relative to the pending bytes,
            # which start at offset 0 of the *next* buffer.
            self._segments = [(b - cut, frag, foff, length)
                              for b, frag, foff, length
                              in clip_segments(segments, cut, len(buffer))]
        else:
            self._pending = b""
            self._segments = []

    def finish(self):
        """Signal end of document; raises for a truncated final sequence."""
        if not self._pending:
            return
        found = find_error(self._pending)
        if found is None:  # pragma: no cover - pending is always truncated
            found = (0, "truncated sequence", self._pending[0])
        self._raise(self._pending, self._segments, *found)

    # -- internals ----------------------------------------------------------
    def _raise(self, buffer, segments, index, reason, value):
        located = clip_segments(segments, index, index + 1)
        if located:
            _b, ordinal, foff, _length = located[0]
        else:  # pragma: no cover - defensive
            ordinal, foff = 0, 0
        fragment_id = self.source.fragment_id(ordinal)
        absolute = self.source.offset_of(ordinal) + foff
        context = buffer[max(0, index - 4):index + 4]
        raise Utf8Error(
            self.document,
            fragment_id,
            foff,
            absolute,
            reason,
            value,
            context,
        )
