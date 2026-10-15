#!/usr/bin/env python3
"""The launcher page, served from this Mac, playing on this Mac.

The streamed edition talks to a runner: it reads a status file a workflow wrote and
opens a link somebody else's machine produced. On a machine you are sitting at, none
of that middle layer is worth keeping, so this serves the launcher and drives the
local play path directly (tools/play-local.py), which is the same planning code the
runner uses:

    python3 tools/local-ui.py                 # then open http://127.0.0.1:8130
    python3 tools/local-ui.py --port 9000     # somewhere else
    python3 tools/local-ui.py --no-browser    # do not open a window

What it does:

  * serves launcher/ on 127.0.0.1 only - never 0.0.0.0, there is no auth here,
  * answers /api/preflight with what this machine is still missing, before a
    multi-hundred-megabyte download starts rather than after,
  * answers /api/play by running play-local.py with exactly the version, loader,
    heap and name that were picked, and streams its output back to the page,
  * answers /api/stop by ending that process group, because a game that will not
    quit is worse than one that will not start.

Nothing is installed until Play is pressed, nothing is published anywhere, and the
only file written outside this folder is the game itself, under ~/.webmc/.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_PORT = 8130


def first_existing(*candidates: pathlib.Path) -> pathlib.Path:
    """The first of these that is there.

    The launcher is served from two layouts: the working tree (launcher/index.html,
    docs/versions.json) and the published one built by tools/build-pages-tree.py
    (index.html, versions.json at the root). Both have to work, because a clone of the
    published repository is a normal way to get this.
    """
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


PAGE_DIR = (ROOT / "launcher" if (ROOT / "launcher" / "index.html").is_file() else ROOT)
CATALOGUE = first_existing(ROOT / "docs" / "versions.json", ROOT / "versions.json")
PLAYABLE = first_existing(ROOT / "launcher" / "playable.json", ROOT / "playable.json")
CONFIG = first_existing(ROOT / "docs" / "config.json", ROOT / "config.json")

MC_DIR = pathlib.Path.home() / ".webmc" / "minecraft"
JDK_ROOT = pathlib.Path.home() / ".webmc" / "jdk"
VENV = pathlib.Path.home() / ".webmc" / "venv"
LIBRARY = "minecraft-launcher-lib>=8.0"
NEWEST_PYTHONS = ("python3.15", "python3.14", "python3.13", "python3.12", "python3.11", "python3.10")

LOADERS = ("fabric", "quilt", "vanilla")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")

# --------------------------------------------------------------------------- #
# the session: one child process, its output, and who is allowed to touch it
# --------------------------------------------------------------------------- #


class Session:
    """A single local play process, plus the log the page reads incrementally."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.status = "idle"          # idle | installing | launching | running | stopped | error
        self.message = "Nothing running."
        self.version = ""
        self.loader = ""
        self.started: float | None = None
        self.ended: float | None = None
        self.returncode: int | None = None
        self.lines: deque[tuple[int, str]] = deque(maxlen=4000)
        self.index = 0
        self.stopping = False        # a stop the user asked for is not a crash

    # -- log ---------------------------------------------------------------- #
    def log(self, text: str) -> None:
        with self.lock:
            for raw in str(text).rstrip("\n").split("\n"):
                self.index += 1
                self.lines.append((self.index, raw))

    def log_since(self, since: int) -> tuple[int, list[str]]:
        with self.lock:
            return self.index, [line for index, line in self.lines if index > since]

    def tail(self, count: int = 200) -> list[str]:
        with self.lock:
            return [line for _, line in list(self.lines)[-count:]]

    # -- lifecycle ---------------------------------------------------------- #
    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, version: str, loader: str, xmx: int, username: str,
              python: str | None = None) -> tuple[bool, str]:
        with self.lock:
            if self.busy():
                return False, f"Already running {self.version} (stop it first)."
            self.lines.clear()
            self.index = 0
            self.status = "installing"
            self.message = f"Preparing Minecraft {version} + {loader} ..."
            self.version, self.loader = version, loader
            self.started, self.ended, self.returncode = time.time(), None, None
            self.stopping = False

        command = [
            str(python or sys.executable), str(HERE / "play-local.py"),
            "--version", version, "--loader", loader,
            "--xmx", str(xmx), "--username", username,
        ]
        self.log(f"$ {' '.join(command)}")
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv list, every part validated
                command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
            )
        except OSError as problem:
            with self.lock:
                self.status, self.message = "error", f"Could not start it: {problem}"
            self.log(self.message)
            return False, self.message

        with self.lock:
            self.proc = proc
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        return True, self.message

    def _pump(self, proc: subprocess.Popen) -> None:
        """Read the child until it ends, and let its own words set the status."""
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log(line)
            low = line.lower()
            with self.lock:
                if "installing minecraft" in low or "downloading" in low or "looking up a jdk" in low:
                    self.status, self.message = "installing", line.strip()
                elif "starting minecraft" in low:
                    self.status, self.message = "launching", "Minecraft is starting."
                elif "setting user" in low or "lwjgl" in low or "sound engine started" in low:
                    self.status, self.message = "running", "Minecraft is running."
        code = proc.wait()
        with self.lock:
            asked_to_stop = self.stopping
            self.proc = None
            self.ended, self.returncode = time.time(), code
            if asked_to_stop:
                self.status, self.message = "stopped", "Stopped. Minecraft is closed."
            elif self.status == "error":
                pass
            elif code == 0:
                self.status, self.message = "stopped", "Minecraft closed."
            else:
                self.status = "error"
                self.message = (f"Exited with code {code}. The last lines above say why; "
                                "a missing launcher library or an old JDK is the usual reason.")
        self.log(self.message)

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            proc = self.proc
            version = self.version
        if proc is None or proc.poll() is not None:
            return False, "Nothing is running."
        self.log(f"stopping {version} ...")
        with self.lock:
            self.stopping = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.terminate()
        for _ in range(50):                     # the game gets 5 s to exit on its own
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            self.log("it did not exit on its own, so it was ended")
        with self.lock:
            self.status, self.message = "stopped", "Stopped. Minecraft is closed."
        return True, self.message

    def state(self) -> dict:
        with self.lock:
            elapsed = 0
            if self.started:
                elapsed = int((self.ended or time.time()) - self.started)
            return {
                "status": self.status, "message": self.message,
                "version": self.version, "loader": self.loader,
                "pid": self.proc.pid if self.busy() else None,
                "elapsed": elapsed, "returncode": self.returncode,
                "lines": self.index,
            }


