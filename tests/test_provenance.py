"""Provenance semantics: exact byte ranges, through decomposition, reordering,
composition and fragment-boundary splits."""

import random
import unittest

from textarchive.archive import iter_text
from textarchive.query import cp_provenance, origin_hits
from textarchive.store import Archive
from tests import oracle
from tests.support import ArchiveTestCase
import unicodedata


class ProvenanceTests(ArchiveTestCase):
    def _build_split(self, text, cuts):
        data = text.encode("utf-8")
        fragments = []
        bounds = []
        previous = 0
        for index, cut in enumerate(list(cuts) + [len(data)]):
            frag_id = "f%d" % index
            fragments.append((frag_id, data[previous:cut]))
            bounds.append((frag_id, previous, cut))
            previous = cut
        archive = self.build([("doc", fragments)], working_memory=64)
        return archive, bounds

    def test_decomposition_inherits_all_bytes(self):
        # U+1E09 decomposes to c + cedilla + acute; all three bytes of the
        # original character stay attached, but its decomposition recomposes.
        archive, _ = self._build_split("ḉ", [])
        with Archive(archive) as a:
            items = cp_provenance(a.db, "doc", 0, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["char"], "ḉ")
        self.assertEqual(items[0]["spans"],
                         [{"fragment": "f0", "ordinal": 0, "offset": 0, "length": 3}])

    def test_decomposed_component_inherits_all_bytes(self):
        # U+0F73 (Tibetan) decomposes to two marks and does not recompose; each
        # component must inherit all 3 bytes of the original character.
        archive, _ = self._build_split("ཱི", [])
        with Archive(archive) as a:
            items = cp_provenance(a.db, "doc", 0, 2)
        self.assertEqual([item["char"] for item in items], ["ཱ", "ི"])
        for item in items:
            self.assertEqual(
                [(s["fragment"], s["offset"], s["length"]) for s in item["spans"]],
                [("f0", 0, 3)])

    def test_separately_typed_components_union(self):
        # c + cedilla + acute typed as three characters: the composite takes the
        # union of the three original byte ranges (1 + 2 + 2 bytes).
        archive, _ = self._build_split("ḉ", [])
        with Archive(archive) as a:
            items = cp_provenance(a.db, "doc", 0, 1)
        self.assertEqual(items[0]["char"], "ḉ")
        self.assertEqual(
            [(s["fragment"], s["offset"], s["length"]) for s in items[0]["spans"]],
            [("f0", 0, 5)])

    def test_reordering_keeps_provenance(self):
        # U+0335 (ccc 1) before U+0301 (ccc 230) after a combining mark that
        # blocks composition: the two swap, each keeping its own bytes.
        # "a" + 0327 (ccc 202, composes with a) is not wanted here; use x + 0301
        # + 0335: NFC reorders to x 0335 0301 (no composition with x).
        text = "x̵́"
        archive, _ = self._build_split(text, [])
        with Archive(archive) as a:
            items = cp_provenance(a.db, "doc", 0, 3)
        # order after NFC: 'x', 0335, 0301
        self.assertEqual([ord(item["char"]) for item in items], [0x78, 0x335, 0x301])
        # 0335 came from bytes 3..5 of the original, 0301 from bytes 1..3
        self.assertEqual([(s["offset"], s["length"]) for s in items[1]["spans"]], [(3, 2)])
        self.assertEqual([(s["offset"], s["length"]) for s in items[2]["spans"]], [(1, 2)])

    def test_composition_unions_components(self):
        # c + combining cedilla (4 bytes) -> single precomposed? no: c+cedilla
        # stays apart; use a + combining acute across a fragment boundary.
        text = "á"
        archive, _ = self._build_split(text, [1])  # split between 'a' and the mark
        with Archive(archive) as a:
            items = cp_provenance(a.db, "doc", 0, 1)
        self.assertEqual(items[0]["char"], "á")
        self.assertEqual(
            {(s["fragment"], s["offset"], s["length"]) for s in items[0]["spans"]},
            {("f0", 0, 1), ("f1", 0, 2)})

    def test_split_inside_multibyte_character(self):
        text = "é"  # é is two bytes
        archive, _ = self._build_split(text, [1])
        with Archive(archive) as a:
            items = cp_provenance(a.db, "doc", 0, 1)
        self.assertEqual(
            {(s["fragment"], s["offset"], s["length"]) for s in items[0]["spans"]},
            {("f0", 0, 1), ("f1", 0, 1)})

    def test_vs_oracle(self):
        rng = random.Random(21)
        pool = (list("abcdeiouAEIO n") + [chr(c) for c in range(0x300, 0x370)]
                + ["ᄀ", "ᅡ", "ᆨ", "ཱ", "ི", "ྵ", "ྐྵ", "क़",
                   "ि", "ो", "中", "文", "Ä", "Å"])
        for _ in range(120):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(1, 25)))
            data = text.encode("utf-8")
            if len(data) < 2:
                continue
            cuts = sorted(rng.sample(range(1, len(data)), min(3, len(data) - 1)))
            archive, bounds = self._build_split(text, cuts)
            truth = oracle.ground_truth(data.decode("utf-8"))
            with Archive(archive) as a:
                doc = a.db.execute("SELECT doc FROM documents WHERE name='doc'").fetchone()[0]
                items = cp_provenance(a.db, "doc", 0, len(truth))
                stored = "".join(iter_text(a.db, doc))
            self.assertEqual(stored, "".join(ch for ch, _ in truth))
            for item, (ch, byte_set) in zip(items, truth):
                want = oracle.byte_set_to_spans(byte_set, bounds)
                have = frozenset((s["fragment"], s["offset"], s["length"])
                                 for s in item["spans"])
                self.assertEqual(have, want, (repr(ch), [hex(ord(c)) for c in text]))

    def test_no_bytes_outside_provenance(self):
        """Every original byte is attributed to some normalized code point."""
        text = "Aḉé中" + "ཱི"
        data = text.encode("utf-8")
        archive, bounds = self._build_split(text, [2, 5, 9])
        total = 0
        with Archive(archive) as a:
            docs = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
            for item in cp_provenance(a.db, "doc", 0, docs[1]):
                for span in item["spans"]:
                    total += span["length"]
        self.assertEqual(total, len(data))


