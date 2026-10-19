#!/usr/bin/env python3
"""Publish the local-helper page and the helper itself into this site.

`webmc-helper/` is where the helper is written, tested and run. This repository is what
GitHub Pages serves. Pages cannot start a game on a visitor's computer, so the hosted page
needs two things from there:

    local.html                  the launcher page, identical to the one the helper serves
    helper/webmc-helper.zip     the helper, as a Finder-launchable app

The download is one file, and it installs nothing: `webmc-helper.command`, a shell script that
carries the whole helper and the launcher page inside it. It has to be inside a zip - macOS gives
a downloaded script no executable bit, so a bare download cannot be double-clicked, and a zip is
the only thing that carries the bit across. No app bundle, no login item, no repository
furniture, and `--check` fails if the site drifts from the source either way.

    python3 tools/publish-helper.py            # copy + pack
    python3 tools/publish-helper.py --check    # exit 1 if anything is out of date
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE.parent
DEFAULT_SRC = SITE.parent / "webmc-helper"

PAGE_SRC = "page/index.html"
PAGE_DEST = "local.html"
ZIP_DEST = "helper/webmc-helper.zip"

APP = "WebMC Helper.app"
BUNDLE_ID = "dev.amphibian.webmc-helper"

# What goes into the app, and where: (source path in webmc-helper, path inside the app, mode)
APP_FILES = [
    ("helper/webmc_helper.py", "Contents/Resources/helper/webmc_helper.py", 0o755),
    ("helper/start-helper.command", "Contents/Resources/helper/start-helper.command", 0o755),
    ("page/index.html", "Contents/Resources/page/index.html", 0o644),
     ("README.md", "Contents/Resources/README.md", 0o644),
]

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>WebMC Helper</string>
  <key>CFBundleDisplayName</key><string>WebMC Helper</string>
  <key>CFBundleIdentifier</key><string>%s</string>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>%s</string>
  <key>CFBundleVersion</key><string>%s</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""

# The app's only job: run the helper's installer, and say something in a dialog if it cannot.
# No Terminal is involved anywhere - that is the point of shipping an .app.
RUN_SCRIPT = """#!/bin/bash
# Launched by Finder (or by launchd). There is no terminal attached, so output goes to a log
# and anything the visitor must see goes to a dialog.
set -u
HERE="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/Library/Logs/webmc-helper.log"
mkdir -p "$(dirname "$LOG")"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  osascript -e 'display dialog "WebMC Helper needs Python 3, which macOS ships with its command line tools.\\n\\nRun this in Terminal once:\\n\\n    xcode-select --install\\n\\nthen open WebMC Helper again." buttons {"OK"} default button 1 with title "WebMC Helper"' >/dev/null 2>&1
  exit 1
fi

"$PY" -u "$HERE/helper/webmc_helper.py" --install >>"$LOG" 2>&1
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  osascript -e "display dialog \\"WebMC Helper could not start (status $STATUS).\\\\n\\\\nWhat it said is in:\\\\n$LOG\\" buttons {\\"OK\\"} default button 1 with title \\"WebMC Helper\\"" >/dev/null 2>&1
fi
exit "$STATUS"
"""

STAMP = (2026, 1, 1, 0, 0, 0)


def helper_version(src_dir):
    for line in (src_dir / "helper" / "webmc_helper.py").read_text().splitlines():
        if line.startswith("VERSION = "):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def safe_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes() if path.exists() else None
    path.write_bytes(data)
    return old != data


def build_page(src_dir):
    text = (src_dir / PAGE_SRC).read_text()
    note = ("<!-- This file is generated from webmc-helper/page/index.html by\n"
            "     tools/publish-helper.py. Edit it there, then run that tool. -->\n")
    # after the doctype, never before it: anything above <!DOCTYPE html> is quirks mode.
    if text.lstrip().lower().startswith("<!doctype"):
        head, _, tail = text.partition("\n")
        return (head + "\n" + note + tail).encode()
    return (note + text).encode()


SINGLE_FILE = "webmc-helper.command"

HEADER = """#!/bin/bash
# webmc-helper - one file, and nothing installed.
#
# Double-click it. It starts in the background, closes this window itself, and waits for you to
# press Play on the launcher page. The game is downloaded into a throwaway folder under /tmp and
# that folder is deleted when the session ends - no daemon, no login item, nothing to uninstall.
#
#   press Play at:   https://amphibiandev.github.io/webmc-launcher/local.html
#   stop it with:    pkill -f webmc_helper        (or leave it; it exits by itself when idle)
#   watch it work:   WEBMC_FOREGROUND=1 ./webmc-helper.command
#
# Needs python3, which macOS provides with its command line tools:  xcode-select --install
set -u
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "This needs python3. Run:  xcode-select --install   then double-click it again."
  read -r -p "press return to close " _
  exit 1
