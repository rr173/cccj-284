"""Opening an archive and loading the data needed for normalization checks."""

from .archive import ArchiveError, check_format, connect, meta_dict
from .unicode_data import UnicodeTables


class Archive:
    """An opened archive with its metadata and Unicode tables loaded."""

    def __init__(self, path):
        self.path = path
        self.db = connect(path, "ro")
        try:
            check_format(self.db)
            self.meta = meta_dict(self.db)
            row = self.db.execute("SELECT data FROM unicode_tables WHERE id = 1").fetchone()
            if row is None:
                raise ArchiveError("archive has no Unicode tables")
            self.tables: UnicodeTables = UnicodeTables.from_blob(bytes(row[0]))
            declared = self.meta.get("unicode_version")
            if declared != self.tables.version:
                raise ArchiveError(
                    "archive Unicode version mismatch: meta says %r, tables say %r"
                    % (declared, self.tables.version))
        except BaseException:
            self.db.close()
            raise

    # -- convenience --------------------------------------------------------
    @property
    def unicode_version(self):
        return self.tables.version

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
