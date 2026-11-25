"""The normalizer must agree with ICU exactly, including fragment-split input."""

import random
import unicodedata
import unittest

from textarchive.nfc import normalize_pairs


def nfc(pairs):
    return "".join(chr(cp) for cp, _ in normalize_pairs(pairs))


class NormalizationTests(unittest.TestCase):
    def test_every_code_point(self):
        tables = None  # default interpreter tables
        for cp in range(0x110000):
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            got = nfc([(cp, ((0, 0, 1),))])
            self.assertEqual(got, unicodedata.normalize("NFC", ch), hex(cp))

    def test_random_sequences(self):
        rng = random.Random(20240501)
        pool = (list("aeiou nAEIOU") + [chr(c) for c in range(0x300, 0x370)]
                + ["ཱ", "ི", "ཱི", "ྵ", "ྐྵ", "क़",
                   "ि", "ो", "가", "각", "ᄀ", "ᅡ",
                   "ᆨ", "中", "文"])
        for _ in range(20000):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 15)))
            got = nfc([(ord(ch), ((0, 0, 1),)) for ch in text])
            self.assertEqual(got, unicodedata.normalize("NFC", text),
                             [hex(ord(c)) for c in text])

    def test_hangul_and_tibetan(self):
        cases = ["각", "ཱི", "ཱི", "ྵྐྵ",
                 "Á̴", "Á̴"]
        for text in cases:
            got = nfc([(ord(ch), ()) for ch in text])
            self.assertEqual(got, unicodedata.normalize("NFC", text), text)

    def test_working_memory_does_not_change_text(self):
        rng = random.Random(7)
        text = "a" + "".join(rng.choice("̴̛̀́̈")
                             for _ in range(20000))
        pairs = [(ord(ch), ((0, i, 1),)) for i, ch in enumerate(text)]
        expected = unicodedata.normalize("NFC", text)
        for limit in (0, 256, 4096, 1 << 20):
            got = "".join(chr(cp) for cp, _ in normalize_pairs(pairs, working_memory=limit))
            self.assertEqual(got, expected, "limit=%d" % limit)


if __name__ == "__main__":
    unittest.main()
