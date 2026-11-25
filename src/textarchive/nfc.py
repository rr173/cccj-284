"""Streaming NFC normalization that carries byte provenance.

The normalizer consumes ``(codepoint, spans)`` pairs where ``spans`` are the
exact original byte ranges that formed the code point, and produces
``(codepoint, spans)`` pairs of the NFC text.

Provenance rules:

* decomposition: every decomposed component inherits *all* byte ranges of the
  original character;
* canonical reordering: order changes, provenance does not;
* composition: the composite inherits the *union* of its components' ranges.

Both rules are implemented literally: decomposition copies the span tuple,
reordering moves whole records, composition calls :func:`spans.union_spans`.

Memory is bounded by ``working_memory`` regardless of document size.  Two
things can grow without bound and are therefore spilled to disk:

* a combining run (an arbitrarily long sequence of non-starters): the
  :class:`~textarchive.spill.RunBuffer` buckets it by combining class on disk,
  which yields exactly the stable canonical order;
* the queue of marks waiting behind a starter: a :class:`SpillQueue`.

Algorithm (UAX #15, with the ordering done on the whole run at once):

* decompose fully;
* buffer the maximal non-starter run; when a starter arrives, replay the run
  in stable canonical order;
* composition: a mark composes with the last starter when it is not blocked
  (no queued mark has ccc >= its own), a starter composes with the last
  starter only when they are adjacent;
* at the end of every starter sequence, emit the starter then the queued marks.

The output is byte-for-byte identical to ``unicodedata.normalize("NFC", ...)``
for the same Unicode version (verified against the interpreter over all code
points and random sequences, including fragment-split input).
"""

from .spans import union_spans
from .spill import SpoolContext
from .unicode_data import UnicodeTables, builtin_tables

DEFAULT_WORKING_MEMORY = 4 << 20  # 4 MiB


class Normalizer:
    """Incremental NFC normalizer with provenance."""

    def __init__(self, tables=None, working_memory=DEFAULT_WORKING_MEMORY,
                 spill_directory=None, spill_context=None):
        self.tables: UnicodeTables = tables or builtin_tables()
        self.working_memory = max(0, int(working_memory or 0))
        self._owns_context = spill_context is None
        self._context = spill_context or SpoolContext(spill_directory)
        self._run = self._context.run_buffer(max(256, self.working_memory // 2))
        self._queue = self._context.queue(max(256, self.working_memory // 4))
        # Bounded memo of full decompositions; cleared wholesale when full so
        # memory never grows with the number of distinct code points.
        self._decomp_cache = {}
        self._decomp_cache_limit = 1 << 16
        self._reset()

    # -- lifecycle ----------------------------------------------------------
    def _reset(self):
        self._starter = None
        self._starter_spans = ()
        self._barrier = 0
        self._run.reset()
        self._queue.reset()

    def close(self):
        self._run.reset()
        self._queue.reset()
        if self._owns_context:
            self._context.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- feeding ------------------------------------------------------------
    def feed(self, cp, spans):
        """Feed one input code point; yields ``(codepoint, spans)`` outputs."""
        for dcp in self._decompose(cp):
            cc = self.tables.ccc.get(dcp, 0)
            if cc == 0:
                yield from self._flush_run()
                yield from self._on_starter(dcp, spans)
            else:
                self._run.add(dcp, cc, spans)

    def finish(self):
        """Flush all pending state; yields the remaining outputs."""
        yield from self._flush_run()
        yield from self._end_sequence()

    # -- internals ----------------------------------------------------------
    def _decompose(self, cp):
        cached = self._decomp_cache.get(cp)
        if cached is None:
            if len(self._decomp_cache) >= self._decomp_cache_limit:
                self._decomp_cache.clear()
            cached = self.tables.decomp_cp(cp)
            self._decomp_cache[cp] = cached
        return cached

    def _flush_run(self):
        if not len(self._run):
            return
        for cp, cc, spans in self._run.iter_sorted():
            yield from self._on_mark(cp, cc, spans)
        self._run.reset()

    def _on_mark(self, cp, cc, spans):
        if self._starter is None:
            # a leading non-starter: nothing to compose with
            yield (cp, spans)
            return
        if self._barrier == 0 or self._barrier < cc:
            composite = self.tables.compose_pair(self._starter, cp)
            if composite is not None:
                self._starter = composite
                self._starter_spans = union_spans(self._starter_spans, spans)
                return
        self._queue.add(cp, spans)
        self._barrier = cc

    def _on_starter(self, cp, spans):
        if self._starter is not None:
            if self._barrier == 0:  # adjacent: the only case starters compose
                composite = self.tables.compose_pair(self._starter, cp)
                if composite is not None:
                    self._starter = composite
                    self._starter_spans = union_spans(self._starter_spans, spans)
                    return
            yield from self._end_sequence()
        self._starter = cp
        self._starter_spans = spans
        self._barrier = 0

    def _end_sequence(self):
        if self._starter is None:
            return
        yield (self._starter, self._starter_spans)
        self._starter = None
        self._starter_spans = ()
        for cp, spans in self._queue:
            yield (cp, spans)
        self._queue.reset()
        self._barrier = 0


def normalize_pairs(pairs, tables=None, working_memory=DEFAULT_WORKING_MEMORY,
                    spill_directory=None):
    """Normalize an iterable of ``(cp, spans)``; yields NFC ``(cp, spans)``."""
    with Normalizer(tables, working_memory, spill_directory) as norm:
        for cp, spans in pairs:
            yield from norm.feed(cp, spans)
        yield from norm.finish()
