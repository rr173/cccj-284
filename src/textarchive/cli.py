"""Command line interface: ``textarchive``.

Commands::

    textarchive build MANIFEST -o ARCHIVE [--working-memory BYTES] [--spill-dir DIR]
    textarchive info ARCHIVE [--json]
    textarchive text ARCHIVE DOCUMENT [--start N] [--end M]
    textarchive spans ARCHIVE DOCUMENT START END        # normalized -> original
    textarchive hits ARCHIVE DOCUMENT FRAGMENT START END # original -> normalized
    textarchive verify ARCHIVE [--jobs N]              # full consistency check

Exit status is 0 on success, 1 on a problem with the input/data, 2 on a usage
or I/O error.  Errors are written to stderr, results to stdout.
"""

import argparse
import json
import os
import sys

from .archive import ArchiveError
from .build import BuildError, ManifestError, build_archive
from .source import FragmentError
from .store import Archive
from .unicode_data import builtin_tables
from .utf8 import Utf8Error


def _fail(message, code=2):
    sys.stderr.write("textarchive: %s\n" % message)
    return code


def _json_dump(obj, stream):
    json.dump(obj, stream, ensure_ascii=False, sort_keys=True)
    stream.write("\n")


def _span_dict(span):
    out = {"fragment": span["fragment"], "offset": span["offset"], "length": span["length"]}
    return out


def cmd_build(args):
    try:
        stats = build_archive(
            args.manifest,
            args.output,
            working_memory=args.working_memory,
            spill_directory=args.spill_dir,
            chunk_size=args.chunk_size,
            jobs=args.jobs,
        )
    except (ManifestError, BuildError, FragmentError) as exc:
        return _fail(str(exc), 1)
    except Utf8Error as exc:
        return _fail(exc.message, 1)
    except OSError as exc:
        return _fail(str(exc), 2)
    payload = stats.to_dict()
    payload["archive"] = os.path.abspath(args.output)
    payload["unicode_version"] = builtin_tables().version
    if args.json:
        _json_dump(payload, sys.stdout)
    else:
        print("archive: %s" % payload["archive"])
        print("unicode version: %s" % payload["unicode_version"])
        print("normalization: NFC")
        print("documents: %d" % payload["documents"])
        print("fragments: %d" % payload["fragments"])
        print("source bytes: %d" % payload["source_bytes"])
        print("NFC code points: %d" % payload["nfc_code_points"])
        print("provenance spans: %d" % payload["provenance_spans"])
        print("reverse index rows: %d" % payload["reverse_index_rows"])
    return 0


def cmd_info(args):
    with Archive(args.archive) as archive:
        from .query import document_info, list_documents
        if args.document:
            payload = {"document": args.document}
            payload.update(document_info(archive.db, args.document))
        else:
            payload = {
                "archive": os.path.abspath(args.archive),
                "unicode_version": archive.unicode_version,
                "normalization": archive.meta.get("normalization"),
                "meta": archive.meta,
                "documents": list_documents(archive.db),
            }
    if args.json:
        _json_dump(payload, sys.stdout)
    else:
        if args.document:
            print("document: %s" % payload["document"])
            for key in ("fragment_count", "byte_count", "cp_count", "sha256_bytes",
                        "sha256_text"):
                print("%s: %s" % (key, payload[key]))
        else:
            print("archive: %s" % payload["archive"])
            print("unicode version: %s" % payload["unicode_version"])
            print("normalization: %s" % payload["normalization"])
            for doc in payload["documents"]:
                print("  %-24s %8d bytes  %8d code points  %d fragments" % (
                    doc["document"], doc["byte_count"], doc["cp_count"],
                    doc["fragment_count"]))
    return 0


