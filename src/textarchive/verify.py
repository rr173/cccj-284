"""End-to-end verification of an archive.

``verify_archive`` re-reads the stored NFC text and provenance without the
original fragment files and checks four properties:

1. the stored text is valid UTF-8 and is already in NFC, computed with the
   Unicode tables stored in the archive rather than the running interpreter's;
2. every code point has at least one provenance span and every span lies inside
   the declared size of its fragment;
3. the reverse index agrees with the forward provenance on a deterministic
   sample of fragment byte ranges (whole fragment, single bytes, and random
   ranges);
4. the code point count and the SHA-256 of the text match the header.

A reopened archive is self-contained: nothing here reads the original files.
"""

import hashlib
import random

from .archive import check_integrity, iter_text
from .nfc import Normalizer
from .query import cp_provenance_by_doc, fragment_ids, origin_hits_by_ordinal
from .store import Archive

VERIFY_CHUNK = 4096


def _normalized_digest(db, doc, tables):
    """SHA-256 of the archive text re-normalized with the archive's tables."""
    hasher = hashlib.sha256()
    buffer = []
    with Normalizer(tables, working_memory=256 << 10) as norm:
        for chunk in iter_text(db, doc, chunk_blocks=VERIFY_CHUNK):
            for ch in chunk:
                for cp, _spans in norm.feed(ord(ch), ()):
                    buffer.append(chr(cp))
                    if len(buffer) >= 8192:
                        hasher.update("".join(buffer).encode("utf-8"))
                        buffer = []
        for cp, _spans in norm.finish():
            buffer.append(chr(cp))
            if len(buffer) >= 8192:
                hasher.update("".join(buffer).encode("utf-8"))
                buffer = []
    if buffer:
        hasher.update("".join(buffer).encode("utf-8"))
    return hasher.hexdigest()


def _stored_digest(db, doc):
    hasher = hashlib.sha256()
    count = 0
    for chunk in iter_text(db, doc, chunk_blocks=VERIFY_CHUNK):
        hasher.update(chunk.encode("utf-8"))
        count += len(chunk)
    return hasher.hexdigest(), count


def _verify_text(db, doc, cp_count, sha_text, tables):
    problems = []
    stored_digest, count = _stored_digest(db, doc)
    if count != cp_count:
        problems.append("code point count mismatch: header %d, text %d" % (cp_count, count))
    if stored_digest != sha_text:
        problems.append("text digest mismatch")
    # Independent of the header: the stored text must be its own NFC.
    if _normalized_digest(db, doc, tables) != stored_digest:
        problems.append("stored text is not NFC (per the archive's Unicode tables)")
    return problems


def _verify_document(archive, doc_row, sample_limit, rng):
    db = archive.db
    doc, name, cp_count, sha_text = doc_row
    problems = _verify_text(db, doc, cp_count, sha_text, archive.tables)

    # -- provenance and reverse index ---------------------------------------
    # One pass over provenance is also the oracle for sampled reverse checks,
    # so a large document is not re-scanned per sample.
    names = fragment_ids(db, doc)
    by_ordinal, empty, bad_span = _provenance_map(db, doc, cp_count, names)
    if empty:
        problems.append("%d code points without provenance" % empty)
    if bad_span:
        problems.append("%d provenance spans outside their fragment" % bad_span)
    mismatches = _check_reverse(db, doc, cp_count, names, by_ordinal, sample_limit, rng)
    if mismatches:
        problems.append("%d reverse-index mismatches" % mismatches)

    return {
        "document": name,
        "cp_count": cp_count,
        "ok": not problems,
        "problems": problems,
    }


def _provenance_map(db, doc, cp_count, names):
    """``ordinal -> {half-open byte interval: set of code points}``; counts gaps.

    Also validates that every span lies inside its fragment.
    """
    by_ordinal = {ordinal: [] for ordinal in names}
    bad_span = 0
    empty = 0
    for item in cp_provenance_by_doc(db, doc, cp_count, 0, cp_count):
        spans = item["spans"]
        if not spans:
            empty += 1
        for span in spans:
            size = names[span["ordinal"]][1]
            if span["offset"] < 0 or span["length"] <= 0 or span["offset"] + span["length"] > size:
                bad_span += 1
            else:
                by_ordinal[span["ordinal"]].append(
                    (span["offset"], span["offset"] + span["length"], item["cp"]))
    return by_ordinal, empty, bad_span


def _check_reverse(db, doc, cp_count, names, by_ordinal, sample_limit, rng):
    """Compare ``origin_hits`` with the provenance map on sampled ranges."""
    mismatches = 0
    samples = []
    ordinals = sorted(names)
    if not ordinals:
        return 0
    for ordinal in ordinals:
        size = names[ordinal][1]
        if size == 0:
            continue
        samples.append((ordinal, 0, size))
        samples.append((ordinal, 0, 1))
        samples.append((ordinal, max(0, size - 1), size))
    for _ in range(sample_limit):
        ordinal = ordinals[rng.randrange(len(ordinals))]
        size = names[ordinal][1]
        if size == 0:
            continue
        start = rng.randrange(size)
        end = rng.randrange(start + 1, size + 1)
        samples.append((ordinal, start, end))
    for ordinal, start, end in samples:
        got = {item["cp"] for item in origin_hits_by_ordinal(
            db, doc, cp_count, ordinal, start, end)}
        want = {cp for lo, hi, cp in by_ordinal.get(ordinal, ())
                if lo < end and hi > start}
        if got != want:
            mismatches += 1
    return mismatches


def verify_archive(path, sample_limit=200, jobs=1, seed=0):
    with Archive(path) as archive:
        check_integrity(archive.db)
        rng = random.Random(seed)
        rows = archive.db.execute(
            "SELECT doc, name, cp_count, sha256_text FROM documents ORDER BY doc").fetchall()
        reports = [_verify_document(archive, row, sample_limit, rng) for row in rows]
        return {
            "archive": path,
            "unicode_version": archive.unicode_version,
            "documents": len(reports),
            "ok": all(r["ok"] for r in reports),
            "document_reports": reports,
        }
