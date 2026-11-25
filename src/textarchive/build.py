"""Building an archive from a manifest.

Manifest (JSON)::

    {
      "documents": [
        {
          "name": "chapter-1",
          "fragments": [
            {"id": "c1-a", "path": "data/c1-a.bin"},
            {"id": "c1-b", "path": "data/c1-b.bin"}
          ]
        }
      ]
    }

Fragments are concatenated in the given order, with no separator; the
concatenation is the document's UTF-8 text.  Relative paths are resolved
against the manifest's directory.

A tiny line-based format is accepted too::

    doc chapter-1
    c1-a data/c1-a.bin
    c1-b data/c1-b.bin

The build is all-or-nothing.  Every document is decoded, normalized and
indexed in full before anything is committed; invalid UTF-8, a missing
fragment or an I/O error aborts the whole build and removes the temporary
database, so no archive with text but no provenance (or vice versa) can exist.
"""

import hashlib
import json
import os

from .archive import (
    ArchiveWriter, REV_BLOCK_SHIFT, REV_BLOCK_SIZE, TEXT_BLOCK_SIZE, TextBlockWriter,
)
from .nfc import DEFAULT_WORKING_MEMORY, Normalizer
from .source import FragmentSource
from .spans import merge_spans
from .unicode_data import UnicodeTables, builtin_tables
from .utf8 import Utf8Decoder

DEFAULT_CHUNK = 1 << 16


class ManifestError(Exception):
    """The manifest is missing, unreadable or malformed."""


class BuildError(Exception):
    """The input cannot be archived."""


class Fragment:
    __slots__ = ("id", "path", "ordinal", "size", "digest", "file_name")

    def __init__(self, frag_id, path, ordinal):
        self.id = frag_id
        self.path = path
        self.ordinal = ordinal
        self.size = 0
        self.digest = None
        self.file_name = ""


class Document:
    __slots__ = ("name", "fragments")

    def __init__(self, name, fragments):
        self.name = name
        self.fragments = fragments


# ---------------------------------------------------------------------------
# manifest loading
# ---------------------------------------------------------------------------

def load_manifest(path):
    """Load a manifest, resolve fragment paths, return ``[Document]``."""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            raw = fp.read()
    except OSError as exc:
        raise ManifestError("cannot read manifest %r: %s" % (path, exc)) from exc
    base = os.path.dirname(os.path.abspath(path))
    text = raw.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ManifestError("manifest is not valid JSON: %s" % exc) from exc
        return _documents_from_json(data, base)
    return _documents_from_lines(raw, base)


def _documents_from_json(data, base):
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        raise ManifestError("manifest must be an object with a 'documents' list")
    docs = []
    names = set()
    for index, item in enumerate(data["documents"]):
        if not isinstance(item, dict):
            raise ManifestError("document %d is not an object" % index)
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ManifestError("document %d has no name" % index)
        if name in names:
            raise ManifestError("duplicate document name %r" % name)
        names.add(name)
        fragments = item.get("fragments")
        if not isinstance(fragments, list) or not fragments:
            raise ManifestError("document %r has no fragments" % name)
        frags = []
        seen = set()
        for ordinal, frag in enumerate(fragments):
            if not isinstance(frag, dict):
                raise ManifestError("document %r fragment %d is not an object" % (name, ordinal))
            frag_id = frag.get("id")
            frag_path = frag.get("path")
            if not isinstance(frag_id, str) or not frag_id:
                raise ManifestError("document %r fragment %d has no id" % (name, ordinal))
            if frag_id in seen:
                raise ManifestError("document %r repeats fragment id %r" % (name, frag_id))
            seen.add(frag_id)
            if not isinstance(frag_path, str) or not frag_path:
                raise ManifestError("document %r fragment %r has no path" % (name, frag_id))
            frags.append(Fragment(frag_id, _resolve(base, frag_path), ordinal))
        docs.append(Document(name, frags))
    if not docs:
        raise ManifestError("manifest contains no documents")
    return docs


def _documents_from_lines(raw, base):
    docs = []
    current = None
    names = set()
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("doc "):
            name = line[4:].strip()
            if not name:
                raise ManifestError("line %d: empty document name" % lineno)
            if name in names:
                raise ManifestError("line %d: duplicate document %r" % (lineno, name))
            names.add(name)
            current = Document(name, [])
            docs.append(current)
            continue
        if current is None:
            raise ManifestError("line %d: fragment before any 'doc' line" % lineno)
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ManifestError("line %d: expected '<id> <path>'" % lineno)
        frag_id, frag_path = parts[0], parts[1].strip()
        if any(f.id == frag_id for f in current.fragments):
            raise ManifestError("line %d: duplicate fragment id %r" % (lineno, frag_id))
        current.fragments.append(
            Fragment(frag_id, _resolve(base, frag_path), len(current.fragments)))
    if not docs:
        raise ManifestError("manifest contains no documents")
    for doc in docs:
        if not doc.fragments:
            raise ManifestError("document %r has no fragments" % doc.name)
    return docs


def _resolve(base, path):
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

class BuildStats:
    def __init__(self):
        self.documents = 0
        self.fragments = 0
        self.bytes = 0
        self.code_points = 0
        self.spans = 0
        self.rev_rows = 0

    def to_dict(self):
        return {
            "documents": self.documents,
            "fragments": self.fragments,
            "source_bytes": self.bytes,
            "nfc_code_points": self.code_points,
            "provenance_spans": self.spans,
            "reverse_index_rows": self.rev_rows,
        }


