#!/usr/bin/env python3
"""Publish the local-helper page and the helper itself into this site.

`webmc-helper/` is where the helper is written, tested and run. This repository is what
GitHub Pages serves. Pages cannot start a game on a visitor's computer, so the hosted page
needs two things from there:

    local.html                  the launcher page, identical to the one the helper serves
    helper/webmc-helper.zip     the helper itself, so a visitor can get it in one click

Both are copied verbatim here, and `--check` fails if the site has gone stale, so the page
people open and the page the helper serves cannot drift apart.

    python3 tools/publish-helper.py            # copy + pack
    python3 tools/publish-helper.py --check    # exit 1 if anything is out of date
"""

import argparse
import hashlib
import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE.parent
DEFAULT_SRC = SITE.parent / "webmc-helper"

PAGE_SRC = "page/index.html"
PAGE_DEST = "local.html"
ZIP_DEST = "helper/webmc-helper.zip"

# A fixed timestamp and sorted members make the archive byte-identical run to run, which
# is what lets --check compare it at all.
STAMP = (2026, 1, 1, 0, 0, 0)
SKIP_DIRS = {"__pycache__", ".git", "out", ".mypy_cache"}
SKIP_SUFFIX = (".pyc", ".pyo", ".part", ".DS_Store")


def wanted(path):
    parts = path.parts
    if any(p in SKIP_DIRS for p in parts):
        return False
    return not path.name.endswith(SKIP_SUFFIX)


def safe_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes() if path.exists() else None
    path.write_bytes(data)
    return old != data


def build_page(src_dir):
    text = (src_dir / PAGE_SRC).read_text()
    note = ("<!-- This file is generated from webmc-helper/page/index.html by\n"
            "     tools/publish-helper.py. Edit it there, then run that tool. -->\n")
    # after the doctype, never before it: anything above <!DOCTYPE html> puts a browser
    # into quirks mode.
    if text.lstrip().lower().startswith("<!doctype"):
        head, _, tail = text.partition("\n")
        return (head + "\n" + note + tail).encode()
    return (note + text).encode()


def build_zip(src_dir):
    import io
    buf = io.BytesIO()
    files = sorted(p for p in src_dir.rglob("*") if p.is_file() and wanted(p.relative_to(src_dir)))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            rel = path.relative_to(src_dir).as_posix()
            info = zipfile.ZipInfo("webmc-helper/" + rel, date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16
            zf.writestr(info, path.read_bytes())
    return buf.getvalue(), [f.relative_to(src_dir).as_posix() for f in files]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=str(DEFAULT_SRC),
                    help="the webmc-helper directory (default %(default)s)")
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if the site is out of date")
    args = ap.parse_args(argv)

    src_dir = Path(args.source).resolve()
    if not (src_dir / PAGE_SRC).exists():
        print("cannot find %s — pass --source <path to webmc-helper>" % (src_dir / PAGE_SRC))
        return 2

    page = build_page(src_dir)
    blob, files = build_zip(src_dir)

    page_target = SITE / PAGE_DEST
    zip_target = SITE / ZIP_DEST

    problems = []
    for target, data, label in ((page_target, page, PAGE_DEST), (zip_target, blob, ZIP_DEST)):
        current = target.read_bytes() if target.exists() else None
        if current == data:
            print("  ok      %-28s %8d bytes" % (label, len(data)))
            continue
        if args.check:
            state = "missing" if current is None else "stale (%d bytes on disk)" % len(current)
            problems.append("%s is %s" % (label, state))
            print("  STALE   %-28s %s" % (label, state))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print("  wrote   %-28s %8d bytes" % (label, len(data)))

    if args.check:
        if problems:
            print("\nrun: python3 tools/publish-helper.py")
            return 1
        print("\nthe site matches webmc-helper (%d files in the archive)" % len(files))
        return 0

    digest = hashlib.sha256(blob).hexdigest()[:16]
    print("\npage:  %s" % PAGE_DEST)
    print("zip:   %s  (%d files, sha256 %s…)" % (ZIP_DEST, len(files), digest))
    print("\nnext: git add %s %s && git commit && git push" % (PAGE_DEST, ZIP_DEST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
