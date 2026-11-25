"""The generated cross-buffer example must be identical at every memory limit."""

import json
import os
import subprocess
import sys
import unittest

from textarchive.archive import iter_text
from textarchive.build import build_archive
from textarchive.query import cp_provenance
from textarchive.store import Archive

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from tests.support import ArchiveTestCase  # noqa: E402


class CrossBufferExampleTests(ArchiveTestCase):
    def test_generated_example_is_buffer_independent(self):
        out_dir = os.path.join(self.tmp, "cross")
        subprocess.run([sys.executable, os.path.join(HERE, "examples", "cross_buffer.py"),
                        out_dir], check=True, stdout=subprocess.DEVNULL)
        manifest = os.path.join(out_dir, "manifest.json")
        results = []
        for limit in (1024, 1 << 20):
            archive = os.path.join(self.tmp, "a%d.tadb" % limit)
            stats = build_archive(manifest, archive, working_memory=limit)
            with Archive(archive) as a:
                doc = a.db.execute("SELECT doc, cp_count FROM documents").fetchone()
                text = "".join(iter_text(a.db, doc[0]))
                prov = [(item["cp"], [(s["fragment"], s["offset"], s["length"])
                                      for s in item["spans"]])
                        for item in cp_provenance(a.db, "huge", 0, doc[1])]
            results.append((text, prov, stats.code_points))
        self.assertEqual(results[0], results[1])
        # the split character really does come from two fragments
        self.assertEqual(results[0][1][0][1],
                         [("part-a", 0, 1), ("part-b", 0, 1)])
        # and the combining run is longer than the small working buffer
        self.assertGreater(len(results[0][0]), 1024)


if __name__ == "__main__":
    unittest.main()
