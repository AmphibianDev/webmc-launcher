#!/usr/bin/env python3
"""Checks on the published local-helper page, and on the helper it hands out.

`local.html` and `helper/webmc-helper.zip` are generated from the `webmc-helper`
repository, so the thing that can go wrong is silent drift: the page people open stops
matching the helper they download. These checks are the guard.

    python3 tools/test-publish-helper.py
"""

import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

APP = "WebMC Helper.app"
BUNDLE_ID = "dev.amphibian.webmc-helper"
EXPECTED = sorted([
    "README.md",
    APP + "/Contents/Info.plist",
    APP + "/Contents/MacOS/run",
    APP + "/Contents/Resources/README.md",
    APP + "/Contents/Resources/helper/start-helper.command",
    APP + "/Contents/Resources/helper/webmc_helper.py",
    APP + "/Contents/Resources/page/index.html",
])

HERE = Path(__file__).resolve().parent
SITE = HERE.parent

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  PASS  %s" % label)
    else:
        failed += 1
        print("  FAIL  %s%s" % (label, ("  -- " + detail) if detail else ""))


def main():
    import subprocess
    print("== the published page matches the helper it was generated from")
    proc = subprocess.run([sys.executable, str(HERE / "publish-helper.py"), "--check"],
                          capture_output=True, text=True)
    check("publish-helper.py --check exits 0", proc.returncode == 0,
          (proc.stdout + proc.stderr).strip().splitlines()[-1] if proc.stdout else "")
    check("and reports what it compared",
          "the site matches webmc-helper" in proc.stdout, proc.stdout.strip()[-80:])

    page_path = SITE / "local.html"
    zip_path = SITE / "helper" / "webmc-helper.zip"
    check("local.html exists", page_path.exists())
    check("helper/webmc-helper.zip exists", zip_path.exists())
    if not page_path.exists() or not zip_path.exists():
        return 1

    page = page_path.read_text()
    print("== the page a visitor gets")
    check("does not begin with a comment before the doctype (quirks mode)",
          page.lstrip().lower().startswith("<!doctype html>"))
    check("is self-contained: no external scripts or stylesheets",
          not re.search(r"<script[^>]+\bsrc=", page) and not re.search(r"<link[^>]+stylesheet", page)
          and "@import" not in page)
    check("defaults to the helper on this machine",
          "http://127.0.0.1:7645" in page)
    check("offers the download at a relative path that works from a project page",
          'href="helper/webmc-helper.zip"' in page)
    check("explains the Local Network Access prompt, by name and by policy",
          "LocalNetworkAccessBlockedForUrls" in page
          and re.search(r"[Ll]ocal\s*network\s*access", page) is not None,
          "the page must name the permission and the policy that can block it")
    check("knows a second tab is not a failure",
          "already gone from /tmp" in page)
    check("does not carry the helper's injected token block", "window.__WEBMC__ =" not in page)

    print("== the archive a visitor downloads")
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
        check("one app plus a README, and nothing else", names == EXPECTED, str(names))
        plist = zf.read(APP + "/Contents/Info.plist").decode()
        check("the app is a real bundle: launcher named, no dock icon",
              "<key>CFBundleExecutable</key><string>run</string>" in plist
              and BUNDLE_ID in plist and "<key>LSUIElement</key><true/>" in plist)
        run = zf.getinfo(APP + "/Contents/MacOS/run")
        mode = (run.external_attr >> 16) & 0o777
        check("the app's launcher is executable inside the archive", bool(mode & 0o111),
              "mode %o" % mode)
        command = zf.getinfo(APP + "/Contents/Resources/helper/start-helper.command")
        check("the command-line path stays executable too",
              ((command.external_attr >> 16) & 0o111) != 0)
        check("no tests, templates or build output anywhere in the archive",
              not any("/tests/" in n or "/out/" in n or ".github" in n or n.endswith(".pyc")
                      for n in names))
        helper = zf.read(APP + "/Contents/Resources/helper/webmc_helper.py").decode()
        check("the helper still allows the site's origin",
              "ORIGIN_PATTERNS" in helper and "github" in helper)
        check("the helper still answers the private-network preflight",
              "Access-Control-Allow-Private-Network" in helper)
        check("the helper still deletes its sandbox on a signal", "install_signal_handlers" in helper)
        check("the helper can install itself without a terminal",
              "--install" in helper and "launchctl" in helper)
        served = zf.read(APP + "/Contents/Resources/page/index.html").decode()
        note = re.compile(r"<!-- This file is generated from .*?-->\n", re.S)
        check("the published page is the shipped page plus only the generated note",
              note.sub("", page, count=1) == served,
              "published %d bytes, shipped %d bytes"
              % (len(note.sub("", page, count=1)), len(served)))

    if sys.platform == "darwin":
        # The failure this check exists for: Apple's own unarchiver (ditto, and therefore
        # double-clicking the zip in Finder) dropped the executable bit, and macOS then refused
        # to launch the app at all - "Launch failed. Launchd job spawn failed". Python's zipfile
        # produces an archive that suffers from it; the system zip does not. Measured, so tested.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(["ditto", "-x", "-k", str(zip_path), tmp],
                                    capture_output=True, text=True)
            check("ditto can unpack the archive", result.returncode == 0,
                  (result.stderr or "").strip()[:120])
            run_path = Path(tmp) / APP / "Contents/MacOS/run"
            unpacked = run_path.stat().st_mode & 0o777 if run_path.exists() else 0
            check("Apple's unarchiver keeps the app launchable (exec bit survives)",
                  bool(unpacked & 0o111), "mode %o" % unpacked)

    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
