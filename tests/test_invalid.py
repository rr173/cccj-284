"""Invalid input, atomicity, corruption detection and purity of working-memory."""

import json
import os
import sqlite3
import unittest

from textarchive.archive import iter_text
from textarchive.build import BuildError, build_archive
from textarchive.query import cp_provenance
from textarchive.store import Archive
from textarchive.utf8 import Utf8Error
from tests.support import ArchiveTestCase


class InvalidUtf8Tests(ArchiveTestCase):
    def test_error_names_fragment_and_offset(self):
        # fragment 1 starts with a stray continuation byte at offset 0
        archive_path = os.path.join(self.tmp, "bad.tadb")
        manifest = self.make_manifest([
            ("doc", [("a", b"ab"), ("b", b"\xffcd")]),
        ])
        with self.assertRaises(Utf8Error) as ctx:
            build_archive(manifest, archive_path)
        error = ctx.exception
        self.assertEqual(error.fragment_id, "b")
        self.assertEqual(error.fragment_offset, 0)
        self.assertFalse(os.path.exists(archive_path), "half-built archive must not exist")

    def test_error_offset_inside_fragment(self):
        manifest = self.make_manifest([("doc", [("a", b"ok\xc3(\xc3\xa9")])])
        with self.assertRaises(Utf8Error) as ctx:
            build_archive(manifest, os.path.join(self.tmp, "bad.tadb"))
        self.assertEqual(ctx.exception.fragment_id, "a")
        self.assertEqual(ctx.exception.fragment_offset, 3)  # the byte after 0xC3

    def test_truncated_sequence_at_document_end(self):
        manifest = self.make_manifest([("doc", [("a", b"ab"), ("b", b"\xe4\xb8")])])
        with self.assertRaises(Utf8Error) as ctx:
            build_archive(manifest, os.path.join(self.tmp, "bad.tadb"))
        self.assertEqual(ctx.exception.fragment_id, "b")

    def test_surrogate_is_rejected(self):
        manifest = self.make_manifest([("doc", [("a", b"\xed\xa0\x80")])])
        with self.assertRaises(Utf8Error):
            build_archive(manifest, os.path.join(self.tmp, "bad.tadb"))


class AtomicBuildTests(ArchiveTestCase):
    def test_second_document_failure_leaves_no_archive(self):
        out = os.path.join(self.tmp, "out.tadb")
        manifest = self.make_manifest([
            ("ok", [("a", b"valid")]),
            ("bad", [("b", b"\xff")]),
        ])
        with self.assertRaises(Utf8Error):
            build_archive(manifest, out)
        self.assertFalse(os.path.exists(out))
        self.assertEqual([p for p in os.listdir(self.tmp) if "building" in p], [])

    def test_failure_does_not_clobber_existing_archive(self):
        good = self.build([("doc", [("f", "keep me".encode())])])
        before = open(good, "rb").read()
        manifest = self.make_manifest([("bad", [("f", b"\xff")])], name="bad.json")
        with self.assertRaises(Utf8Error):
            build_archive(manifest, good)
        self.assertEqual(open(good, "rb").read(), before)
        with Archive(good) as a:
            doc = a.db.execute("SELECT doc FROM documents").fetchone()[0]
            self.assertEqual("".join(iter_text(a.db, doc)), "keep me")


class WorkingMemoryTests(ArchiveTestCase):
    TEXT = "a" + "̀́̈" * 3000 + "中" + "ཱི" * 2000

    def test_text_and_provenance_are_identical_at_any_limit(self):
        data = self.TEXT.encode("utf-8")
        runs = []
        for limit in (0, 512, 4096, 1 << 20):
            directory = os.path.join(self.tmp, "w%d" % limit)
            os.makedirs(directory)
            manifest = self.make_manifest([("doc", [("f", data)])],
                                          name="m%d.json" % limit)
            out = os.path.join(directory, "a.tadb")
            build_archive(manifest, out, working_memory=limit)
            with Archive(out) as a:
                doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
                text = "".join(iter_text(a.db, doc[0]))
                prov = cp_provenance(a.db, "doc", 0, doc[1])
            runs.append((text, [(i["cp"], i["spans"]) for i in prov]))
        for other in runs[1:]:
            self.assertEqual(other, runs[0])

    def test_long_run_exceeding_buffer_uses_disk(self):
        # a combining run far larger than the 256-byte floor of the buffer
        text = "x" + "̀" * 5000
        archive = self.build([("doc", [("f", text.encode())])], working_memory=256)
        with Archive(archive) as a:
            doc = a.db.execute("SELECT doc FROM documents").fetchone()[0]
            import unicodedata
            self.assertEqual("".join(iter_text(a.db, doc)), unicodedata.normalize("NFC", text))

    def test_run_larger_than_any_internal_batch(self):
        # 150k combining marks: bigger than the provenance/queue batches, so the
        # run buffer spills to per-class files and is streamed back.
        import unicodedata
        text = "A" + "̀" * 150000
        archive = self.build([("doc", [("f", text.encode())])], working_memory=1024)
        with Archive(archive) as a:
            doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
            first = cp_provenance(a.db, "doc", 0, 1)[0]
        self.assertEqual(first["char"], "À")
        self.assertEqual(
            [(s["fragment"], s["offset"], s["length"]) for s in first["spans"]],
            [("f", 0, 3)])
        with Archive(archive) as a:
            self.assertEqual("".join(iter_text(a.db, doc[0])), unicodedata.normalize("NFC", text))


if __name__ == "__main__":
    unittest.main()
