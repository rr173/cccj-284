# textarchive — offline NFC text archive with byte provenance

`textarchive` turns a set of documents, each delivered as an ordered list of
byte fragments, into a single self-contained archive that holds:

* the complete **NFC** text of every document (with the Unicode version used),
* for every normalized code point, the **exact original byte ranges** it came
  from — no whole-span approximations,
* a **reverse index** that, for a byte range of one fragment, returns every
  normalized code point it affected, ordered by position in the normalized
  text and independent of fragment order,

and can be reopened and queried later, offline, without the original files and
without the interpreter's Unicode tables (the tables are stored in the
archive).  Invalid UTF-8 is reported with its fragment and byte offset, and a
failed build leaves no archive at all.

The tool is pure Python 3 (3.8+) using only the standard library (`sqlite3`
included).  Nothing is downloaded and nothing needs compiling.

---

## Quick start

```sh
./textarchive build  examples/basic/manifest.json -o /tmp/basic.tadb
./textarchive info   /tmp/basic.tadb
./textarchive text   /tmp/basic.tadb greeting
./textarchive spans  /tmp/basic.tadb greeting 0 4          # normalized -> original
./textarchive hits   /tmp/basic.tadb tibetan tibetan-0a 0 3 # original -> normalized
./textarchive verify /tmp/basic.tadb
```

Run the whole walkthrough (basic example, generated cross-buffer example and
verification) with:

```sh
sh examples/run_examples.sh
```

Run the test suite with:

```sh
python3 run_tests.py
```

---

## Manifest

A manifest lists documents; each document lists its fragments **in order**.
The fragments are concatenated byte-for-byte, with no separator; the result is
the document's UTF-8 text.  Documents never share bytes.

```json
{
  "documents": [
    {
      "name": "greeting",
      "fragments": [
        {"id": "greeting-0a", "path": "fragments/greeting-0a.bin"},
        {"id": "greeting-0b", "path": "fragments/greeting-0b.bin"}
      ]
    }
  ]
}
```

* Fragment ids are unique within a document and are what queries report.
* A UTF-8 character may be split across any number of fragment boundaries.
* Relative paths are resolved against the manifest's directory.
* A one-line-per-fragment alternative exists for shell use:

  ```
  doc greeting
  greeting-0a fragments/greeting-0a.bin
  greeting-0b fragments/greeting-0b.bin
  ```

---

## Guarantees

### Text

* The stored text is the complete document in **NFC**, computed with the
  Unicode version recorded in the archive (`unicode_version` in `info`).
* No case folding, no compatibility folding, no byte replacement.
* The normalizer reproduces ICU exactly: it agrees with Python's
  `unicodedata.normalize("NFC", …)` on **all 1,114,112 code points** and on
  large randomized corpora, including input split at arbitrary byte offsets.
  (See `tests/test_nfc.py`.)

### Provenance rules

For every normalized code point the archive stores a tuple of spans
`(fragment id, zero-based byte offset, byte length)`.  A span covers only bytes
that actually contributed:

* a character that **decomposes** gives each decomposed component **all** of the
  original character's bytes;
* **canonical reordering** moves code points but not provenance;
* a **composite** inherits the **union** of its components' provenance;
* a UTF-8 sequence split across fragments yields one span per fragment piece.

Provenance is stored exactly — never widened to "first byte … last byte" of a
fragment, and never assuming that fragment order equals text order.

### Reverse direction

`hits` finds every normalized code point whose provenance intersects the given
byte range of a fragment, including code points that merely touch the range at
one byte, and including code points whose other bytes (or other fragments) lie
outside the range.  Results are complete, ordered by normalized position.

### Bounded working memory

`build --working-memory BYTES` bounds the in-memory buffers.  Two structures
can grow without limit and are spilled to disk when the bound is exceeded:

* a combining run (an arbitrarily long sequence of non-starters) is bucketed by
  canonical combining class on disk, giving the exact stable canonical order;
* the queue of marks waiting behind a starter is a FIFO spooled to disk.

A single document, or a single uninterrupted combining sequence, may therefore
be far larger than the working buffer.  The resulting text and provenance are
**identical** at every limit; `tests/test_invalid.py` and
`tests/test_cross_buffer.py` assert exactly that, and `examples/cross_buffer.py`
generates a document that forces the spill path.  Peak memory stays roughly
constant as a document grows (a fixed SQLite page cache and fixed write
batches — a 20× larger document does not raise peak RSS).

Use `--spill-dir DIR` to choose where spill files go (default: system temp).
Spill files are removed when the build ends, successfully or not.

### Atomicity and errors

