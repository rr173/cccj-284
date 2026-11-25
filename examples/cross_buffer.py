#!/usr/bin/env python3
"""Generate a document whose normalization crosses the working-buffer bound.

The example builds one document out of three fragments:

* ``part-a`` -- the first byte of a two-byte UTF-8 character (``é``), i.e. a
  character split across a fragment boundary;
* ``part-b`` -- the second byte of that character, then a combining sequence
  longer than the default working memory (thousands of marks), then the first
  half of a precomposed character that decomposes and recomposes across the
  boundary;
* ``part-c`` -- the remainder.

With ``--working-memory 1024`` the combining run cannot fit in the buffer, so
the builder spills it to disk.  The resulting text and provenance are exactly
the same as with a large buffer; the accompanying test proves it.

Usage: ``python3 examples/cross_buffer.py OUTPUT_DIR``
"""

import json
import os
import sys

RUN_LENGTH = 6000          # combining marks in one uninterrupted run
PREFIX = "é"               # split inside this character
MIDDLE = "a" + "̈" * RUN_LENGTH + "中"
SUFFIX = "ḉz"              # ḉ decomposes and recomposes


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    text = PREFIX + MIDDLE + SUFFIX
    data = text.encode("utf-8")

    # Find a split point inside the first multi-byte character: the first
    # continuation byte belongs to the following fragment.
    split = 1
    while split < len(data) and not (0x80 <= data[split] < 0xC0):
        split += 1
    # Split once more, far into the combining run, so the run spans fragments.
    third = min(len(data) - 1, 2 * len(data) // 3)

    first_cut = split
    second_cut = third
    pieces = [
        ("part-a", data[:first_cut]),
        ("part-b", data[first_cut:second_cut]),
        ("part-c", data[second_cut:]),
    ]
    fragments = []
    for frag_id, payload in pieces:
        path = os.path.join(out_dir, frag_id + ".bin")
        with open(path, "wb") as fp:
            fp.write(payload)
        fragments.append({"id": frag_id, "path": frag_id + ".bin"})
    manifest = {"documents": [{"name": "huge", "fragments": fragments}]}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)
    print("wrote %s (%d bytes, %d code points after NFC)"
          % (os.path.join(out_dir, "manifest.json"), len(data), len(text)))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: cross_buffer.py OUTPUT_DIR\n")
        raise SystemExit(2)
    main(sys.argv[1])
