"""Queries against an open archive.

Two directions:

``cp_provenance`` (normalized -> original)
    For a normalized code point range ``[start, end)`` of one document, return
    the exact byte spans (fragment id + zero-based offset + length) of the
    original material that formed each code point.  Only the ``cp_spans`` rows
    of that range and the text blocks covering it are read -- never the whole
    document.

``origin_hits`` (original -> normalized)
    For a fragment id and a zero-based byte range ``[start, end)``, return every
    normalized code point whose provenance intersects the range.  The reverse
    index is pre-split at block boundaries, so only the blocks intersected by
    the query are touched; the result is ordered by position in the normalized
    text, meaning it does not assume the fragment order equals the text order.

Provenance stores a fragment's *ordinal* within its document (0-based, in
manifest order).  Public results always report the fragment id.
"""

from .archive import read_text

REV_BLOCK_SHIFT = 10  # must match the writer; stored in meta as rev_block_size


def _find_doc(db, name):
    row = db.execute("SELECT doc, cp_count FROM documents WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError("no such document: %r" % (name,))
    return row[0], row[1]


def document_info(db, name):
    row = db.execute(
        "SELECT doc, name, fragment_count, byte_count, cp_count, sha256_bytes, sha256_text"
        " FROM documents WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError("no such document: %r" % (name,))
    return {
        "document": row[1],
        "fragment_count": row[2],
        "byte_count": row[3],
        "cp_count": row[4],
        "sha256_bytes": row[5],
        "sha256_text": row[6],
    }


def list_documents(db):
    return [
        {"document": name, "cp_count": cp_count, "byte_count": byte_count,
         "fragment_count": frags}
        for name, cp_count, byte_count, frags in db.execute(
            "SELECT name, cp_count, byte_count, fragment_count FROM documents ORDER BY doc")
    ]


def fragment_ids(db, doc):
    """``ordinal -> (frag_id, byte_count)`` for one document."""
    return {
        ordinal: (frag_id, size)
        for ordinal, frag_id, size in db.execute(
            "SELECT ordinal, frag_id, byte_count FROM fragments WHERE doc = ?", (doc,))
    }


def resolve_fragment(db, doc, frag_id):
    row = db.execute(
        "SELECT ordinal, byte_count FROM fragments WHERE doc = ? AND frag_id = ?",
        (doc, frag_id)).fetchone()
    if row is None:
        raise KeyError("no such fragment %r in document %r" % (frag_id, doc))
    return row[0], row[1]


# ---------------------------------------------------------------------------
# normalized -> original
# ---------------------------------------------------------------------------

def cp_provenance(db, document, start, end):
    """Exact provenance of normalized code points ``[start, end)`` of ``document``."""
    doc, cp_count = _find_doc(db, document)
    return cp_provenance_by_doc(db, doc, cp_count, start, end)


def cp_provenance_by_doc(db, doc, cp_count, start, end):
    if start < 0 or end < start or end > cp_count:
        raise ValueError("code point range [%d, %d) out of bounds (document has %d)"
                         % (start, end, cp_count))
    if start == end:
        return []
    names = fragment_ids(db, doc)
    rows = db.execute(
        "SELECT cp, frag, off, len FROM cp_spans WHERE doc = ? AND cp >= ? AND cp < ?"
        " ORDER BY cp, idx", (doc, start, end))
    result = []
    current = None
    for cp, frag, off, length in rows:
        if current is None or current["cp"] != cp:
            current = {"cp": cp, "char": None, "spans": []}
            result.append(current)
        current["spans"].append(_span(names, frag, off, length))
    text = read_text(db, doc, start, end)
    if len(result) != len(text):  # pragma: no cover - corrupt archive
        raise RuntimeError(
            "archive is inconsistent for document %d: text has %d code points in"
            " [%d, %d) but provenance has %d; run 'textarchive verify'"
            % (doc, len(text), start, end, len(result)))
    for item, ch in zip(result, text):
        item["char"] = ch
    return result


def _span(names, ordinal, off, length):
    frag_id, _size = names[ordinal]
    return {"fragment": frag_id, "ordinal": ordinal, "offset": off, "length": length}


# ---------------------------------------------------------------------------
# original -> normalized
# ---------------------------------------------------------------------------

def origin_hits(db, document, frag_id, start, end):
    """Normalized code points affected by bytes ``[start, end)`` of a fragment."""
    doc, cp_count = _find_doc(db, document)
    return origin_hits_by_doc(db, doc, cp_count, frag_id, start, end)


def origin_hits_by_doc(db, doc, cp_count, frag_id, start, end):
    ordinal, size = resolve_fragment(db, doc, frag_id)
    return origin_hits_by_ordinal(db, doc, cp_count, ordinal, start, end)


def origin_hits_by_ordinal(db, doc, cp_count, ordinal, start, end):
    row = db.execute("SELECT frag_id, byte_count FROM fragments WHERE doc = ? AND ordinal = ?",
                     (doc, ordinal)).fetchone()
    if row is None:
        name = db.execute("SELECT name FROM documents WHERE doc = ?", (doc,)).fetchone()
        where = name[0] if name else str(doc)
        raise KeyError("no such fragment ordinal %r in document %r" % (ordinal, where))
    _frag_id, size = row
    if start < 0 or end < start or end > size:
        raise ValueError("byte range [%d, %d) out of bounds (fragment has %d bytes)"
                         % (start, end, size))
    if start == end:
        return []
    block_size = _rev_block_size(db)
    first = start // block_size
    last = (end - 1) // block_size
    positions = set()
    for block in range(first, last + 1):
        base = block * block_size
        lo = max(start, base) - base
        hi = min(end, base + block_size) - base
        for (cp,) in db.execute(
                "SELECT DISTINCT cp FROM rev WHERE doc = ? AND frag = ? AND block = ?"
                " AND s < ? AND e > ?", (doc, ordinal, block, hi, lo)):
            positions.add(cp)
    if not positions:
        return []
    return _load_positions(db, doc, sorted(positions))


def _rev_block_size(db):
    row = db.execute("SELECT value FROM meta WHERE key = 'rev_block_size'").fetchone()
    return int(row[0]) if row else (1 << REV_BLOCK_SHIFT)


def _load_positions(db, doc, positions):
    """Load text and complete provenance for the given sorted positions."""
    names = fragment_ids(db, doc)
    text = _read_positions_text(db, doc, positions)
    result = [{"cp": pos, "char": ch, "spans": []}
              for pos, ch in zip(positions, text)]
    by_pos = {item["cp"]: item for item in result}
    for chunk_start in range(0, len(positions), 400):
        chunk = positions[chunk_start:chunk_start + 400]
        placeholders = ",".join("?" * len(chunk))
        for cp, frag, off, length in db.execute(
                "SELECT cp, frag, off, len FROM cp_spans WHERE doc = ? AND cp IN (%s)"
                " ORDER BY cp, idx" % placeholders, [doc] + chunk):
            by_pos[cp]["spans"].append(_span(names, frag, off, length))
    for item in result:
        if not item["spans"]:  # pragma: no cover - integrity guard
            raise RuntimeError("missing provenance for code point %d" % item["cp"])
    return result


def _read_positions_text(db, doc, positions):
    """Read characters at scattered positions, coalescing contiguous runs.

    ``read_text`` reads whole text blocks, so the work is proportional to the
    number of touched blocks, never to the document size.
    """
    out = []
    run_start = positions[0]
    run_end = run_start + 1
    for pos in positions[1:] + [None]:
        if pos is not None and pos == run_end:
            run_end += 1
            continue
        out.extend(read_text(db, doc, run_start, run_end))
        if pos is not None:
            run_start = pos
            run_end = pos + 1
    return out