fi

# The helper is carried below as a heredoc. It is written out once (bash cannot put two heredocs
# in one if/else) and the copy is deleted again the moment python has read it, so the only thing
# that outlives a session is whatever is still running.
SOURCE="${TMPDIR:-/tmp}/webmc-helper-$$.py"
cat > "$SOURCE" <<'WEBMC_HELPER_SOURCE'
"""
FOOTER = """WEBMC_HELPER_SOURCE

export WEBMC_SELF_DELETE="$SOURCE"
ARGS=("--open" "--idle-exit" "1800")
PAGE="${WEBMC_PAGE_URL-https://amphibiandev.github.io/webmc-launcher/local.html}"
[ -n "$PAGE" ] && ARGS+=("--page-url" "$PAGE")
LOGFILE="${TMPDIR:-/tmp}/webmc-helper-run.log"

if [ "${WEBMC_FOREGROUND:-0}" != "0" ]; then
  exec "$PY" -u "$SOURCE" "${ARGS[@]}" "$@"
fi

nohup "$PY" -u "$SOURCE" "${ARGS[@]}" "$@" >"$LOGFILE" 2>&1 &
HELPER_PID=$!
sleep 2
if kill -0 "$HELPER_PID" 2>/dev/null || curl -s -o /dev/null -m 3 http://127.0.0.1:7645/api/hello; then
  echo "webmc-helper is running in the background - this window closes itself."
  echo "The launcher page should be opening in your browser; press Play there."
else
  echo "It did not start. What it said:"
  tail -20 "$LOGFILE" 2>/dev/null
  read -r -p "press return to close " _
