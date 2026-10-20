#!/usr/bin/env python3
"""Checks on the published local-helper page, and on the helper it hands out.

`local.html` and `helper/webmc-helper.zip` are generated from the `webmc-helper`
repository, so the thing that can go wrong is silent drift: the page people open stops
matching the helper they download. These checks are the guard.

    python3 tools/test-publish-helper.py
"""

import base64
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# The download installs nothing: the helper, the page it serves, its launcher and a README.
EXPECTED = ["webmc-helper.command"]

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
    def read(rel):
        return Path(__file__).resolve().parent.parent.parent.joinpath("webmc-helper", rel).read_text()

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
    check("treats an already-collected session as normal, not as a failure",
          "already collected" in page)
    # The interface deliberately has no session list: a session is stored for you when it ends, so
    # there is nothing to press. The API still keeps them, and the page still asks it.
    check("asks the helper for its sessions, and stores each one without being told to",
          "/api/sessions" in page and "sessionEnded" in page and "last-session.zip" in page)
    check("documents itself: a Documentation view with sections",
          "Documentation" in page and page.count("<h3 id=\"d") >= 6)
    check("hands the tab over instead of failing when it cannot fetch the helper",
          "function handOver" in page and "handing the tab" in page)
    check("keeps play usable with no helper, in a hosted copy",
          "Play stays usable" in page or "hands the tab" in page.lower()
          or "hands this tab" in page.lower())
    check("does not carry the helper's injected token block", "window.__WEBMC__ =" not in page)

    print("== the archive a visitor downloads")
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
        check("exactly one file, and nothing else", names == EXPECTED, str(names))
        command = zf.getinfo("webmc-helper.command")
        mode = (command.external_attr >> 16) & 0o777
        # A downloaded script gets no executable bit; the zip is the only thing that carries it,
        # so this bit is the difference between "double-click and play" and "does nothing".
        check("the single file is executable inside the archive", bool(mode & 0o111),
              "mode %o" % mode)
        check("no tests, templates or build output anywhere in the archive",
              not any("/tests/" in n or "/out/" in n or ".github" in n or n.endswith(".pyc")
                      for n in names))
        helper = zf.read("webmc-helper.command").decode()
        check("the helper still allows the site's origin",
              "ORIGIN_PATTERNS" in helper and "github" in helper)
        check("the helper still answers the private-network preflight",
              "Access-Control-Allow-Private-Network" in helper)
        check("the helper still deletes its sandbox on a signal", "install_signal_handlers" in helper)
        check("the helper can clean up after itself",
              "def cleanup" in helper and "launchctl" in helper)
        served = base64.b64decode(re.search(r'_EMBEDDED_PAGE = "([^"]*)"', helper).group(1)).decode()
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
            run_path = Path(tmp) / "webmc-helper.command"
            unpacked = run_path.stat().st_mode & 0o777 if run_path.exists() else 0
            helper = read("helper/webmc_helper.py")
    check("the helper can use a relay nobody had to deploy",
          "DEFAULT_RELAYS" in helper and "def resolve_relay" in helper)
    check("and it falls back to downloading directly when no relay answers",
          "downloading directly" in helper and '"off"' in helper or "OFF = (" in helper)
    check("the page offers automatic, never, and a relay of your own",
          all(v in page for v in ('id="relayMode"', 'value="auto"', 'value="off"',
                                  'value="custom"', 'id="relayCustomRow"')))
    check("and the page says which relay is actually in use",
          'id="relayInUse"' in page and "showRelayInUse" in page)
    check("the relay test reads what the Worker really returns",
          "j.files || j.prefixes" in page and "j.tunnel" in page)
    check("the promise is checkable: the page can see what is on the machine",
          'id="machineState"' in page and "showMachineState" in page
          and 'footprint' in read("helper/webmc_helper.py"))
    check("and can remove all of it without a terminal",
          'id="purge"' in page and "/api/purge" in page and 'def purge(' in read("helper/webmc_helper.py"))
    check("a purge covers the cache and the installed files, not just the sandbox",
          "download cache" in read("helper/webmc_helper.py")
          and '("log", "log file")' in read("helper/webmc_helper.py"))
    check("the first run opens no Terminal: it ships an app bundle",
          "build_mac_app" in pathlib.Path("tools/publish-helper.py").read_text()
          and (SITE / "helper" / "WebMC-Helper-mac.zip").exists())
    check("and the login item comes back if the helper stops",
          "<key>KeepAlive</key><true/>" in read("helper/webmc_helper.py"))
    check("a page can sit open all afternoon without the helper giving up",
          "--idle-exit 21600" in read("helper/start-helper.command"))
    check("the helper's own root is a sandbox it can identify later",
          '"role": "helper root"' in read("helper/webmc_helper.py"))
    check("Apple's unarchiver keeps the launcher executable (ditto drops it otherwise)",
                  bool(unpacked & 0o111), "mode %o" % unpacked)

    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
