"""Independent oracle for provenance, used only by the tests.

It implements the rules from the task literally and slowly: it tracks the
absolute byte range of every original character, decomposes each character,
reorders with byte sets attached, and composes while unioning byte sets.  It is
then compared with the archive, which must agree exactly.
"""

import unicodedata


def _compose(a, b):
    merged = unicodedata.normalize("NFC", a + b)
    return merged if len(merged) == 1 and merged != a + b else None


def decompose_with_bytes(text):
    """Return list of ``(char, frozenset[absolute byte index])`` after NFD+reorder."""
    seq = []
    pos = 0
    for ch in text:
        width = len(ch.encode("utf-8"))
        span = frozenset(range(pos, pos + width))
        pos += width
        for dec in unicodedata.normalize("NFD", ch):
            seq.append((dec, span))
    # stable canonical ordering
    i = 1
    while i < len(seq):
        j = i
        while (j > 0 and unicodedata.combining(seq[j][0]) != 0
               and unicodedata.combining(seq[j - 1][0]) > unicodedata.combining(seq[j][0])):
            seq[j - 1], seq[j] = seq[j], seq[j - 1]
            j -= 1
        i += 1
    return seq


def ground_truth(text):
    """NFC characters with the exact original byte set of each one."""
    seq = decompose_with_bytes(text)
    out = []
    starter = None
    barrier = 0
    marks = []

    def end():
        nonlocal starter, barrier, marks
        if starter is not None:
            out.append(starter)
            out.extend(marks)
        starter = None
        barrier = 0
        marks = []

    for ch, span in seq:
        cc = unicodedata.combining(ch)
        if cc == 0:
            if starter is not None:
                merged = _compose(starter[0], ch) if barrier == 0 else None
                if merged is not None:
                    starter = (merged, starter[1] | span)
                    continue
                end()
            starter = (ch, span)
            continue
        if starter is None:
            out.append((ch, span))
            continue
        if not (barrier != 0 and barrier >= cc):
            merged = _compose(starter[0], ch)
            if merged is not None:
                starter = (merged, starter[1] | span)
                continue
        marks.append((ch, span))
        barrier = cc
    end()
    result = "".join(ch for ch, _ in out)
    assert result == unicodedata.normalize("NFC", text)
    return out


def byte_set_to_spans(byte_set, fragments):
    """Convert absolute byte indexes to maximal ``(frag_id, offset, length)`` runs.

    ``fragments``: list of ``(frag_id, absolute_start, absolute_end)``.
    """
    spans = set()
    for frag_id, start, end in fragments:
        indexes = sorted(b for b in byte_set if start <= b < end)
        if not indexes:
            continue
        run = [indexes[0]]
        for b in indexes[1:]:
            if b == run[-1] + 1:
                run.append(b)
            else:
                spans.add((frag_id, run[0] - start, len(run)))
                run = [b]
        spans.add((frag_id, run[0] - start, len(run)))
    return frozenset(spans)