fi
exit 0
"""


def build_single_file(src_dir):
    """One shell script that carries the whole helper and the page it serves.

    macOS cannot run a downloaded script at all (no executable bit survives a download) and an
    .app is a directory, so "one file" means this: a single .command inside a zip, because the zip
    is the only thing that carries the executable bit to the other side.
    """
    import base64
    source = (src_dir / "helper" / "webmc_helper.py").read_text()
    if "_EMBEDDED_PAGE = \"\"" not in source:
        raise SystemExit("helper/webmc_helper.py no longer has an empty _EMBEDDED_PAGE to fill")
    page = (src_dir / "page" / "index.html").read_bytes()
    source = source.replace('_EMBEDDED_PAGE = ""',
                            '_EMBEDDED_PAGE = "%s"' % base64.b64encode(page).decode(), 1)
    return (HEADER + source + FOOTER).encode()


def stage_app(src_dir, stage):
    """Lay the download out on disk. Returns the list of relative paths staged."""
    target = stage / SINGLE_FILE
    target.write_bytes(build_single_file(src_dir))
    target.chmod(0o755)
    return [SINGLE_FILE]


def zip_cli():
    found = shutil.which("zip")
    if not found:
        raise SystemExit("this needs the `zip` command (Info-ZIP): Apple's own unarchiver "
                         "only restores executable bits from archives that zip writes")
    return found


def build_zip(src_dir, out_path):
    """Build the download with the system `zip`.

    Python's zipfile writes a perfectly valid archive that Apple's unarchiver (ditto, and so
    double-clicking in Finder) extracts *without* the executable bit - measured, twice - which
    leaves an .app that macOS refuses to launch ("Launch failed / Launchd job spawn failed").
    The same files archived by Info-ZIP survive the round trip, so the archive is built by the
    system's zip. --check therefore compares a manifest rather than raw bytes.
    """
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        staged = stage_app(src_dir, stage)
        if out_path.exists():
            out_path.unlink()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([zip_cli(), "-q", "-r", "-y", str(out_path)] + staged,
                                cwd=stage, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit("zip failed: %s" % (result.stderr or result.stdout).strip())
    if not out_path.exists():
        raise SystemExit("zip produced nothing at %s" % out_path)
    return staged


def zip_manifest(path):
    """(name -> (mode, sha256)) for every entry, for comparing two archives meaningfully."""
    with zipfile.ZipFile(path) as zf:
        out = {}
        for info in zf.infolist():
            name = info.filename[2:] if info.filename.startswith("./") else info.filename
            payload = zf.read(info)
            out[name] = ((info.external_attr >> 16) & 0o777,
                         hashlib.sha256(payload).hexdigest())
        return out


def stage_manifest(stage):
    out = {}
    for base, _dirs, files in os.walk(stage):
        for name in files:
            full = Path(base) / name
            rel = str(full.relative_to(stage))
            out[rel] = (full.stat().st_mode & 0o777,
                        hashlib.sha256(full.read_bytes()).hexdigest())
    return out


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

    problems = []
    current_page = (SITE / PAGE_DEST).read_bytes() if (SITE / PAGE_DEST).exists() else None
    if current_page == page:
        print("  ok      %-28s %8d bytes" % (PAGE_DEST, len(page)))
    elif args.check:
        problems.append(PAGE_DEST)
        print("  STALE   %-28s %s" % (PAGE_DEST,
              "missing" if current_page is None else "differs from webmc-helper/"))
    else:
        (SITE / PAGE_DEST).parent.mkdir(parents=True, exist_ok=True)
        (SITE / PAGE_DEST).write_bytes(page)
        print("  wrote   %-28s %8d bytes" % (PAGE_DEST, len(page)))

    zip_target = SITE / ZIP_DEST
    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            staged = stage_app(src_dir, stage)
            wanted = stage_manifest(stage)
        got = zip_manifest(zip_target) if zip_target.exists() else {}
        if got == wanted:
            print("  ok      %-28s %8d files" % (ZIP_DEST, len(got)))
        else:
            missing = sorted(set(wanted) - set(got))
            extra = sorted(set(got) - set(wanted))
            changed = sorted(n for n in set(got) & set(wanted) if got[n] != wanted[n])
            problems.append(ZIP_DEST)
            print("  STALE   %-28s %s" % (ZIP_DEST,
                  "missing %s extra %s changed %s" % (missing[:3], extra[:3], changed[:3])))
    else:
        staged = build_zip(src_dir, zip_target)
        print("  wrote   %-28s %8d files" % (ZIP_DEST, len(staged)))

    if args.check:
        if problems:
            print("\nrun: python3 tools/publish-helper.py")
            return 1
        print("\nthe site matches webmc-helper")
        return 0

    names = called = None
    with zipfile.ZipFile(zip_target) as zf:
        names = zf.namelist()
    digest = hashlib.sha256(zip_target.read_bytes()).hexdigest()[:16]
    print("\npage:  %s" % PAGE_DEST)
    print("zip:   %s  (%d files, sha256 %s…)" % (ZIP_DEST, len(names), digest))
    for n in names:
        print("         %s" % n)
    print("\nnext: git add %s %s && git commit && git push" % (PAGE_DEST, ZIP_DEST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
