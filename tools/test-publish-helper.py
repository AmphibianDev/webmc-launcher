#!/usr/bin/env python3
"""Checks on the published local-helper page, and on the helper it hands out.

`local.html` and `helper/webmc-helper.zip` are generated from the `webmc-helper`
repository, so the thing that can go wrong is silent drift: the page people open stops
matching the helper they download. These checks are the guard.

    python3 tools/test-publish-helper.py
"""

import re
import sys
import zipfile
from pathlib import Path

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
    check("and says the archive's file count",
          "files in the archive" in proc.stdout, proc.stdout.strip()[-80:])

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
    check("explains the Local Network Access prompt",
          "LocalNetworkAccessBlockedForUrls" in page and "Local network access" in page)
    check("knows a second tab is not a failure",
          "already gone from /tmp" in page)
    check("does not carry the helper's injected token block", "window.__WEBMC__ =" not in page)

    print("== the archive a visitor downloads")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        check("unpacks into one webmc-helper/ directory",
              all(n.startswith("webmc-helper/") for n in names), str(names[:2]))
        check("contains the helper itself",
              "webmc-helper/helper/webmc_helper.py" in names)
        check("contains the page the helper serves",
              "webmc-helper/page/index.html" in names)
        for exe in ("webmc-helper/helper/start-helper.command",
                    "webmc-helper/tests/hello-mod/build.sh"):
            info = zf.getinfo(exe) if exe in names else None
            mode = (info.external_attr >> 16) & 0o777 if info else 0
            check("%s is executable inside the zip (archive utility honours this)" % Path(exe).name,
                  mode & 0o111 != 0, "mode %o" % mode)
        check("leaves build output out of the archive",
              not any("/out/" in n or n.endswith(".pyc") for n in names))
        helper = zf.read("webmc-helper/helper/webmc_helper.py").decode()
        check("the helper still allows the site's origin",
              "github" in helper and "ORIGIN_PATTERNS" in helper)
        check("the helper still answers the private-network preflight",
              "Access-Control-Allow-Private-Network" in helper)
        check("the helper still deletes its sandbox on a signal",
              "install_signal_handlers" in helper)
        served = zf.read("webmc-helper/page/index.html").decode()
        # The published page is the shipped page with one generated comment inserted after
        # the doctype. Stripping it must give back exactly the file in the archive, which is
        # the actual anti-drift claim: one page, not two that look alike.
        note = re.compile(r"<!-- This file is generated from .*?-->\n", re.S)
        check("the published page is the shipped page plus only the generated note",
              note.sub("", page, count=1) == served,
              "published %d bytes, shipped %d bytes" % (len(note.sub("", page, count=1)), len(served)))
        check("the shipped page is what publish-helper.py would rebuild",
              served == (SITE.parent / "webmc-helper" / "page" / "index.html").read_text()
              if (SITE.parent / "webmc-helper" / "page" / "index.html").exists()
              else "webmc-helper/ is not checked out beside this repository",
              "")

    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
