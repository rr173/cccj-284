"""End-to-end reopen, integrity, corruption detection and bounded query plans."""

import os
import sqlite3
import unicodedata
import unittest

from textarchive.archive import iter_text
from textarchive.query import cp_provenance, origin_hits
from textarchive.store import Archive
from textarchive.verify import verify_archive
from tests.support import ArchiveTestCase


class ReopenTests(ArchiveTestCase):
    def test_reopen_reads_text_and_both_directions(self):
        text = "Aḉ b́ 中 ཱི"
        archive = self.build([("doc", [("f0", text.encode())])])
        with Archive(archive) as a:
            doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
            stored = "".join(iter_text(a.db, doc[0]))
            self.assertEqual(stored, unicodedata.normalize("NFC", text))
            items = cp_provenance(a.db, "doc", 0, doc[1])
            self.assertEqual("".join(i["char"] for i in items), stored)
            hits = origin_hits(a.db, "doc", "f0", 0, len(text.encode()))
            self.assertEqual([h["cp"] for h in hits], [i["cp"] for i in items])

    def test_unicode_version_is_recorded_and_used(self):
        archive = self.build([("doc", [("f", "é".encode())])])
        with Archive(archive) as a:
            self.assertEqual(a.meta["unicode_version"], a.tables.version)
            self.assertEqual(a.meta["normalization"], "NFC")

    def test_verify_ok(self):
        archive = self.build([
            ("a", [("x", "one".encode())]),
            ("b", [("y", "two".encode()), ("z", "three".encode())]),
        ])
        report = verify_archive(archive, sample_limit=20)
        self.assertTrue(report["ok"], report)

    def test_verify_detects_non_nfc_text(self):
        archive = self.build([("doc", [("f", "aé́́".encode())])])
        conn = sqlite3.connect(archive)
        conn.execute("UPDATE text_blocks SET data = ?",
                     (("a" + "e" + 3 * "́").encode(),))
        conn.commit()
        conn.close()
        report = verify_archive(archive)
        self.assertFalse(report["ok"])
        self.assertTrue(any("not NFC" in p for p in report["document_reports"][0]["problems"]))

    def test_verify_detects_bad_provenance(self):
        archive = self.build([("doc", [("f", b"hello")])])
        conn = sqlite3.connect(archive)
        conn.execute("UPDATE cp_spans SET len = 999 WHERE cp = 0")
        conn.commit()
        conn.close()
        report = verify_archive(archive)
        self.assertFalse(report["ok"])


class QueryPlanTests(ArchiveTestCase):
    """Small range queries must use indexes, not scan the document."""

    def test_indexes_are_used(self):
        archive = self.build([("doc", [("f", ("aḉb中" * 2000).encode())])])
        with Archive(archive) as a:
            plans = {
                "cp_spans": a.db.execute(
                    "EXPLAIN QUERY PLAN SELECT cp, frag, off, len FROM cp_spans"
                    " WHERE doc = ? AND cp >= ? AND cp < ? ORDER BY cp, idx",
                    (0, 0, 5)).fetchall(),
                "rev": a.db.execute(
                    "EXPLAIN QUERY PLAN SELECT DISTINCT cp FROM rev"
                    " WHERE doc = ? AND frag = ? AND block = ? AND s < ? AND e > ?",
                    (0, 0, 0, 10, 0)).fetchall(),
            }
        for name, rows in plans.items():
            detail = " ".join(str(r[-1]) for r in rows)
            self.assertIn("SEARCH", detail, name)
            self.assertNotIn("SCAN", detail, name)

    def test_small_query_is_fast_and_exact(self):
        text = "aḉb中" * 20000
        archive = self.build([("doc", [("f", text.encode())])], working_memory=1 << 16)
        with Archive(archive) as a:
            doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
            total = doc[1]
            for position in (0, total // 3, total - 5):
                items = cp_provenance(a.db, "doc", position, position + 5)
                self.assertEqual(len(items), 5)
                self.assertEqual(items[0]["cp"], position)


if __name__ == "__main__":
    unittest.main()
