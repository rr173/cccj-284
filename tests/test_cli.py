"""The command line interface: build, query both directions, verify, errors."""

import json
import os
import subprocess
import sys
import unittest

from tests.support import ArchiveTestCase

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(HERE, "textarchive")


def run(*args, **kwargs):
    return subprocess.run([sys.executable, TOOL] + list(args),
                          capture_output=True, text=True, **kwargs)


class CliTests(ArchiveTestCase):
    def _archive(self):
        manifest = self.make_manifest([
            ("doc", [("f0", "Aḉ".encode()), ("f1", "b́中".encode())]),
        ])
        out = os.path.join(self.tmp, "a.tadb")
        result = run("build", manifest, "-o", out, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return out, json.loads(result.stdout)

    def test_build_reports_settings(self):
        _, payload = self._archive()
        self.assertEqual(payload["documents"], 1)
        self.assertEqual(payload["fragments"], 2)
        self.assertTrue(payload["unicode_version"])

    def test_info_and_text(self):
        archive, _ = self._archive()
        info = json.loads(run("info", archive, "--json").stdout)
        self.assertEqual(info["documents"][0]["document"], "doc")
        text = json.loads(run("text", archive, "doc", "--json").stdout)
        self.assertEqual(text["text"], "Aḉb́中")

    def test_spans_and_hits(self):
        archive, _ = self._archive()
        spans = json.loads(run("spans", archive, "doc", "1", "2", "--json").stdout)
        self.assertEqual(spans["code_points"][0]["char"], "ḉ")
        hits = json.loads(run("hits", archive, "doc", "f1", "0", "1", "--json").stdout)
        self.assertEqual([c["char"] for c in hits["code_points"]], ["b"])
        # a range covering the first byte of the combining mark hits it too
        hits = json.loads(run("hits", archive, "doc", "f1", "0", "2", "--json").stdout)
        self.assertEqual([c["char"] for c in hits["code_points"]], ["b", "́"])

    def test_verify(self):
        archive, _ = self._archive()
        result = run("verify", archive, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_invalid_utf8_exit_code_and_location(self):
        manifest = self.make_manifest([("bad", [("f0", b"\xff")])])
        result = run("build", manifest, "-o", os.path.join(self.tmp, "x.tadb"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid UTF-8", result.stderr)
        self.assertIn("f0", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x.tadb")))

    def test_out_of_range_is_an_error(self):
        archive, _ = self._archive()
        result = run("spans", archive, "doc", "0", "999")
        self.assertEqual(result.returncode, 1)
        self.assertIn("out of bounds", result.stderr)

    def test_missing_fragment_is_reported(self):
        manifest = os.path.join(self.tmp, "m.json")
        with open(manifest, "w") as fp:
            json.dump({"documents": [{"name": "d",
                                      "fragments": [{"id": "x",
                                                     "path": "nope.bin"}]}]}, fp)
        result = run("build", manifest, "-o", os.path.join(self.tmp, "x.tadb"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot stat fragment", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_unknown_document_is_reported(self):
        archive, _ = self._archive()
        result = run("text", archive, "nope")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no such document", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
