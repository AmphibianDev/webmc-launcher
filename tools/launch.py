#!/usr/bin/env python3
"""
WebMC launcher.

    prepare   install Minecraft + Fabric for the detected version, drop the
              mods into the game folder, optionally add Fabric API, then say
              which launch profile to use.
    run       build the launch command, start Minecraft, watch it, and keep
              docs/session.json up to date until the game exits.

Everything the player configured lives in docs/config.json; everything the
mods asked for was worked out by tools/session.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

import minecraft_launcher_lib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION = os.path.join(ROOT, "tools", "session.py")
LOG_PATH = "/tmp/minecraft.log"
FABRIC_API_MAVEN = "https://maven.fabricmc.net/net/fabricmc/fabric-api/fabric-api"
MODRINTH = "https://api.modrinth.com/v2/project/fabric-api/version"
USER_AGENT = "webmc/1.0 (+https://github.com)"
GRAPHICS_MODES = {"fast": 0, "fancy": 1, "fabulous": 2}

# Added automatically while `optimization_mods` is on (docs/config.json). On a
# runner with no GPU, Minecraft's own renderer is the bottleneck: these are the
# client-side rewrites of it that make software OpenGL usable. A mod the player
# already dropped into mods/ is never overridden.
OPTIMIZATION_MODS = [
    ("sodium", "Sodium"),
    ("lithium", "Lithium"),
    ("immediatelyfast", "ImmediatelyFast"),
    ("ferritecore", "FerriteCore"),
]
ALL_OPTIMIZATION_SLUGS = {slug for slug, _ in OPTIMIZATION_MODS}


def selected_optimization_mods(value) -> set:
    """`optimization_mods` may be true/false, or a list of slugs to try."""
    text = str(value).strip().lower()
    if text in ("", "false", "0", "no", "off", "none"):
        return set()
    if text in ("true", "1", "yes", "on", "all"):
        return set(ALL_OPTIMIZATION_SLUGS)
    wanted = {part.strip() for part in re.split(r"[,\s]+", text) if part.strip()}
    unknown = wanted - ALL_OPTIMIZATION_SLUGS
    if unknown:
        log(f"note: ignoring unknown optimization_mods entries: {', '.join(sorted(unknown))}")
    return wanted & ALL_OPTIMIZATION_SLUGS


NO_STATE = {"on": False}


def log(message: str) -> None:
    print(message, flush=True)


def publish(status: str, message: str, progress: int, commit: bool = True, extra: dict | None = None) -> None:
    """Update the public session state through tools/session.py."""
    if NO_STATE["on"]:
        return
    command = [
        sys.executable, SESSION, "publish",
        "--state", os.path.join(ROOT, "docs"),
        "--status", status,
        "--message", message,
        "--progress", str(progress),
    ]
    for key, value in (extra or {}).items():
        command += ["--set", f"{key}={value}"]
    if commit:
        command.append("--commit")
    subprocess.run(command, check=False)


def read_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001
        return {}


def gh_env(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_ENV")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    os.environ[name] = value


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def progress_callback():
    state = {"max": 1, "last": 0}

    def set_status(text: str) -> None:
        log(f"  {text}")

    def set_max(value: int) -> None:
        state["max"] = max(int(value), 1)

    def set_progress(value: int) -> None:
        percent = int(int(value) / state["max"] * 100)
        if percent - state["last"] >= 25:
            state["last"] = percent
            log(f"  ... {percent}%")

    return {"setStatus": set_status, "setMax": set_max, "setProgress": set_progress}


def fetch(url: str, target: str, timeout: int = 180) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as error:  # noqa: BLE001
        log(f"  ! download failed ({error})")
        try:
            os.unlink(target)
        except OSError:
            pass
        return False
    if os.path.getsize(target) < 50_000:  # an error page, not a mod
        log("  ! download looked like an error page, ignoring it")
        os.unlink(target)
        return False
    return True


def fabric_api_from_maven(mc_dir: str, version: str) -> str | None:
    """FabricMC's maven already serves the loader, so try it before anything else."""
    try:
        request = urllib.request.Request(f"{FABRIC_API_MAVEN}/maven-metadata.xml", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            metadata = response.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        log(f"  ! FabricMC maven not reachable ({error})")
        return None
    releases = [v for v in re.findall(r"<version>([^<]+)</version>", metadata) if v.endswith(f"+{version}")]
    if not releases:
        log(f"  ! FabricMC maven has no Fabric API build for {version}")
        return None
    pick = releases[-1]
    filename = f"fabric-api-{pick}.jar"
    if fetch(f"{FABRIC_API_MAVEN}/{pick}/{filename}", os.path.join(mc_dir, "mods", filename)):
        log(f"  + {filename} (auto, from FabricMC)")
        return filename
    return None


def modrinth_versions(slug: str, version: str):
    query = urllib.parse.urlencode(
        {"game_versions": json.dumps([version]), "loaders": json.dumps(["fabric"])}
    )
    request = urllib.request.Request(
        f"https://api.modrinth.com/v2/project/{slug}/version?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def download_modrinth_mod(slug: str, label: str, mc_dir: str, version: str) -> str | None:
    try:
        versions = modrinth_versions(slug, version)
    except Exception as error:  # noqa: BLE001
        log(f"  ! could not reach Modrinth for {label} ({error})")
        return None
    for entry in versions:
        files = entry.get("files") or []
        pick = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not pick:
            continue
        target = os.path.join(mc_dir, "mods", os.path.basename(pick["filename"]))
        if fetch(pick["url"], target):
            log(f"  + {os.path.basename(target)} (auto, {label} {entry.get('version_number')})")
            return os.path.basename(target)
    log(f"  ! no {label} build found for Minecraft {version}")
    return None


def download_fabric_api(mc_dir: str, version: str) -> str | None:
    """Add the Fabric API build matching this Minecraft version (best effort)."""
    from_maven = fabric_api_from_maven(mc_dir, version)
    if from_maven:
        return from_maven
    return download_modrinth_mod("fabric-api", "Fabric API", mc_dir, version)


def installed_mod_ids(mods_dir: str) -> set:
    """Mod ids already sitting in the game folder (so auto mods never fight them)."""
    found = set()
    if not os.path.isdir(mods_dir):
        return found
    for name in sorted(os.listdir(mods_dir)):
        if not name.lower().endswith(".jar"):
            continue
        try:
            with zipfile.ZipFile(os.path.join(mods_dir, name)) as jar:
                if "fabric.mod.json" not in jar.namelist():
                    continue
                meta = json.loads(jar.read("fabric.mod.json").decode("utf-8", "replace"))
                if meta.get("id"):
                    found.add(str(meta["id"]).lower())
        except Exception:  # noqa: BLE001
            continue
    return found


def write_options(mc_dir: str, config: dict) -> None:
    """Point Minecraft at settings this CPU-only runner can actually keep up with."""
    fps = clamp_int(config.get("fps_limit"), 30, 5, 260)
    distance = clamp_int(config.get("render_distance"), 8, 2, 32)
    graphics = GRAPHICS_MODES.get(str(config.get("graphics") or "fast").lower(), 0)
    settings = [
        f"maxFps:{fps}",
        f"renderDistance:{distance}",
        f"graphicsMode:{graphics}",
        "ao:0",              # smooth lighting is one of the costliest things on llvmpipe
        "particles:2",       # minimal
        "entityShadows:false",
        "clouds:false",
        "renderClouds:false",
        "mipmapLevels:0",
        "biomeBlendRadius:0",
        "enableVsync:false",
        "pauseOnLostFocus:false",  # nobody is sitting at the runner's keyboard
        # Raw input asks GLFW for the mouse device's own counts. On a virtual display driven
        # by an injected pointer there is no such device, and the mismatch is what spins the
        # camera (a warp to the centre arrives as a huge delta). The streamed pointer that
        # Selkies controls is the one that behaves.
        "rawMouseInput:false",
        "mouseSensitivity:0.5",
        "narrator:0",        # text-to-speech costs CPU and nobody hears it
        "syncChunkWrites:false",
        "guiScale:2",
        # The virtual screen is exactly the game's size and there is no window manager, so
        # fullscreen means the game owns the whole display - which is also the state in which
        # it grabs the pointer most predictably.
        "fullscreen:true",
    ]
    path = os.path.join(mc_dir, "options.txt")
    os.makedirs(mc_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(settings) + "\n")
    log(f"wrote options.txt: {fps} fps cap, render distance {distance}, fast graphics")


def clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


def find_profile(mc_dir: str, version: str, loader: str) -> str:
    installed = minecraft_launcher_lib.utils.get_installed_versions(mc_dir)
    prefixes = {"fabric": "fabric-loader-", "quilt": "quilt-loader-", "vanilla": f"{version}"}
    prefix = prefixes.get(loader, "fabric-loader-")
    candidates = [entry["id"] for entry in installed if entry["id"].startswith(prefix)]
    if loader == "vanilla":
        return version
    if not candidates:
        raise SystemExit(f"no {loader} launch profile was installed")
    return sorted(candidates, key=lambda item: os.path.getmtime(
        os.path.join(mc_dir, "versions", item, f"{item}.json")
    ))[-1]


def cmd_profile(args) -> int:
    """Print the launch profile id to use (used by benchmarks and scripts)."""
    print(find_profile(args.mc_dir, args.version, args.loader))
    return 0


def jar_mod_info(path: str) -> dict:
    """id, display name and the Minecraft version a Fabric/Quilt jar asks for."""
    try:
        with zipfile.ZipFile(path) as jar:
            if "fabric.mod.json" not in jar.namelist():
                return {}
            meta = json.loads(jar.read("fabric.mod.json"))
    except Exception:  # noqa: BLE001
        return {}
    depends = meta.get("depends") or {}
    return {
        "id": meta.get("id"),
        "name": (meta.get("name") or meta.get("id")),
        "minecraft": depends.get("minecraft"),
        "loader": depends.get("fabricloader") or depends.get("quilt_loader"),
    }


def mod_fits_session(spec, version: str) -> bool:
    """Ask session.py's version rules, so the plan and the launch cannot disagree."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from session import spec_matches  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True  # no rules available: never block a session over this
    return bool(spec_matches(spec, version))


def skipped_message(skipped: list, version: str) -> str:
    first = skipped[0]
    more = f" (+{len(skipped) - 1} more)" if len(skipped) > 1 else ""
    fix = first.get("fix") or ""
    return (f"{len(skipped)} mod(s) left out, they do not fit this session: "
            f"{first['name']} {first['reason']}{more}.{fix}")


def cmd_prepare(args) -> int:
    mc_dir = args.mc_dir
    os.makedirs(os.path.join(mc_dir, "mods"), exist_ok=True)
    config = read_config(args.config)
    write_options(mc_dir, config)

    publish(
        "preparing",
        f"Downloading Minecraft {args.version}...",
        10,
        extra={
            "minecraft_version": args.version,
            "loader": args.loader,
            # where the version came from: the file, the button, or the mods themselves
            "version_source": os.environ.get("WEBMC_VERSION_SOURCE") or "docs/config.json",
        },
    )
    minecraft_launcher_lib.install.install_minecraft_version(
        args.version, mc_dir, callback=progress_callback()
    )
    log(f"Minecraft {args.version} installed")

    if args.loader == "fabric":
        publish("preparing", "Installing Fabric...", 30)
        minecraft_launcher_lib.fabric.install_fabric(args.version, mc_dir)
        log("Fabric installed")
    elif args.loader == "quilt":
        publish("preparing", "Installing Quilt...", 30)
        minecraft_launcher_lib.quilt.install_quilt(args.version, mc_dir)
        log("Quilt installed")
    elif args.loader != "vanilla":
        raise SystemExit(f"loader '{args.loader}' is not supported by the simple flow (use fabric or vanilla)")

    profile = find_profile(mc_dir, args.version, args.loader)
    log(f"launch profile: {profile}")

    # ---- mods in
    #
    # A jar built for a different Minecraft version does not just fail to load: Fabric
    # refuses to start the game at all, so the session shows an empty screen and looks
    # broken (it then retried three times and gave up). Anything this session's version
    # cannot satisfy is therefore left out by name, with the reason published on the
    # site, and the game still starts.
    publish("preparing", "Copying your mods...", 40)
    mods_target = os.path.join(mc_dir, "mods")
    installed: list[str] = []
    skipped: list[dict] = []
    for name in sorted(os.listdir(args.mods)):
        path = os.path.join(args.mods, name)
        if not (os.path.isfile(path) and name.lower().endswith(".jar")):
            continue
        if args.loader in ("fabric", "quilt"):
            info = jar_mod_info(path)
            needed = info.get("minecraft")
            if needed and not mod_fits_session(needed, args.version):
                skipped.append({
                    "file": name,
                    "id": info.get("id") or name,
                    "name": info.get("name") or name,
                    "reason": f"needs Minecraft {spec_text(needed)}, this session runs {args.version}",
                    "fix": f' Set "minecraft_version": "{spec_text(needed).split()[-1]}" in '
                           f"docs/config.json to run it.",
                })
                log(f"! {name} left out: {skipped[-1]['reason']}")
                continue
        shutil.copy2(path, os.path.join(mods_target, name))
        installed.append(name)
    log(f"copied {len(installed)} mod(s): {', '.join(installed) if installed else '-'}")
    if skipped:
        publish("preparing", skipped_message(skipped, args.version), 45)

    auto_added = []
    present = installed_mod_ids(mods_target)
    if args.fabric_api and args.loader == "fabric":
        if "fabric-api" in present:
            log("Fabric API is already in mods/, not adding it again")
        else:
            added = download_fabric_api(mc_dir, args.version)
            if added:
                auto_added.append(added)
                present.add("fabric-api")
    if args.optimization_mods:
        wanted = selected_optimization_mods(args.optimization_mods)
        if args.loader != "fabric" and wanted:
            log("optimization mods are Fabric-only, skipping them")
            wanted = set()
        for slug, label in OPTIMIZATION_MODS:
            if slug not in wanted:
                continue
            if slug in present:
                log(f"{label} is already in mods/, leaving yours in place")
                continue
            added = download_modrinth_mod(slug, label, mc_dir, args.version)
            if added:
                auto_added.append(added)
                present.add(slug)

    # ---- put back the world the last session saved
    repo_world = os.path.join(ROOT, "world")
    if os.path.isdir(repo_world):
        saves = os.path.join(mc_dir, "saves")
        os.makedirs(saves, exist_ok=True)
        restored = []
        for name in sorted(os.listdir(repo_world)):
            source = os.path.join(repo_world, name)
            if not os.path.isdir(source):
                continue
            destination = os.path.join(saves, name)
            if os.path.isdir(destination):
                shutil.rmtree(destination)
            shutil.copytree(source, destination, symlinks=True)
            restored.append(name)
        if restored:
            log(f"restored world(s): {', '.join(restored)}")
        else:
            log("no saved world to restore yet")

    # ---- keep docs/mods.json honest about what actually got installed
    manifest_path = os.path.join(ROOT, "docs", "mods.json")
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception:  # noqa: BLE001
        manifest = {"mods": []}
    known = {entry.get("file") for entry in manifest.get("mods", [])}
    for name in auto_added:
        if name not in known:
            manifest.setdefault("mods", []).append(
                {
                    "file": name,
                    "id": "fabric-api",
                    "name": "Fabric API",
                    "version": None,
                    "minecraft": args.version,
                    "loader": "fabric",
                    "size_kb": round(os.path.getsize(os.path.join(mods_target, name)) / 1024),
                    "auto": True,
                }
            )
    manifest["installed"] = sorted(installed + auto_added)
    # What was left out and why, so the site can say it instead of showing a blank stream.
    manifest["skipped"] = skipped or manifest.get("skipped", [])
    if skipped:
        manifest["warnings"] = [skipped_message(skipped, args.version)[:400]]
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    total = len(installed) + len(auto_added)
    publish(
        "preparing",
        f"{total} mod(s) installed. Starting the stream...",
        50,
        extra={
            "mods_count": total,
            # left-out mods stay visible on the page while the session plays: a jar that
            # does not fit is the one thing a player needs to know about
            "skipped_mods": len(skipped),
            "skipped_note": skipped_message(skipped, args.version)[:200] if skipped else "",
        },
    )
    gh_env("WEBMC_PROFILE", profile)
    gh_env("WEBMC_MC_DIR", mc_dir)
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
# Only markers that appear *after* the resource/atlas loading finishes count as
# "the game is up". "Backend library: LWJGL" and the block atlas appear seconds
# after launch, which made the site say "playing" while a stuck loading screen
# was still on the screen.
BOOT_MARKERS = (
    "Sound engine started",
)
FAIL_MARKERS = (
    "Incompatible mod set",
    "Incompatible mods found!",
    "Mod resolution failed",
    "but only the wrong version is present",
    "Missing or unsupported mandatory dependencies",
    "ModResolutionException",
    "A mod crashed",
    "requires any version",
    "Failed to start Minecraft",
    "UnsupportedClassVersionError",
    "java.lang.OutOfMemoryError",
)

# A mod that cannot load is a *deterministic* failure: restarting Minecraft will hit
# exactly the same wall. These markers are what Fabric says when a jar does not fit the
# game version, and the message is turned into something a player can act on.
MOD_FAIL_MARKERS = (
    "Incompatible mods found!",
    "Incompatible mod set",
    "Mod resolution failed",
    "but only the wrong version is present",
    "Missing or unsupported mandatory dependencies",
    "ModResolutionException",
    "HARD_DEP",
)

WRONG_VERSION = re.compile(
    r"Mod '([^']+)' \(([^)]+)\)\s+(\S+) requires version ([^\s]+) of '([^']+)'"
    r"(?: \([^)]+\))?, but only the wrong version is present: ([^\s!]+)"
)
HARD_DEP = re.compile(r"HARD_DEP[A-Z_]*\s+(\S+)\s+(\S+)\s+\{depends\s+(\S+)\s+@\s+\[([^\]]+)\]")


def spec_text(spec) -> str:
    """A dependency spec as words: lists mean "any of these"."""
    if isinstance(spec, (list, tuple)):
        return " or ".join(str(item) for item in spec)
    return str(spec or "")


def mod_failure(lines) -> str | None:
    """Turn Fabric's resolution dump into one sentence that says what to change."""
    # Fabric prints the friendly sentence and the internal reason; prefer the sentence,
    # wherever in the block it appears, and fall back to the internal one.
    for line in lines:
        match = WRONG_VERSION.search(line.strip())
        if match:
            name, mod_id, mod_version, want, dependency, have = match.groups()
            return (f"{name} {mod_version} needs {dependency} {want}, but this session runs {have}. "
                    f"Delete mods/{mod_id}*.jar, or set \"minecraft_version\": \"{want}\" "
                    f"in docs/config.json to match the mod.")
    for line in lines:
        text = line.strip()
        match = HARD_DEP.search(text)
        if match:
            mod_id, mod_version, dependency, want = match.groups()
            return (f"{mod_id} {mod_version} needs {dependency} {want}, which this session does not "
                    f"provide. Delete mods/{mod_id}*.jar or pin the matching version in "
                    f"docs/config.json.")
        if "Missing or unsupported mandatory dependencies" in text or "ModResolutionException" in text:
            return ("A mod's dependencies cannot be satisfied, so Fabric stopped the game. "
                    "Check the run log for the mod name, or remove that jar from mods/.")
    return None


def offline_uuid(name: str) -> str:
    import hashlib
    import uuid as uuid_module

    digest = hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest()
    return str(uuid_module.UUID(bytes=digest, version=3))


def tail(path: str, lines: int = 60) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.readlines()[-lines:]
    except OSError:
        return []


STOP = {"requested": False}


def request_stop(signum, _frame) -> None:
    """The runner sends SIGTERM when a push cancels the session: save what we can."""
    STOP["requested"] = True
    log(f"signal {signum} received, wrapping up")


def save_world(mc_dir: str, reason: str) -> None:
    """Copy saves/ into the repository so the world outlives the runner."""
    if NO_STATE["on"]:
        return
    saves = os.path.join(mc_dir, "saves")
    if not os.path.isdir(saves) or not os.listdir(saves):
        return
    log(f"saving world ({reason})")
    subprocess.run(
        [
            sys.executable, SESSION, "save-world",
            "--saves", saves,
            "--world-dir", os.path.join(ROOT, "world"),
            "--message", f"world: {reason}",
            "--commit",
        ],
        check=False,
    )


def scan_log(path: str) -> tuple[bool, str | None, bool]:
    """Return (booted, fatal_line, is_a_mod_problem)."""
    booted = False
    fatal = None
    mod_problem = False
    lines = tail(path, 400)
    if any(marker in line for line in lines for marker in MOD_FAIL_MARKERS):
        mod_problem = True
        fatal = mod_failure(lines)
    for line in lines:
        if any(marker in line for marker in BOOT_MARKERS):
            booted = True
        if fatal is None and any(marker in line for marker in FAIL_MARKERS):
            fatal = line.strip()[:220]
    return booted, fatal, mod_problem


def stop_process(process) -> None:
    process.terminate()
    for _ in range(20):
        if process.poll() is not None:
            return
        time.sleep(0.5)
    process.kill()


def follow_desktop_size() -> None:
    """Keep the game window the same size as the desktop.

    Selkies resizes the X screen to match the browser window (enable_resize).
    Nothing else resizes Minecraft's window - there is no window manager - so
    without this the game would stay at the resolution it was launched with and
    sit in a corner of a differently sized desktop. Matching them means the
    browser scales nothing and the picture is 1:1 sharp.
    """
    window = None
    last = None
    while not STOP["requested"]:
        try:
            size = subprocess.run(
                ["xdotool", "getdisplaygeometry"], capture_output=True, text=True, timeout=5
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return
        if len(size) == 2:
            width, height = int(size[0]), int(size[1])
            if (width, height) != last:
                if window is None:
                    try:
                        found = subprocess.run(
                            ["xdotool", "search", "--name", "Minecraft"],
                            capture_output=True, text=True, timeout=5,
                        ).stdout.split()
                    except (OSError, subprocess.SubprocessError):
                        found = []
                    window = found[-1] if found else None
                if window:
                    subprocess.run(["xdotool", "windowmove", window, "0", "0"], check=False)
                    subprocess.run(["xdotool", "windowsize", window, str(width), str(height)], check=False)
                    subprocess.run(["xdotool", "windowfocus", window], check=False)
                    if last is not None:
                        log(f"desktop is now {width}x{height}; game window matched it")
                        publish_diag(f"desktop={width}x{height} game window resized to match")
                    last = (width, height)
        time.sleep(2)


def find_minecraft_window():
    """The game window's id, or None. Tries name, then class, then anything."""
    for args in (["--name", "Minecraft"], ["--name", "minecraft"],
                 ["--class", "minecraft"], ["--class", "Minecraft"],
                 ["--onlyvisible", "--name", "."]):
        try:
            out = subprocess.run(["xdotool", "search", *args],
                                 capture_output=True, text=True, timeout=10).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return None
        if out:
            return out[-1]
    return None


def x_geometry():
    try:
        out = subprocess.run(["xdotool", "getdisplaygeometry"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return f"{out[0]}x{out[1]}" if len(out) == 2 else "?"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"


def publish_diag(summary: str) -> None:
    """Put what we see into the state file, so it can be checked from outside."""
    log(f"DIAG {summary}")
    subprocess.run([sys.executable, SESSION, "publish",
                    "--state", os.path.join(ROOT, "docs"),
                    "--set", f"diag={summary}",
                    "--set", f"updated={int(time.time())}"], check=False)


def focus_minecraft_window(timeout: float = 45.0) -> bool:
    """Give the Minecraft window the X keyboard focus.

    There is no window manager in the session (it would draw a bar over the top
    and shift everything the user clicks on), so the focus is set explicitly.
    Without it, clicks land on a window that never received key focus and the
    game behaves as if it is not being touched.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            found = subprocess.run(
                ["xdotool", "search", "--name", "Minecraft"],
                capture_output=True, text=True, timeout=10,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            log("note: xdotool is not available, skipping the focus fix")
            return False
        if found:
            window = found[-1]
            subprocess.run(["xdotool", "windowfocus", window], check=False)
            subprocess.run(["xdotool", "windowactivate", window], check=False)
            size = ""
            try:
                geo = subprocess.run(["xdotool", "getwindowgeometry", "--shell", window],
                                     capture_output=True, text=True, timeout=5).stdout
                size = " ".join(part for part in geo.split() if part.startswith(("WIDTH", "HEIGHT")))
            except (OSError, subprocess.SubprocessError):
                pass
            log(f"focused the Minecraft window (id {window}) {size}")
            publish_diag(f"display={os.environ.get('DISPLAY')} desktop={x_geometry()} window={window} {size}")
            return True
        time.sleep(2)
    log("note: could not find the Minecraft window to focus")
    return False


def launch_once(command, mc_dir, environment):
    logfile = open(LOG_PATH, "w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=mc_dir,
        env=environment,
        stdout=logfile,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, logfile


def watch(process, args, started: float, last_save: dict) -> tuple[str, int | None, str | None]:
    """Watch one Minecraft run. Returns (outcome, exit code, fatal line)."""
    publish("booting", "Minecraft is loading...", 80)
    booted = False
    fatal = None
    mod_problem = False
    last_size = 0
    last_growth = time.time()
    stall_reported = False
    slow_reported = False
    while process.poll() is None:
        time.sleep(3)
        if args.world_save_minutes and (time.time() - last_save["at"] > args.world_save_minutes * 60):
            last_save["at"] = time.time()
            save_world(args.mc_dir, f"autosave {time.strftime('%H:%M')} UTC")
        try:
            size = os.path.getsize(LOG_PATH)
        except OSError:
            size = last_size
        if size != last_size:
            last_size = size
            last_growth = time.time()
            stall_reported = False
        if not booted:
            ready, fatal_line, is_mod_problem = scan_log(LOG_PATH)
            if ready:
                booted = True
                publish("playing", "Minecraft is up. The window shows up in the stream.", 100)
                log("Minecraft finished loading")
            elif fatal_line:
                fatal = fatal_line
                mod_problem = mod_problem or is_mod_problem
                break
            elif not stall_reported and time.time() - last_growth > 120:
                # the game is on screen but has not written a line in two minutes
                stall_reported = True
                stuck_at = (tail(LOG_PATH, 1) or ["(empty log)"])[-1].strip()[:160]
                log(f"! Minecraft looks stuck, last log line: {stuck_at}")
                publish(
                    "error",
                    f"Minecraft looks stuck while loading (nothing in its log for 2 minutes). "
                    f"Last line: {stuck_at}",
                    85,
                )
            elif not slow_reported and time.time() - started > 240:
                slow_reported = True
                stuck_at = (tail(LOG_PATH, 1) or ["(empty log)"])[-1].strip()[:160]
                publish("booting", f"Still loading after 4 minutes. Last line: {stuck_at}", 85)
        if STOP["requested"]:
            log("stopping because the workflow was cancelled")
            stop_process(process)
            return "cancelled", process.poll(), None
        if args.max_runtime and time.time() - started > args.max_runtime * 60:
            publish("offline", "Session reached its time limit and was stopped.", 100, extra={"started": ""})
            stop_process(process)
            return "timeout", process.poll(), None
    return ("mod-error" if mod_problem else "fatal") if fatal else "exited", process.poll(), fatal


def cmd_run(args) -> int:
    # xdotool talks to the X server, so our own environment needs DISPLAY too -
    # passing it only to the game process was why focusing silently did nothing
    os.environ.setdefault("DISPLAY", ":99")
    mc_dir = args.mc_dir
    config = read_config(args.config)
    width = int(config.get("width") or 1280)
    height = int(config.get("height") or 720)
    auto_join = str(config.get("auto_join") or "").strip()

    java_bin = os.path.join(os.environ.get("JAVA_HOME", ""), "bin", "java")
    if not os.path.isfile(java_bin):
        java_bin = shutil.which("java") or "java"

    natives_dir = os.path.join(mc_dir, "versions", args.version, "natives")
    if not os.path.isdir(natives_dir) or not os.listdir(natives_dir):
        os.makedirs(natives_dir, exist_ok=True)
        try:
            minecraft_launcher_lib.natives.extract_natives(args.version, mc_dir, natives_dir)
            log(f"extracted natives into {natives_dir}")
        except Exception as error:  # noqa: BLE001
            log(f"! could not extract natives ({error})")

    options = {
        "username": args.username,
        "uuid": offline_uuid(args.username),
        "token": "",
        "launcherName": "WebMC",
        "launcherVersion": "1.0",
        "gameDirectory": mc_dir,
        "executablePath": java_bin,
        "jvmArguments": [
            f"-Xmx{args.xmx}G",
            "-Xms1G",
            "-XX:+UseG1GC",
            "-XX:MaxGCPauseMillis=50",
            "-XX:+DisableExplicitGC",
            "-Dfile.encoding=UTF-8",
        ],
        "customResolution": True,
        "resolutionWidth": str(width),
        "resolutionHeight": str(height),
        "nativesDirectory": natives_dir,
        "enableLoggingConfig": True,
    }
    if auto_join:
        options["quickPlayMultiplayer"] = auto_join

    command = minecraft_launcher_lib.command.get_minecraft_command(args.profile, mc_dir, options)
    # LWJGL 3.3+ looks at org.lwjgl.librarypath before java.library.path on some distros
    command.insert(1, f"-Djava.library.path={natives_dir}")
    command.insert(2, f"-Dorg.lwjgl.librarypath={natives_dir}")

    log("launching: " + " ".join(command[:6]) + " ...")
    log(f"resolution: {width}x{height}   heap: {args.xmx}G   user: {args.username}")
    if auto_join:
        log(f"auto join: {auto_join}")

    environment = dict(os.environ)
    environment.setdefault("DISPLAY", ":99")

    started = time.time()
    last_save = {"at": time.time()}
    for signal_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)

    attempt = 0
    quick_failures = 0
    final_code = 0
    while True:
        attempt += 1
        process, logfile = launch_once(command, mc_dir, environment)
        focus = threading.Thread(target=focus_minecraft_window, daemon=True)
        focus.start()
        watcher = threading.Thread(target=follow_desktop_size, daemon=True)
        watcher.start()
        run_started = time.time()
        outcome, code, fatal = watch(process, args, started, last_save)
        logfile.close()
        final_code = code or 0

        if outcome in ("cancelled", "timeout"):
            save_world(mc_dir, "workflow cancelled" if outcome == "cancelled" else "time limit reached")
            if outcome == "cancelled":
                publish("offline", "Session was stopped. Push a change to mods/ to start a new one.", 100,
                        extra={"url": "", "user": "", "password": ""})
            return 0

        lasted = time.time() - run_started
        log(f"Minecraft exited with code {code} after {round(lasted / 60, 1)} min ({outcome})")
        for line in tail(LOG_PATH, 40):
            log(line.rstrip())
        save_world(mc_dir, "session ended")

        quick_failures = quick_failures + 1 if lasted < 45 else 0
        remaining = (args.max_runtime * 60 - (time.time() - started)) if args.max_runtime else 3600
        can_restart = (
            not STOP["requested"]
            and outcome != "mod-error"      # the same jar will fail the same way
            and remaining > 90
            and attempt <= args.restarts
            and quick_failures < 2
        )
        if not can_restart:
            break

        log(f"starting Minecraft again ({attempt}/{args.restarts}) - it closed, the session stays up")
        publish("booting", "Minecraft closed. Starting it again...", 80)
        time.sleep(3)

    if fatal:
        publish("error", f"Minecraft could not start: {fatal}", 100,
                extra={"url": "", "user": "", "password": ""})
        return 1
    if final_code != 0:
        publish("error", f"Minecraft crashed (exit code {final_code}) and did not come back. See the run log.",
                100, extra={"url": "", "user": "", "password": ""})
        return 1
    publish("offline", "Minecraft closed and this session has no restarts left. Push a change to mods/ for a new one.",
            100, extra={"url": "", "user": "", "password": ""})
    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="WebMC launcher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="install Minecraft, Fabric and the mods")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--loader", default="fabric")
    prepare.add_argument("--mc-dir", default=os.path.expanduser("~/minecraft"))
    prepare.add_argument("--mods", default=os.path.join(ROOT, "mods"))
    prepare.add_argument("--config", default=os.path.join(ROOT, "docs", "config.json"))
    prepare.add_argument("--fabric-api", default="true")
    prepare.add_argument("--optimization-mods", default="true")
    prepare.add_argument("--no-state", action="store_true")
    prepare.set_defaults(func=cmd_prepare)

    profile = subparsers.add_parser("profile", help="print the launch profile id to use")
    profile.add_argument("--version", required=True)
    profile.add_argument("--loader", default="fabric")
    profile.add_argument("--mc-dir", default=os.path.expanduser("~/minecraft"))
    profile.set_defaults(func=cmd_profile)

    run = subparsers.add_parser("run", help="launch Minecraft and watch it")
    run.add_argument("--profile", required=True)
    run.add_argument("--version", required=True)
    run.add_argument("--mc-dir", default=os.path.expanduser("~/minecraft"))
    run.add_argument("--config", default=os.path.join(ROOT, "docs", "config.json"))
    run.add_argument("--username", default="Player")
    run.add_argument("--xmx", type=int, default=4)
    run.add_argument("--max-runtime", type=int, default=300, help="minutes")
    run.add_argument("--world-save-minutes", type=int, default=20, help="0 disables periodic saves")
    run.add_argument("--restarts", type=int, default=3, help="how often Minecraft is restarted if it closes")
    run.add_argument("--no-state", action="store_true", help="never publish or commit state (benchmarks)")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    NO_STATE["on"] = bool(getattr(args, "no_state", False))
    if isinstance(getattr(args, "fabric_api", None), str):
        args.fabric_api = args.fabric_api.strip().lower() not in ("", "false", "0", "no")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
