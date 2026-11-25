"""textarchive -- offline, provenance-preserving NFC text archive.

The archive stores, for every document:

* the complete NFC (Unicode Normalization Form C) text,
* for every normalized code point, the exact byte ranges (fragment id +
  zero-based offset inside that fragment) of the original bytes it came from,
* a reverse index that answers "which normalized code points are affected by
  this byte range of this fragment?" without scanning the whole document.

The package is pure Python + sqlite3 (both in the standard library); nothing
needs to be downloaded or compiled.
"""

__version__ = "1.0.0"

#: identifier written into the archive meta table
ARCHIVE_FORMAT = "textarchive/1"
