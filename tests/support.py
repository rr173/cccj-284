"""Test helpers: build throw-away manifests and archives."""

import json
import os
import shutil
import tempfile
import unittest

from textarchive.build import build_archive


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="textarchive-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_fragment(self, name, data):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(data)
        return path

    def make_manifest(self, documents, name="manifest.json"):
        """``documents``: list of ``(doc_name, [ (frag_id, bytes), ... ])``."""
        entries = []
        for doc_name, fragments in documents:
            frags = []
            for frag_id, data in fragments:
                path = self.write_fragment("frags/%s__%s.bin" % (doc_name, frag_id), data)
                frags.append({"id": frag_id, "path": path})
            entries.append({"name": doc_name, "fragments": frags})
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"documents": entries}, fp, ensure_ascii=False)
        return path

    def build(self, documents, **kwargs):
        manifest = self.make_manifest(documents)
        out = os.path.join(self.tmp, "archive.tadb")
        build_archive(manifest, out, **kwargs)
        return out