def cmd_text(args):
    with Archive(args.archive) as archive:
        from .archive import read_text
        from .query import document_info
        try:
            info = document_info(archive.db, args.document)
        except KeyError as exc:
            return _fail(str(exc.args[0]), 1)
        start = 0 if args.start is None else args.start
        end = info["cp_count"] if args.end is None else args.end
        if start < 0 or end < start or end > info["cp_count"]:
            return _fail("code point range [%d, %d) out of bounds (document has %d)"
                         % (start, end, info["cp_count"]), 1)
        text = read_text(archive.db, _doc_number(archive.db, args.document), start, end)
    if args.json:
        _json_dump({"document": args.document, "start": start, "end": end,
                    "text": text}, sys.stdout)
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _doc_number(db, name):
    row = db.execute("SELECT doc FROM documents WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError(name)
    return row[0]


def cmd_spans(args):
    from .query import cp_provenance
    with Archive(args.archive) as archive:
        try:
            items = cp_provenance(archive.db, args.document, args.start, args.end)
        except (KeyError, ValueError) as exc:
            return _fail(exc.args[0] if isinstance(exc, KeyError) else str(exc), 1)
    payload = {
        "document": args.document,
        "range": [args.start, args.end],
        "code_points": [
            {"cp": item["cp"], "char": item["char"],
             "spans": [_span_dict(s) for s in item["spans"]]}
            for item in items
        ],
    }
    if args.json:
        _json_dump(payload, sys.stdout)
    else:
        for item in payload["code_points"]:
            spans = ", ".join("%s@%d+%d" % (s["fragment"], s["offset"], s["length"])
                              for s in item["spans"])
            print("%d\tU+%04X\t%s\t%s" % (item["cp"], ord(item["char"]),
                                          item["char"], spans))
    return 0


def cmd_hits(args):
    from .query import origin_hits
    with Archive(args.archive) as archive:
        try:
            items = origin_hits(archive.db, args.document, args.fragment,
                                args.start, args.end)
        except (KeyError, ValueError) as exc:
            return _fail(exc.args[0] if isinstance(exc, KeyError) else str(exc), 1)
    payload = {
        "document": args.document,
        "fragment": args.fragment,
        "range": [args.start, args.end],
        "code_points": [
            {"cp": item["cp"], "char": item["char"],
             "spans": [_span_dict(s) for s in item["spans"]]}
            for item in items
        ],
    }
    if args.json:
        _json_dump(payload, sys.stdout)
    else:
        for item in payload["code_points"]:
            spans = ", ".join("%s@%d+%d" % (s["fragment"], s["offset"], s["length"])
                              for s in item["spans"])
            print("%d\tU+%04X\t%s\t%s" % (item["cp"], ord(item["char"]),
                                          item["char"], spans))
    return 0


def cmd_verify(args):
    from .verify import verify_archive
    try:
        report = verify_archive(args.archive, sample_limit=args.sample_limit,
                                jobs=args.jobs)
    except ArchiveError as exc:
        return _fail(str(exc), 1)
    if args.json:
        _json_dump(report, sys.stdout)
    else:
        print("archive: %s" % report["archive"])
        print("unicode version: %s" % report["unicode_version"])
        print("documents: %d" % report["documents"])
        print("checked: text bytes, NFC form, provenance coverage, reverse index")
        for doc in report["document_reports"]:
            status = "ok" if doc["ok"] else "FAIL"
            print("  %-24s %s  (%d code points)" % (doc["document"], status, doc["cp_count"]))
        print("result: %s" % ("ok" if report["ok"] else "FAILED"))
    return 0 if report["ok"] else 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="textarchive",
        description="Build and query an offline NFC text archive with byte provenance.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build an archive from a manifest")
    p.add_argument("manifest")
    p.add_argument("-o", "--output", required=True, dest="output")
    p.add_argument("--working-memory", type=int, default=4 << 20, dest="working_memory",
                   help="in-memory working buffer limit in bytes (default: 4 MiB)")
    p.add_argument("--spill-dir", default=None, dest="spill_dir",
                   help="directory for disk spill files (default: system temp)")
    p.add_argument("--chunk-size", type=int, default=1 << 16, dest="chunk_size",
                   help="fragment read size in bytes (default: 65536)")
    p.add_argument("--jobs", type=int, default=1, help="reserved; must be 1")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("info", help="show archive and document summaries")
    p.add_argument("archive")
    p.add_argument("--document", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("text", help="print normalized text (optionally a code point range)")
    p.add_argument("archive")
    p.add_argument("document")
    p.add_argument("--start", type=int, default=None)
    p.add_argument("--end", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("spans", help="original byte spans for normalized code points")
    p.add_argument("archive")
    p.add_argument("document")
    p.add_argument("start", type=int)
    p.add_argument("end", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_spans)

    p = sub.add_parser("hits", help="normalized code points affected by a fragment byte range")
    p.add_argument("archive")
    p.add_argument("document")
    p.add_argument("fragment")
    p.add_argument("start", type=int)
    p.add_argument("end", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_hits)

    p = sub.add_parser("verify", help="re-check an archive end to end")
    p.add_argument("archive")
    p.add_argument("--sample-limit", type=int, default=200, dest="sample_limit")
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ArchiveError as exc:
        return _fail(str(exc), 1)
    except KeyError as exc:
        return _fail(str(exc.args[0]) if exc.args else str(exc), 1)
    except (ValueError, OSError) as exc:
        return _fail(str(exc), 2)
    except BrokenPipeError:  # pragma: no cover - piping to head et al.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
