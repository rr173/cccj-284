"""Provenance span helpers.

A *span* is ``(fragment_ordinal, offset_in_fragment, length)``: a half-open
byte range inside one fragment.  A normalized code point carries a tuple of
spans -- the exact original bytes it was built from.  Spans of the same
fragment are kept sorted by offset and merged only when they touch or overlap,
so a span never includes a byte that did not contribute.
"""


def merge_spans(spans):
    """Sort, then merge touching/overlapping spans of the same fragment."""
    ordered = sorted(spans)
    out = []
    for frag, off, length in ordered:
        if length <= 0:
            raise ValueError("empty provenance span")
        if out and out[-1][0] == frag and off <= out[-1][1] + out[-1][2]:
            pf, po, pl = out[-1]
            end = max(po + pl, off + length)
            out[-1] = (pf, po, end - po)
        else:
            out.append((frag, off, length))
    return tuple(out)


def union_spans(*groups):
    """Merge several span tuples into one exact span tuple."""
    merged = []
    for group in groups:
        merged.extend(group)
    return merge_spans(merged)


def clip_span(frag, off, length, start, end):
    """Return the part of ``(frag, off, length)`` inside ``[start, end)``."""
    lo = max(off, start)
    hi = min(off + length, end)
    if lo >= hi:
        return ()
    return ((frag, lo, hi - lo),)