* The archive is built in a temporary SQLite database next to the destination
  and moved into place only after the whole transaction commits.  A failure
  leaves the destination untouched (an older archive there stays valid).
* Text, provenance and reverse index are written in the same transaction, so
  an archive with text but no provenance (or vice versa) cannot exist.
* Invalid UTF-8 is never replaced.  The build fails with the document, fragment
  id, in-fragment byte offset, absolute offset, offending byte and reason:

  ```
  textarchive: invalid UTF-8 in document 'doc', fragment 'b', byte offset 0:
               invalid start byte (byte 0xFF)
  ```

  Surrogate encodings (`ED A0 80`), overlong forms and code points above
  U+10FFFF are all rejected and located the same way.

### Queries do not scan the document

* Forward queries (`spans`) read only the `cp_spans` rows for the requested
  code point range and the text blocks covering it.
* Reverse queries (`hits`) read only the reverse-index blocks intersected by
  the byte range.
* `tests/test_archive.py::QueryPlanTests` asserts that both use indexes and
  never a table scan, and that small queries on a 100k-code-point document are
  fast.

### Reopening and verification

`verify` reopens the archive and, using the stored Unicode tables and no source
files, checks: the stored text is valid UTF-8 and is NFC; every code point has
provenance whose spans lie inside the declared fragment sizes; the reverse
index agrees with the forward provenance on sampled byte ranges (whole
fragments, single bytes and random ranges); the code point count and SHA-256 of
the text match the header.

---

## Command reference

```
textarchive build MANIFEST -o ARCHIVE [--working-memory BYTES]
                          [--spill-dir DIR] [--chunk-size BYTES] [--json]
textarchive info ARCHIVE [--document NAME] [--json]
textarchive text ARCHIVE DOCUMENT [--start N] [--end M] [--json]
textarchive spans ARCHIVE DOCUMENT START END [--json]   # normalized -> original
textarchive hits ARCHIVE DOCUMENT FRAGMENT START END [--json]  # original -> normalized
textarchive verify ARCHIVE [--sample-limit N] [--json]
```

Ranges are zero-based half-open: `[start, end)` in code points for `text` and
`spans`, in bytes for `hits`.  Fragment byte offsets are zero-based within that
fragment.  `--json` output is stable and suitable for scripting.

Exit status: `0` success, `1` invalid data (bad UTF-8, out-of-range query,
failed verification), `2` usage or I/O error.

### JSON shapes

`spans` and `hits`:

```json
{
  "document": "greeting",
  "range": [0, 4],
  "code_points": [
    {"cp": 0, "char": "X",
     "spans": [{"fragment": "greeting-0a", "offset": 0, "length": 1}]}
  ]
}
```

`hits` additionally echoes `"fragment": "..."`.  Each `spans` list is the
code point's complete provenance (it may include bytes outside the queried
range), with spans sorted by fragment then offset.

---

## Design notes

* **Normalizer.**  Full canonical decomposition (Hangul handled
  algorithmically, everything else from canonical decomposition mappings),
  buffering of the maximal non-starter run, a stable ordering pass (in memory
  or via per-class disk buckets), then UAX #15 composition with the standard
  blocking rules.  Composition pairs are derived at build time by filtering
  canonical decompositions of length two through NFC itself, which correctly
  removes full composition exclusions.  The algorithm was validated against
  ICU on every code point and randomized sequences.
* **Storage.**  SQLite.  Text is stored in 1024-code-point blocks; provenance in
  one row per span; the reverse index splits each span at 1024-byte block
  boundaries, so an interval query touches only the relevant blocks and the
  `s < hi AND e > lo` test is exact at every offset (no R-tree float
  coordinate rounding issue at large offsets).
* **Unicode version.**  The build records `unicodedata.unidata_version` and
  serializes the tables into the archive; queries and verification use the
  stored tables, so reopening on a machine with a different Unicode version
  does not change the text or its provenance.

### Files

```
src/textarchive/
  unicode_data.py   Unicode tables, serialization, decomposition
  nfc.py            streaming NFC with provenance
  spill.py          bounded-memory buffers (run buckets, FIFO spool)
  utf8.py           strict incremental UTF-8 with byte provenance and errors
  source.py         fragment concatenation as a byte stream
  spans.py          span merge/union helpers
  archive.py        SQLite schema, text blocks, atomic writer
  build.py          manifest loading and archive construction
  query.py          forward and reverse queries
  store.py          opening an archive and loading its tables
  verify.py         end-to-end consistency check
  cli.py            command line interface
examples/           runnable examples (checked-in and generated)
tests/              unittest suite, including an independent oracle
```