def build_archive(manifest_path, out_path, tables=None, working_memory=DEFAULT_WORKING_MEMORY,
                  spill_directory=None, chunk_size=DEFAULT_CHUNK, jobs=1):
    """Build ``out_path`` from ``manifest_path``.  Raises on any invalid input."""
    documents = load_manifest(manifest_path)
    if tables is None:
        tables = builtin_tables()
    if isinstance(tables, bytes):
        tables = UnicodeTables.from_blob(tables)
    writer = ArchiveWriter(out_path, {
        "unicode_version": tables.version,
        "normalizer": "textarchive-nfc",
        "working_memory": int(working_memory),
        "text_block_size": TEXT_BLOCK_SIZE,
        "rev_block_size": REV_BLOCK_SIZE,
    })
    writer.set_tables(tables.to_blob())
    stats = BuildStats()
    try:
        for index, document in enumerate(documents):
            _build_document(writer.db, index, document, tables, working_memory,
                            spill_directory, chunk_size, stats)
        writer.set_meta("document_count", str(len(documents)))
        writer.commit()
    except BaseException:
        writer.abort()
        raise
    stats.documents = len(documents)
    return stats


def _build_document(db, doc_index, document, tables, working_memory,
                    spill_directory, chunk_size, stats):
    fragments = document.fragments
    source = FragmentSource([(f.id, f.path) for f in fragments], chunk_size)

    # The original bytes are hashed separately from decoding; a failure to
    # read any fragment aborts the build (FragmentSource raises FragmentError).
    byte_hasher = hashlib.sha256()
    total_bytes = 0
    frag_rows = []
    for ordinal, frag in enumerate(fragments):
        frag.file_name = os.path.basename(frag.path)
        frag.size = source.fragment_size(ordinal)
        digest = hashlib.sha256()
        try:
            with open(frag.path, "rb") as fp:
                while True:
                    data = fp.read(1 << 20)
                    if not data:
                        break
                    digest.update(data)
                    byte_hasher.update(data)
        except OSError as exc:
            raise BuildError("cannot read fragment %r: %s" % (frag.path, exc)) from exc
        frag.digest = digest.hexdigest()
        total_bytes += frag.size
        frag_rows.append((doc_index, ordinal, frag.id, frag.file_name, frag.size, frag.digest))

    db.executemany(
        "INSERT INTO fragments (doc, ordinal, frag_id, file_name, byte_count, sha256)"
        " VALUES (?, ?, ?, ?, ?, ?)", frag_rows)

    text = TextBlockWriter(db, doc_index)
    cp_spans = []
    rev_rows = []
    rev_row_total = 0
    span_count = 0
    cp_index = 0

    def emit(ncp, nspans):
        nonlocal cp_index, span_count, rev_row_total
        text.add(ncp)
        spans = merge_spans(nspans)
        if not spans:
            raise BuildError("internal error: normalized code point without provenance")
        span_count += len(spans)
        for idx, (frag, off, length) in enumerate(spans):
            cp_spans.append((doc_index, cp_index, idx, frag, off, length))
            rev_row_total += _append_rev(rev_rows, doc_index, frag, off, length, cp_index,
                                         REV_BLOCK_SHIFT)
        cp_index += 1
        if len(cp_spans) >= 40000:
            flush_cp_spans()
        if len(rev_rows) >= 40000:
            flush_rev()

    def flush_cp_spans():
        if cp_spans:
            db.executemany(
                "INSERT INTO cp_spans (doc, cp, idx, frag, off, len) VALUES (?, ?, ?, ?, ?, ?)",
                cp_spans)
            del cp_spans[:]

    def flush_rev():
        if rev_rows:
            db.executemany(
                "INSERT INTO rev (doc, frag, block, s, e, cp) VALUES (?, ?, ?, ?, ?, ?)",
                rev_rows)
            del rev_rows[:]

    decoder = Utf8Decoder(document.name, source)
    with Normalizer(tables, working_memory, spill_directory) as norm:
        for ordinal, offset, data in source.iter_chunks():
            for cp, spans in decoder.feed(ordinal, offset, data):
                for ncp, nspans in norm.feed(cp, spans):
                    emit(ncp, nspans)
        decoder.finish()
        for ncp, nspans in norm.finish():
            emit(ncp, nspans)
    flush_cp_spans()
    flush_rev()
    text.flush()

    db.execute(
        "INSERT INTO documents (doc, name, fragment_count, byte_count, cp_count,"
        " sha256_bytes, sha256_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_index, document.name, len(fragments), total_bytes, text.count,
         byte_hasher.hexdigest(), text.sha256.hexdigest()))

    stats.fragments += len(fragments)
    stats.bytes += total_bytes
    stats.code_points += text.count
    stats.spans += span_count
    stats.rev_rows += rev_row_total


def _append_rev(rows, doc_index, frag, off, length, cp_index, block_shift=REV_BLOCK_SHIFT):
    """Split one source span into block-sized pieces for the reverse index.

    Returns the number of rows appended.  Splitting means a query for bytes in
    one block never reads index rows of other blocks, and the interval check
    ``s < hi AND e > lo`` is exact within a block at any offset.
    """
    size = 1 << block_shift
    end = off + length
    block = off >> block_shift
    count = 0
    while off < end:
        block_start = block << block_shift
        block_end = block_start + size
        stop = end if end < block_end else block_end
        rows.append((doc_index, frag, block, off - block_start, stop - block_start, cp_index))
        count += 1
        off = stop
        block += 1
    return count