class ReverseIndexTests(ArchiveTestCase):
    def test_intersects_without_extra_bytes(self):
        # 'a' 'ḉ' 'b': query only the first byte of the 2-byte 'ḉ'
        data = "aḉb".encode("utf-8")
        archive = self.build([("doc", [("f0", data)])], working_memory=64)
        with Archive(archive) as a:
            hits = origin_hits(a.db, "doc", "f0", 1, 2)
            self.assertEqual([h["char"] for h in hits], ["ḉ"])
            self.assertNotIn("a", [h["char"] for h in hits])

    def test_order_independent_of_fragment_order(self):
        # fragment 1 holds the second half of 'é' but appears first in the manifest
        first = b"a\xc3"
        second = b"\xa9b"
        archive = self.build([("doc", [("z0", first), ("a0", second)])])
        with Archive(archive) as a:
            hits = origin_hits(a.db, "doc", "a0", 0, 1)
        self.assertEqual([h["char"] for h in hits], ["é"])

    def test_disjoint_ranges(self):
        data = "abcdef".encode()
        archive = self.build([("doc", [("f0", data)])])
        with Archive(archive) as a:
            self.assertEqual(origin_hits(a.db, "doc", "f0", 2, 2), [])
            self.assertEqual([h["char"] for h in origin_hits(a.db, "doc", "f0", 2, 3)], ["c"])
            self.assertEqual([h["char"] for h in origin_hits(a.db, "doc", "f0", 4, 6)],
                             ["e", "f"])

    def test_whole_fragment_equals_all_documents(self):
        data = "aḉb中é".encode()
        archive = self.build([("doc", [("f0", data)])])
        with Archive(archive) as a:
            doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
            everything = cp_provenance(a.db, "doc", 0, doc[1])
            hits = origin_hits(a.db, "doc", "f0", 0, len(data))
        self.assertEqual([h["cp"] for h in hits], [item["cp"] for item in everything])

    def test_fragment_ids_unrelated_to_text_order(self):
        # Fragment ids and file order say nothing about text order: z9 holds
        # the first character, z0 the last.  Hits must return normalized
        # positions, not fragment order.
        chars = ["a", "ḉ", "b", "ཱ", "ི", "中"]
        fragments = []
        for index, ch in enumerate(chars):
            fragments.append(("z%d" % (9 - index), ch.encode()))
        archive = self.build([("doc", fragments)])
        with Archive(archive) as a:
            doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
            text = "".join(iter_text(a.db, doc[0]))
            self.assertEqual(text, "aḉbཱི中")
            self.assertEqual([h["char"] for h in origin_hits(a.db, "doc", "z9", 0, 1)], ["a"])
            self.assertEqual([h["cp"] for h in origin_hits(a.db, "doc", "z9", 0, 1)], [0])
            # z4 holds the last character (中), at normalized position 5
            self.assertEqual([h["cp"] for h in origin_hits(a.db, "doc", "z4", 0, 3)], [5])

    def test_across_reverse_index_block_boundary(self):
        # more than one block (1024 bytes) of source; every byte range must give
        # exactly the code points whose provenance touches it.
        data = ("aḉb中" * 400).encode()   # ~3200 bytes, several blocks
        archive = self.build([("doc", [("f0", data)])], working_memory=1 << 15)
        with Archive(archive) as a:
            doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
            everything = cp_provenance(a.db, "doc", 0, doc[1])
            for start in (0, 1000, 1023, 1024, 1025, 2047, 2048, 3000):
                hits = origin_hits(a.db, "doc", "f0", start, start + 2)
                want = [item["cp"] for item in everything
                        if any(s["fragment"] == "f0"
                               and s["offset"] < start + 2
                               and s["offset"] + s["length"] > start
                               for s in item["spans"])]
                self.assertEqual([h["cp"] for h in hits], want, start)


if __name__ == "__main__":
    unittest.main()