SESSION = Session()

POST_ONLY = ("/api/play", "/api/stop", "/api/install-lib")


# --------------------------------------------------------------------------- #
# what this machine is missing (asked before Play, not after)
# --------------------------------------------------------------------------- #


def launcher_lib_version(python: pathlib.Path | str | None = None) -> str | None:
    """Which launcher library a given interpreter can import, if any."""
    interpreter = str(python or sys.executable)
    try:
        done = subprocess.run(
            [interpreter, "-c",
             "import minecraft_launcher_lib as m; print(getattr(m, '__version__', 'installed'))"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or "installed").strip().splitlines()[-1] if done.stdout.strip() else "installed"


def venv_python() -> pathlib.Path | None:
    """The launcher's own interpreter, if the install button has been used."""
    candidate = VENV / "bin" / "python"
    return candidate if candidate.is_file() else None


def play_python() -> pathlib.Path | None:
    """The interpreter that can actually import the library, venv first."""
    for candidate in (venv_python(), pathlib.Path(sys.executable)):
        if candidate and launcher_lib_version(candidate) is not None:
            return candidate
    return None


def python_major_minor(interpreter: pathlib.Path | str) -> tuple[int, int] | None:
    try:
        done = subprocess.run(
            [str(interpreter), "-c", "import sys; print(*sys.version_info[:2])"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001
        return None
    parts = (done.stdout or "").strip().split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def interpreter_for_venv() -> pathlib.Path | None:
    """A Python 3.10+ to build ~/.webmc/venv with.

    The library needs 3.10+, and the python3 this may be running under can be older
    (macOS ships one from the Command Line Tools), so the venv is built with the
    newest interpreter that is actually here rather than with whatever is running.
    """
    found: list[pathlib.Path] = [pathlib.Path(sys.executable)]
    for directory in (pathlib.Path.home() / ".local" / "bin",
                      pathlib.Path("/opt/homebrew/bin"), pathlib.Path("/usr/local/bin")):
        if not directory.is_dir():
            continue
        found += [directory / name for name in NEWEST_PYTHONS if (directory / name).is_file()]
    best: tuple[tuple[int, int], pathlib.Path] | None = None
    for candidate in found:
        version = python_major_minor(candidate)
        if not version or version < (3, 10):
            continue
        if best is None or version > best[0]:
            best = (version, candidate)
    return best[1] if best else None


def install_library(log) -> tuple[bool, str]:
    """Make ~/.webmc/venv with a 3.10+ interpreter and put the library in it."""
    interpreter = interpreter_for_venv()
    if interpreter is None:
        return False, ("no Python 3.10+ on this machine, and the launcher library needs it - "
                       "install one (brew install python@3.13) and press this again")
    log(f"$ {interpreter} -m venv {VENV}")
    made = subprocess.run([str(interpreter), "-m", "venv", str(VENV)],
                          capture_output=True, text=True, timeout=600)
    if made.returncode != 0:
        log((made.stdout or "") + (made.stderr or ""))
        return False, "could not make the launcher's venv - see the log"
    python = venv_python()
    if python is None:
        return False, "the venv was made but has no python in it"
    log(f"$ {python} -m pip install {LIBRARY!r}")
    got = subprocess.run([str(python), "-m", "pip", "install", LIBRARY],
                         capture_output=True, text=True, timeout=1800)
    log((got.stdout or "")[-4000:] + (got.stderr or "")[-2000:])
    if got.returncode != 0:
        return False, "pip refused it - the log says why"
    return True, f"Installed into {VENV}."


def java_found() -> tuple[str | None, int | None]:
    """The best JDK already on this machine: system, or one a previous run downloaded."""
    candidates: list[pathlib.Path] = []
    system = shutil.which("java")
    if system:
        candidates.append(pathlib.Path(system))
    if JDK_ROOT.is_dir():
        for item in sorted(JDK_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            for found in (item.glob("*/Contents/Home/bin/java"), item.glob("*/bin/java"),
                          item.glob("Contents/Home/bin/java"), item.glob("bin/java")):
                candidates += list(found)
    for java in candidates:
        try:
            out = subprocess.run([str(java), "-version"], capture_output=True, text=True,
                                 timeout=20).stderr
        except Exception:  # noqa: BLE001
            continue
        for token in out.replace('"', " ").split():
            if token[:1].isdigit() and "." in token:
                major = token.split(".")
                heading = int(major[0])
                return str(java), int(major[1]) if heading == 1 else heading
    return None, None


def jar_mods() -> list[dict]:
    """The mods/ folder, read the same way the play path reads it."""
    wanted = HERE / "play-local.py"
    jars: list[dict] = []
    try:
        spec = importlib.util.spec_from_file_location("webmc_play_local", wanted)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        info = module.jar_mod_info
    except Exception:  # noqa: BLE001
        def info(path: pathlib.Path) -> dict:  # type: ignore[misc]
            return {}

    for jar in sorted((ROOT / "mods").glob("*.jar")):
        detail = info(jar) or {}
        wanted_version = detail.get("minecraft")
        if isinstance(wanted_version, list):
            wanted_version = ", ".join(str(v) for v in wanted_version)
        jars.append({
            "file": jar.name,
            "name": detail.get("name") or jar.stem,
            "id": detail.get("id"),
            "minecraft": wanted_version,
            "size_mb": round(jar.stat().st_size / 1048576, 1),
        })
    return jars


def installed_versions() -> list[dict]:
    versions = MC_DIR / "versions"
    found: list[dict] = []
    if versions.is_dir():
        for item in sorted(versions.iterdir()):
            if item.is_dir() and (item / f"{item.name}.json").is_file():
                found.append({"id": item.name,
                              "size_mb": round(sum(f.stat().st_size for f in item.rglob("*")
                                                   if f.is_file()) / 1048576, 1)})
    return found


def preflight() -> dict:
    python = play_python()
    lib = launcher_lib_version(python) if python else None
    venv = venv_python()
    builder = interpreter_for_venv()
    running = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    java, major = java_found()
    installed = installed_versions()
    mods = jar_mods()
    free_gb = round(shutil.disk_usage(str(pathlib.Path.home())).free / 1073741824, 1)

    if lib:
        where = f"in {VENV}" if python and venv and python == venv else f"in {python or sys.executable}"
        library_detail = f"minecraft-launcher-lib {lib} {where}"
        library_fix = None
    elif builder is not None:
        library_detail = (f"not installed yet - the button makes {VENV} with "
                          f"{builder} and puts it there")
        library_fix = None
    else:
        library_detail = "not installed, and no Python 3.10+ to install it into"
        library_fix = "brew install python@3.13   # the library needs 3.10+"

    items = [
        {"name": "launcher library", "ok": lib is not None,
         "detail": library_detail, "fix": library_fix},
        {"name": "launcher python", "ok": sys.version_info >= (3, 10),
         "detail": (f"{running} (this server)" if sys.version_info >= (3, 10) else
                    f"{running} - too old for the library, so it runs in {VENV} instead"),
         "fix": None},
        {"name": "Java", "ok": java is not None,
         "detail": f"{java} (major {major})" if java else
                   "none yet - Play downloads Temurin from Adoptium first",
         "fix": None},
        {"name": "game files", "ok": bool(installed),
         "detail": (f"{len(installed)} version(s) under {MC_DIR}" if installed
                    else f"empty - Play installs into {MC_DIR}"),
         "fix": None},
        {"name": "mods folder", "ok": bool(mods),
         "detail": f"{len(mods)} jar(s) in mods/" if mods else "no jars in mods/",
         "fix": None},
        {"name": "disk", "ok": free_gb > 12,
         "detail": f"{free_gb} GB free", "fix": None},
    ]
    return {
        "ready": lib is not None,
        "items": items,
        "mods": mods,
        "installed": installed,
        "game_dir": str(MC_DIR),
        "venv": str(VENV),
        "python": str(python) if python else None,
        "java": java,
        "java_major": major,
    }


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #


def read_json(path: pathlib.Path, fallback: dict | None = None) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return fallback if fallback is not None else {}


def catalogue() -> dict:
    """Mojang's own version list, as this repo last fetched it."""
    return read_json(CATALOGUE, {"latest": {}, "count": 0, "versions": []})


def validate_play(payload: dict, known: set[str]) -> tuple[bool, str, dict]:
    """What the page sent, checked against what this machine will actually run.

    The version has to be one Mojang lists, and everything else has to be a value the
    play path accepts, because these four strings end up as argv.
    """
    version = str(payload.get("version") or "")
    loader = str(payload.get("loader") or "fabric").lower()
    username = str(payload.get("username") or "Player")
    raw_heap = payload.get("xmx", 4)
    if raw_heap in (None, ""):
        xmx = 4
    else:
        try:
            xmx = int(raw_heap)
        except (TypeError, ValueError):
            return False, "heap must be a whole number of GB.", {}

    if version not in known:
        return False, f"{version!r} is not a version Mojang lists.", {}
    if loader not in LOADERS:
        return False, f"loader must be one of {', '.join(LOADERS)}.", {}
    if not 1 <= xmx <= 64:
        return False, "heap must be between 1 and 64 GB.", {}
    if not USERNAME_RE.match(username):
        return False, "name must be 1-16 letters, digits or _.", {}
    return True, "", {"version": version, "loader": loader, "xmx": xmx, "username": username}


class Handler(BaseHTTPRequestHandler):
    server_version = "webmc-local-ui"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - base class signature
        if os.environ.get("WEBMC_UI_VERBOSE"):
            sys.stderr.write("  %s\n" % (fmt % args))

    # -- plumbing ----------------------------------------------------------- #
    def send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: pathlib.Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    # -- routes ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - base class signature
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            self.send_file(PAGE_DIR / "index.html", "text/html; charset=utf-8")
        elif route == "/api/preflight":
            self.send_json(preflight())
        elif route == "/api/versions":
            self.send_json(catalogue())
        elif route == "/api/playable":
            self.send_json(read_json(PLAYABLE, {"mode": "none", "counts": {}, "versions": []}))
        elif route == "/api/config":
            self.send_json(read_json(CONFIG))
        elif route == "/api/state":
            self.send_json(SESSION.state())
        elif route == "/api/log":
            since = 0
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("since="):
                        try:
                            since = int(part[6:])
                        except ValueError:
                            since = 0
            index, lines = SESSION.log_since(since)
            self.send_json({"index": index, "lines": lines})
        elif route.startswith("/launcher/"):
            self.send_file(PAGE_DIR / route.rsplit("/", 1)[-1], "application/octet-stream")
        elif route in POST_ONLY:
            self.send_error(405, "POST only")
        else:
            self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - base class signature
        route = self.path.split("?", 1)[0]
        payload = self.body_json()

        if route == "/api/play":
            known = {entry["id"] for entry in catalogue().get("versions", [])}
            allowed, complaint, params = validate_play(payload, known)
            if not allowed:
                self.send_json({"ok": False, "message": complaint}, 400)
                return
            python = play_python()
            if python is None:
                self.send_json({"ok": False, "message":
                                "the launcher library is not installed yet - press the button "
                                "under \"Before Play\" first (one click, into ~/.webmc/venv)"}, 400)
                return
            ok, message = SESSION.start(python=str(python), **params)
            self.send_json({"ok": ok, "message": message}, 200 if ok else 409)

        elif route == "/api/stop":
            ok, message = SESSION.stop()
            self.send_json({"ok": ok, "message": message}, 200 if ok else 409)

        elif route == "/api/install-lib":
            ok, message = install_library(SESSION.log)
            self.send_json({"ok": ok, "message": message})

        elif route in POST_ONLY:
            self.send_error(405, "POST only")
        else:
            self.send_error(404, "not found")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the WebMC launcher on this machine")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1",
                        help="127.0.0.1 by default; there is no password on this server")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not (PAGE_DIR / "index.html").is_file():
        print(f"  the launcher page is missing: {PAGE_DIR / 'index.html'}", file=sys.stderr)
        return 2

    room = preflight()
    print()
    print("  WebMC launcher, on this machine")
    print("  " + "-" * 46)
    print(f"  playing     {room['game_dir']}")
    for item in room["items"]:
        print(f"  {'ok  ' if item['ok'] else '--  '} {item['name']:<18} {item['detail']}")
    print()
    if not room["ready"]:
        builder = interpreter_for_venv()
        print("  Play needs the launcher library, and the page installs it in one click")
        print(f"    it makes {VENV} so nothing is installed into your own python")
        if builder:
            print(f"    built with {builder}")
        else:
            print("    there is no Python 3.10+ here though: brew install python@3.13")
        print()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"  launcher    {url}")
    print("  nothing is published anywhere; stop it with Ctrl-C")
    print()
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping ...")
    finally:
        if SESSION.busy():
            SESSION.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
