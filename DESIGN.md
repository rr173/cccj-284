# Design and decisions

Short notes on the choices behind `textarchive`, why they were made, and what
they guarantee.  The task's acceptance criteria are quoted where relevant.

## Normalization

**NFC, no case folding, no replacement.**  The builder decodes strictly and
normalizes to NFC.  Invalid UTF-8 raises instead of producing U+FFFD, so there
is never a "best effort" text that silently loses the original.

**Unicode version is pinned.**  The tables are derived from the interpreter at
build time (`unicodedata.unidata_version`, e.g. 14.0.0) and serialized into the
archive.  Queries and `verify` load the stored tables, so a later interpreter
with a different Unicode version cannot change what "NFC" means for an existing
archive.  `info` reports the version.

**The normalizer matches ICU.**  `tests/test_nfc.py` compares it with Python's
`unicodedata.normalize("NFC", …)` for **all** 1,114,112 code points and for
20,000 random sequences drawn from a pool of combining marks, Hangul jamo,
Indic signs and decomposable letters.  The composition-pair table is built by
filtering canonical two-part decompositions through NFC, which removes full
composition exclusions correctly (e.g. U+0958, U+0F73, U+0344, U+2ADC).  Hangul
is handled algorithmically.

## Provenance (quoted requirements → implementation)

> *"分解出的成分继承原字符的全部字节位置"* — recursive canonical decomposition
returns each component together with a *copy* of the original character's span
tuple (`nfc.py`, `_decompose` + `feed`).

> *"重排不改来源"* — canonical reordering moves whole `(codepoint, spans)`
records; only combining class decides the order (`spill.RunBuffer`).

> *"合成后的字继承其各成分的来源并集"* — composition calls
`spans.union_spans`, which merges the two span tuples and coalesces touching
ranges in the same fragment.

> *"不以包含无关字节的首尾大区间替代"* — provenance is never widened to the
min/max bytes of a fragment.  A span covers only bytes that actually
contributed; when a UTF-8 character is split across fragments there is one span
per fragment piece, and no span spans a gap.

> *"不能假定原件顺序与规范文本中的顺序相同"* — the reverse lookup returns code
points ordered by their position in the normalized text, not by fragment or
byte order.  A composite built from bytes of several fragments is reported in
normalized order regardless of where those fragments sit in the manifest.

Correctness is checked against an **independent oracle**
(`tests/oracle.py`) that implements the rules literally with absolute byte
indexes and Python's own `unicodedata`; `tests/test_provenance.py::test_vs_oracle`
requires exact agreement on 120 random multi-fragment documents.

## Reverse lookup cost

> *"查一小段不用扫描整篇"*

The reverse index stores each provenance span split at 1024-byte block
boundaries (`rev(doc, frag, block, s, e, cp)` with relative offsets).  A query
for bytes `[a, b)` of fragment `f` visits only blocks `a>>10 … (b-1)>>10` and
runs the exact vertical/horizontal test `s < hi AND e > lo` inside each block.
The index `rev_lookup(doc, frag, block, s)` serves it; UUID-sized fragments with
offsets far beyond 2^32 are fine because no floating-point geometry is
involved.  Forward queries read `cp_spans` by primary key `(doc, cp, idx)` and
the text blocks that cover the range.  `tests/test_archive.py::QueryPlanTests`
asserts both plans use `SEARCH` and never `SCAN`, and that small queries on a
100k-code-point document take well under a millisecond.

The sampler in `verify` compares `origin_hits` with a brute-force scan on whole
fragments, single bytes and random ranges, which keeps the two implementations
honest against each other.

## Bounded memory and long runs

> *"一篇正文乃至一个连续组合序列都可能超过缓冲"*

Two structures can in principle be unbounded:

* the **combining run** — handled by `spill.RunBuffer`, which buckets records by
  combining class into per-class files once the memory budget is exceeded.
  Reading buckets in class order is the *stable* sort that canonical ordering
  requires, so the result is byte-identical to the in-memory path.  It is
  O(run size) I/O, not O(run²).
* the **mark queue** behind a starter — a `SpillQueue` FIFO spooled to disk.

The buffer limit is advisory: it selects the in-memory path or the disk path,
and never changes the output.  `tests/test_invalid.py` builds the same document
at 0, 512, 4096 and 1 MiB and requires identical text *and* identical
provenance; `tests/test_cross_buffer.py` does the same for the generated
`examples/cross_buffer.py` document (a 6000-mark run, split mid-character).

## Atomicity

> *"无效输入不能发布只有正文或只有来源的半成品"*

Everything — text, provenance, reverse index, metadata — is written to a
temporary database file and committed in a single transaction; only then is the
file `os.replace`d onto the destination.  Any exception removes the temporary
file.  Consequences, all tested:

* invalid UTF-8, a missing fragment or an I/O error aborts with no archive
  created;
* an existing archive at the destination is left byte-for-byte intact;
* a partially written archive with text but no provenance cannot be produced.

## What is stored

For the reviewers:

* `documents` — name, fragment count, byte count, NFC code point count,
  SHA-256 of the original bytes and of the NFC text;
* `fragments` — id, file name, size, SHA-256;
* `text_blocks` — the NFC text, blockwise;
* `cp_spans` — one row per provenance span of each code point;
* `rev` — the block-split reverse index;
* `unicode_tables` — the exact tables used.

`verify` re-derives everything it can from this alone (no source files): that
the text is its own NFC under the stored tables, that every code point has
in-bounds provenance, and that the reverse index matches the forward data.

## Limits / deliberate non-goals

* One archive is one SQLite file; there is no cross-document deduplication of
  fragments (documents are explicitly disjoint).
* `--jobs` is accepted for interface stability but the build is single-process;
  SQLite writes one transaction anyway.
* The line-oriented manifest format is a convenience; JSON is the documented
  interface for paths containing spaces (the line format splits on whitespace).
* `verify --sample-limit` samples the reverse index rather than checking every
  possible byte range (which would be O(bytes²)); a full check of provenance
  bounds and text form is always performed.
