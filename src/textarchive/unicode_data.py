"""Unicode character data used by the normalizer.

The data is derived at build time from the Python interpreter's own
``unicodedata`` module -- the same data that defines "NFC" for the archive --
and embedded into an archive as a versioned blob so that reopening does not
depend on the interpreter's Unicode version.

If you *do* want to normalise with the interpreter's tables (used only for the
self-check of the pristine-vs-archive build path), ``tables_from_unicodedata``
rebuilds them on the fly.
"""

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Hangul constants (Unicode 3.12, section 3.12 "Conjoining Jamo Behavior")
# ---------------------------------------------------------------------------
SBASE = 0xAC00
LBASE = 0x1100
VBASE = 0x1161
TBASE = 0x11A7
LCOUNT = 19
VCOUNT = 21
TCOUNT = 28
NCOUNT = VCOUNT * TCOUNT
SCOUNT = LCOUNT * NCOUNT


class UnicodeTables:
    """Immutable per-codepoint data needed to compute NFC.

    Attributes
    ----------
    version : str
        The Unicode version the tables were derived from (``unicodedata.unidata_version``).
    ccc : dict[int, int]
        Canonical combining class, for the uncharted classes only (0 otherwise).
    decomp : dict[int, tuple[int, ...]]
        Canonical (not compatibility) full decompositions, only for code points
        that are not Hangul syllables (Hangul is handled algorithmically).
    pairs : dict[tuple[int, int], int]
        Primary composites usable for NFC: canonical decomposition of length two
        whose NFC equals the character itself (this filter removes full
        composition exclusions -- e.g. U+0958, U+0F73, U+0344, U+2ADC).
    """

    __slots__ = ("version", "ccc", "decomp", "pairs")

    def __init__(
        self,
        version: str,
        ccc: Dict[int, int],
        decomp: Dict[int, Tuple[int, ...]],
        pairs: Dict[Tuple[int, int], int],
    ) -> None:
        self.version = version
        self.ccc = ccc
        self.decomp = decomp
        self.pairs = pairs

    # -- serialization ------------------------------------------------------
    def to_blob(self) -> bytes:
        """Serialize to a compact text blob (ASCII only, deterministic)."""
        lines: List[str] = ["textarchive-tables 1", "version " + self.version]
        lines.append("ccc " + ",".join("%X:%X" % (cp, cc) for cp, cc in sorted(self.ccc.items())))
        lines.append(
            "decomp "
            + ",".join(
                "%X=" % cp + "+".join("%X" % x for x in parts)
                for cp, parts in sorted(self.decomp.items())
            )
        )
        lines.append(
            "pairs "
            + ",".join("%X+%X=%X" % (a, b, c) for (a, b), c in sorted(self.pairs.items()))
        )
        return ("\n".join(lines) + "\n").encode("ascii")

    @classmethod
    def from_blob(cls, blob: bytes) -> "UnicodeTables":
        text = blob.decode("ascii")
        version = "unknown"
        ccc: Dict[int, int] = {}
        decomp: Dict[int, Tuple[int, ...]] = {}
        pairs: Dict[Tuple[int, int], int] = {}
        for line in text.splitlines():
            if not line:
                continue
            tag, _, rest = line.partition(" ")
            if tag == "version":
                version = rest
            elif tag == "ccc" and rest:
                for item in rest.split(","):
                    cp_s, cc_s = item.split(":")
                    ccc[int(cp_s, 16)] = int(cc_s, 16)
            elif tag == "decomp" and rest:
                for item in rest.split(","):
                    cp_s, _, parts_s = item.partition("=")
                    decomp[int(cp_s, 16)] = tuple(int(x, 16) for x in parts_s.split("+"))
            elif tag == "pairs" and rest:
                for item in rest.split(","):
                    lhs, _, cp_s = item.partition("=")
                    a_s, b_s = lhs.split("+")
                    pairs[(int(a_s, 16), int(b_s, 16))] = int(cp_s, 16)
            elif tag == "textarchive-tables":
                continue
            else:
                raise ValueError("unknown table line: %r" % line[:40])
        return cls(version, ccc, decomp, pairs)

    # -- algorithm ----------------------------------------------------------
    def decomp_cp(self, cp: int) -> List[int]:
        """Full canonical decomposition of a single code point."""
        if SBASE <= cp < SBASE + SCOUNT:
            s = cp - SBASE
            l = LBASE + s // NCOUNT
            v = VBASE + (s % NCOUNT) // TCOUNT
            t = TBASE + s % TCOUNT
            return [l, v, t] if t != TBASE else [l, v]
        parts = self.decomp.get(cp)
        if parts is None:
            return [cp]
        out: List[int] = []
        for x in parts:
            out.extend(self.decomp_cp(x))
        return out

    def compose_pair(self, a: int, b: int) -> Optional[int]:
        """Primary composite of ``a`` and ``b`` in NFC, or None."""
        if LBASE <= a < LBASE + LCOUNT and VBASE <= b < VBASE + VCOUNT:
            return SBASE + ((a - LBASE) * VCOUNT + (b - VBASE)) * TCOUNT
        if (
            SBASE <= a < SBASE + SCOUNT
            and (a - SBASE) % TCOUNT == 0
            and TBASE < b < TBASE + TCOUNT
        ):
            return a + (b - TBASE)
        return self.pairs.get((a, b))


def tables_from_unicodedata() -> UnicodeTables:
    """Derive NFC tables from this interpreter's ``unicodedata`` module."""
    ccc: Dict[int, int] = {}
    decomp: Dict[int, Tuple[int, ...]] = {}
    candidates: List[Tuple[int, int, int, int]] = []
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        cc = unicodedata.combining(ch)
        if cc:
            ccc[cp] = cc
        d = unicodedata.decomposition(ch)
        if not d or d.startswith("<"):
            continue
        parts = tuple(int(x, 16) for x in d.split())
        decomp[cp] = parts
        if len(parts) == 2:
            candidates.append((parts[0], parts[1], cp, 0))
    pairs: Dict[Tuple[int, int], int] = {}
    for a, b, cp, _ in candidates:
        if _nfc_selfcheck(a, b) == cp:
            pairs[(a, b)] = cp
    return UnicodeTables(unicodedata.unidata_version, ccc, decomp, pairs)


def _nfc_selfcheck(a: int, b: int) -> int:
    """NFC of the two-code-point string ``a b`` as computed by unicodedata.

    Used only while deriving ``pairs`` so that full composition exclusions are
    dropped, exactly as the standard requires.
    """
    composed = unicodedata.normalize("NFC", chr(a) + chr(b))
    return ord(composed) if len(composed) == 1 else -1


_TABLES: Optional[UnicodeTables] = None


def builtin_tables() -> UnicodeTables:
    """Tables derived from the running interpreter (cached)."""
    global _TABLES
    if _TABLES is None:
        _TABLES = tables_from_unicodedata()
    return _TABLES
