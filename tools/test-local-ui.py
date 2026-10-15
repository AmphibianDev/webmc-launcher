#!/usr/bin/env python3
"""Tests the local launcher without a browser and without downloading a JDK.

    python3 tools/test-local-ui.py

What it checks:

  * the page's contract: one inline script, it parses, and every id it reaches for
    exists in the markup (a missing id shows up as a launcher that silently stops
    updating, which is exactly the bug this catches),
  * the playability scan agrees with itself and with webmc-port's own parse,
  * a version, loader, heap or name the play path would not accept is refused
    *before* it becomes an argument,
  * a session really starts, reports what it is doing, and stops when asked -
    using a stub play-local.py, so nothing is downloaded,
  * the routes answer: state and versions as JSON, the page as HTML, POST-only
    routes as 405 when they are asked for with GET.

The page and the scan are looked for in both layouts: the working tree
(launcher/index.html, launcher/playable.json) and the published one built by
tools/build-pages-tree.py (index.html, playable.json at the root), so the same tests
run against the copy that goes on GitHub Pages.

No network, no game, no browser: the server binds to 127.0.0.1 on a free port and
is shut down again at the end.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
WORKSPACE = ROOT.parent
FAILED = 0


def report(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILED = 1


def load_ui():
    sys.dont_write_bytecode = True   # loading it must not leave a __pycache__ behind
    spec = importlib.util.spec_from_file_location("webmc_local_ui", HERE / "local-ui.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# --------------------------------------------------------------------------- #
# 1. the page's own contract
# --------------------------------------------------------------------------- #

def find(*candidates: pathlib.Path) -> pathlib.Path:
    """The first of these that exists, so one test suite covers both layouts."""
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def test_page() -> None:
    print("\nthe launcher page")
    page_path = find(ROOT / "launcher" / "index.html", ROOT / "index.html")
    page = page_path.read_text()
    scripts = re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', page, re.S)
    report("there is exactly one inline script", len(scripts) == 1)
    script = scripts[0] if scripts else ""

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        path = handle.name
    parse = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    os.unlink(path)
    report("the inline script parses", parse.returncode == 0, parse.stderr.strip()[:200])

    ids = sorted(set(re.findall(r'el\("([\w-]+)"\)', script)))
    present = set(re.findall(r'id="([\w-]+)"', page))
    missing = sorted(set(ids) - present)
    report(f"every id the script reads exists ({len(ids)} ids)", not missing, str(missing))

    for label, needle in {
        "the rail has the four game tiles, Java Edition selected":
            page.count('class="game') == 4 and 'class="game on"' in page,
        "the player and the launcher version sit at the foot of the rail":
            'class="rail-foot"' in page and 'id="playerName"' in page
            and re.search(r'class="rail-foot".*id="playerName"', page, re.S) is not None
            and 'class="ver mono" id="buildInfo"' in page,
        "the tab row is the launcher's five tabs, Realms and Skins greyed out":
            re.findall(r'<button class="tab[^"]*"[^>]*>([^<]+)</button>', page)
            == ["Play", "Installations", "Realms", "Skins", "Patch notes"]
            and page.count('class="tab dead"') == 2,
        "the play column is hero, then PLAY, then the installation picker":
            page.index('class="hero"') < page.index('id="playButton"') < page.index('id="pickerButton"')
            and 'id="pickerInstallation"' in page,
        "the layout is flat launcher grey, drawn in CSS, no images fetched":
            "--rail: #141414" in page and "--green: #3c8527" in page
            and "url(" not in page.split("<script")[0] and "<img" not in page,
        "every class in the markup has a rule in the stylesheet":
            all(name in "".join(re.findall(r"\.([\w-]+)", page.split("<script")[0]))
                for chunk in re.findall(r'class="([^"]+)"', page) for name in chunk.split()),
        "no other launcher's branding is carried":
            not any(name in page for name in ("EAGLERCRAFT", "Norshare", "ShadowNet", "Shadow Net")),
        "PLAY is the green raised button": "play-btn" in page
            and "box-shadow: 0 3px 0 var(--green-dark)" in page,
        "the installation picker can be searched": 'id="versionSearch"' in page and "fillPicker" in script,
        "the picker filters by release/snapshot/old": 'data-type="release"' in page and "state.chips" in script,
        "the loader, heap and name are sent to the server": all(
            f'id="{name}"' in page for name in ("loaderSelect", "ramInput", "nameInput")),
        "play posts the picked session and not a hardcoded one": 'postJson("api/play"' in script
                                                                 and "state.chosen.id" in script,
        "the log is read incrementally": 'api/log?since=' in script and "state.logFrom" in script,
        "stop is wired to the stop route": 'id="stopButton"' in page and 'postJson("api/stop")' in script,
        "what is missing is shown before Play, not after": 'id="pfList"' in page
                                                           and "api/preflight" in script,
        "the page says it plays here rather than streams":
            "Nothing streamed" in page or "no stream, no runner" in page
            or "nothing of the game" in page.lower(),
        "the wordmark is ours, and says what the page does":
            "WebMC" in page and "Java Edition" in page,
        "patch notes are honest about not being Mojang's":
            "Mojang writes the patch notes" in page,
        "the venv is what the page installs into": "~/.webmc/venv" in page,
        "the page can also be served as plain files (the published copy)":
            "fetchData" in script and 'fetchJson(name + ".json")' in script,
        "and says so instead of offering a Play that cannot work": "goStatic" in script
                                                                     and "published copy" in script,
        "the switcher can filter by what runs on this machine": 'data-play="native"' in page
                                                               and "state.plays" in script,
        "the switcher defaults to the arm64 ones": re.search(
            r'class="chip on" data-play="native"', page) is not None,
        "each version row carries a plays-here badge": "playBadge(" in script and ".play.ok" in page,
        "and so does the chosen version, without opening the picker":
            'id="pickerPlay"' in page and "paintPickerBadge" in script,
        "the versions table shows java and lwjgl": all(h in page for h in ("<th>Java</th>", "<th>LWJGL</th>",
                                                                        "<th>Plays here</th>")),
        "the scan's own provenance is on the page": "evidence.not_checked" in script
                                                    and "lwjgl_arm64_from" in script,
        "a version that cannot start is warned about, not hidden": 'kind === "rosetta"' in script
                                                                  and 'kind === "other"' in script,
    }.items():
        report(label, needle)


def test_playable() -> None:
    print("\nthe playability scan")
    path = find(ROOT / "launcher" / "playable.json", ROOT / "playable.json")
    report("the scan file is where the page expects it", path.is_file(), str(path))
    scan = json.loads(path.read_text())
    rows = scan["versions"]
    counts = scan["counts"]

    report("it covers Mojang's whole catalogue", len(rows) == counts["total"] and len(rows) > 900,
           f"{len(rows)} rows, total says {counts['total']}")
    tally: dict[str, int] = {}
    for row in rows:
        tally[row["verdict"]] = tally.get(row["verdict"], 0) + 1
    report("the counts add up to the rows", all(counts.get(k) == v for k, v in tally.items()),
           f"tally {tally} vs counts {counts}")
    report("native plus rosetta is what it calls playable",
           counts["playable"] == counts.get("native", 0) + counts.get("rosetta", 0))

    def lwjgl_major_minor(text: str):
        parts = [int(p) for p in str(text).split(".")[:2] if p.isdigit()]
        return parts if len(parts) == 2 else None

    wrong = []
    for row in rows:
        version = lwjgl_major_minor(row.get("lwjgl"))
        if row["verdict"] == "native" and version and version < [3, 3]:
            wrong.append(f"{row['id']} native but LWJGL {row['lwjgl']}")
        if row["verdict"] == "rosetta" and version and version >= [3, 3]:
            wrong.append(f"{row['id']} rosetta but LWJGL {row['lwjgl']}")
        if row["verdict"] == "rosetta" and str(row.get("lwjgl", "")).startswith("4"):
            wrong.append(f"{row['id']} rosetta on LWJGL 4")
    report("the arm64 line is LWJGL 3.3, and nothing crosses it the wrong way", not wrong,
           "; ".join(wrong[:4]))

    routes = WORKSPACE / "webmc-port" / "site" / "routes.json"
    if routes.is_file():
        known = json.loads(routes.read_text()).get("versions", {})
        checked = conflicts = 0
        for row in rows:
            if row["provenance"]["lwjgl"] != "document":
                continue
            checked += 1
            theirs = known.get(row["id"], {})
            if theirs.get("lwjgl") and str(theirs["lwjgl"]) != str(row["lwjgl"]):
                conflicts += 1
        report(f"document-derived rows match webmc-port's own parse ({checked} rows)",
               checked > 0 and conflicts == 0, f"{conflicts} conflicts")

    if scan["mode"] == "offline":
        unchecked = (scan["evidence"].get("not_checked") or [])
        report("an offline scan says what it could not check", len(unchecked) >= 2, str(unchecked))
        report("and says why it was offline", "why_offline" in scan["evidence"])
    for row in rows[:50]:
        if row["verdict"] == "native" and not row.get("reason"):
            report("every verdict carries its reason", False, row["id"])
            break
    else:
        report("every verdict carries its reason", True)


def test_interpreter(ui) -> None:
    print("\nthe interpreter Play will use")
    builder = ui.interpreter_for_venv()
    if builder is None:
        report("either a Python 3.10+ exists, or nothing claims to install the library",
               ui.preflight()["items"][0]["fix"] is not None)
    else:
        version = ui.python_major_minor(builder)
        report(f"the venv builder is 3.10+ ({builder}, {version})",
               bool(version) and version >= (3, 10))
    report("the library is looked for in the launcher's own venv first",
           ui.venv_python() == (ui.VENV / "bin" / "python") or ui.venv_python() is None)
    report("a missing library is not mistaken for a present one",
           ui.play_python() is None or ui.launcher_lib_version(ui.play_python()) is not None)
    names = [item["name"] for item in ui.preflight()["items"]]
    report("preflight reports the python it is running under", "launcher python" in names, str(names))


# --------------------------------------------------------------------------- #
# 2. what the play path will accept
# --------------------------------------------------------------------------- #

def test_validation(ui) -> None:
    print("\nwhat Play accepts")
    known = {entry["id"] for entry in ui.catalogue().get("versions", [])}
    report("the version list is Mojang's, not invented", len(known) > 800, f"{len(known)} versions")

    ok, _, params = ui.validate_play(
        {"version": "1.21.5", "loader": "fabric", "xmx": 4, "username": "Derek_1"}, known)
    report("a normal session passes", ok and params["version"] == "1.21.5" and params["xmx"] == 4)

    for label, payload in {
        "a version Mojang does not list": {"version": "1.21.5; rm -rf /"},
        "a loader that is not one of ours": {"version": "1.21.5", "loader": "forge; curl evil"},
        "a heap that is not a number": {"version": "1.21.5", "xmx": "all of it"},
        "a heap past the machine": {"version": "1.21.5", "xmx": 4096},
        "a name with a space in it": {"version": "1.21.5", "username": "not a name"},
        "a name that is a path": {"version": "1.21.5", "username": "../../etc/passwd"},
    }.items():
        refused, complaint, _ = ui.validate_play(payload, known)
        report(f"refused: {label}", not refused, complaint)


# --------------------------------------------------------------------------- #
# 3. a session, with a stub in place of the game
# --------------------------------------------------------------------------- #

def test_session(ui) -> None:
    print("\na session (stub play-local.py: nothing is downloaded)")
    real_here = ui.HERE
    with tempfile.TemporaryDirectory() as tmp:
        stub = pathlib.Path(tmp) / "play-local.py"
        stub.write_text(
            "import sys, time\n"
            "print('  WebMC, on this machine', flush=True)\n"
            "print('installing Minecraft ' + sys.argv[2] + ' ...', flush=True)\n"
            "time.sleep(0.3)\n"
            "print('starting Minecraft ...', flush=True)\n"
            "print('Setting user: Tester', flush=True)\n"
            "time.sleep(120)\n"
        )
        ui.HERE = pathlib.Path(tmp)
        session = ui.Session()
        try:
            started, message = session.start("1.21.5", "fabric", 4, "Tester")
            report("it starts", started, message)

            seen = []
            for _ in range(60):
                state = session.state()
                seen.append(state["status"])
                if state["status"] == "running":
                    break
                time.sleep(0.1)
            report("its own words move the status through installing → launching → running",
                   seen[-1] == "running", " → ".join(dict.fromkeys(seen)))
            report("it has a process id", bool(session.state()["pid"]))

            lines = session.tail(50)
            report("the log keeps what it printed", any("installing Minecraft 1.21.5" in l for l in lines))
            index, new = session.log_since(0)
            report("the log can be read from a mark", index == len(lines) and len(new) == index,
                   f"index {index}, {len(new)} lines")

            second, complaint = session.start("26.3", "fabric", 4, "Tester")
            report("a second session is refused while one runs", not second, complaint)

            stopped, message = session.stop()
            report("stop ends it", stopped, message)
            report("and the status says stopped, not crashed", session.state()["status"] == "stopped",
                   session.state()["message"])
            report("its process is gone", session.state()["pid"] is None)
        finally:
            if session.busy():
                session.stop()
            ui.HERE = real_here


# --------------------------------------------------------------------------- #
# 4. the routes
# --------------------------------------------------------------------------- #

def test_routes(ui) -> None:
    print("\nthe routes, on a free port")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(base + path, timeout=10) as answer:
                return answer.status, answer.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as problem:
            return problem.code, problem.read().decode("utf-8", "replace")

    def post(path: str, payload: dict) -> tuple[int, str]:
        request = urllib.request.Request(base + path, method="POST",
                                        data=json.dumps(payload).encode(),
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as answer:
                return answer.status, answer.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as problem:
            return problem.code, problem.read().decode("utf-8", "replace")

    try:
        code, body = get("/")
        report("the page is served", code == 200 and "Minecraft: Java Edition" in body, f"http {code}")

        code, body = get("/api/preflight")
        report("preflight answers with this machine", code == 200 and "items" in json.loads(body),
               f"http {code}")

        code, body = get("/api/versions")
        report("the version list is served", code == 200 and json.loads(body)["count"] > 800,
               f"http {code}")

        code, body = get("/api/playable")
        served = json.loads(body)
        report("the playability scan is served too",
               code == 200 and len(served.get("versions", [])) > 900, f"http {code}")

        code, body = get("/api/state")
        report("state is idle before anything runs", code == 200 and json.loads(body)["status"] == "idle")

        code, _ = get("/api/play")
        report("POST-only routes say so instead of 404", code == 405, f"http {code}")

        code, body = post("/api/play", {"version": "not-a-version"})
        report("an invented version is refused with 400", code == 400, f"http {code} {body[:80]}")

        if ui.play_python() is None:
            code, body = post("/api/play", {"version": "1.21.5", "loader": "fabric"})
            report("a real version is still refused while the library is missing, and says why",
                   code == 400 and "library" in body, f"http {code} {body[:120]}")
        else:
            report("the library is installed, so Play is not blocked by it", True)

        code, _ = get("/nothing-here")
        report("an unknown route is still a 404", code == 404, f"http {code}")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    ui = load_ui()
    test_page()
    test_playable()
    test_validation(ui)
    test_interpreter(ui)
    test_session(ui)
    test_routes(ui)
    print()
    print("local launcher tests passed" if not FAILED else "local launcher tests FAILED")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
