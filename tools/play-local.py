#!/usr/bin/env python3
"""Play the same session on *this* machine instead of a streamed runner.

The streamed edition needs a cloud runner because it has to show the game to someone who
will not install anything. If the someone is you, that trade is all cost: the picture
travels thousands of kilometres (measured: ~330 ms round trip, ~500 ms from click to
change) and a session takes minutes to start, while a machine that already has the files
starts the game in seconds at native speed, with any Fabric mod, any Minecraft version.

This is that path, and it deliberately reuses the repo's own planning code, so the mods
that get skipped and the reason they get skipped are the same here as on the runner:

    python3 tools/play-local.py --check      # say what it would do, change nothing
    python3 tools/play-local.py              # install what is missing, then play

What it does:

  1. plans a session exactly like the streamed edition (mods/, docs/config.json),
  2. makes sure a JDK of the version the game needs is present, downloading Adoptium's
     Temurin build from its GitHub release if it is not,
  3. installs Minecraft + Fabric into ~/.webmc/minecraft (no state published, no commits),
  4. launches the real client on this display: no stream, no tunnel, no latency.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

JDK_ROOT = pathlib.Path.home() / ".webmc" / "jdk"
DEFAULT_MC_DIR = pathlib.Path.home() / ".webmc" / "minecraft"
ADOPTIUM = "https://api.github.com/repos/adoptium/temurin{feature}-binaries/releases/latest"


def log(message: str) -> None:
    print(message, flush=True)


def read_config() -> dict:
    try:
        return json.loads((ROOT / "docs" / "config.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# platform -> which Adoptium asset this machine needs
# --------------------------------------------------------------------------- #

def jdk_asset_matches() -> list[str]:
    system = platform.system()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if system == "Darwin":
        return ["aarch64_mac", "tar.gz"] if arm else ["x64_mac", "tar.gz"]
    if system == "Linux":
        return ["aarch64_linux", "tar.gz"] if arm else ["x64_linux", "tar.gz"]
    if system == "Windows":
        return ["aarch64_windows", "zip"] if arm else ["x64_windows", "zip"]
    raise SystemExit(f"unsupported system: {system} {machine}")


def java_home_of(path: pathlib.Path) -> pathlib.Path | None:
    """A JDK tarball puts bin/java either at the root or under Contents/Home."""
    for candidate in (path, path / "Contents" / "Home"):
        if (candidate / "bin" / "java").is_file():
            return candidate
    for found in path.glob("*/Contents/Home"):
        if (found / "bin" / "java").is_file():
            return found
    for found in path.glob("*/bin/java"):
        if found.is_file():
            return found.parent.parent
    return None


def java_major(java: pathlib.Path) -> int | None:
    try:
        out = subprocess.run([str(java), "-version"], capture_output=True, text=True).stderr
    except OSError:
        return None
    for token in out.replace('"', " ").split():
        if token[:1].isdigit() and "." in token:
            return int(token.split(".")[0]) if int(token.split(".")[0]) != 1 else int(token.split(".")[1])
    return None


def find_java(needed: int, override: str | None) -> pathlib.Path | None:
    """A JDK new enough: the one asked for, an already downloaded one, or the system's."""
    candidates: list[pathlib.Path] = []
    if override:
        candidates.append(pathlib.Path(override))
    if os.environ.get("JAVA_HOME"):
        candidates.append(pathlib.Path(os.environ["JAVA_HOME"]))
    if JDK_ROOT.is_dir():
        candidates += sorted(JDK_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    system = shutil.which("java")
    if system:
        candidates.append(pathlib.Path(system).parent.parent)

    for candidate in candidates:
        home = java_home_of(candidate) if candidate.is_dir() else None
        java = (home / "bin" / "java") if home else candidate
        if not java.is_file():
            continue
        if java_major(java) is None or (java_major(java) or 0) >= needed:
            return java
    return None


def download_java(feature: int) -> pathlib.Path:
    """Adoptium's Temurin JDK from its GitHub release - the only place we fetch from."""
    wants_arm, suffix = jdk_asset_matches()
    url = ADOPTIUM.format(feature=feature)
    log(f"  looking up a JDK {feature} for this machine ...")
    with urllib.request.urlopen(url, timeout=60) as response:
        release = json.load(response)
    assets = [a for a in release.get("assets", [])
              if wants_arm in a["name"] and a["name"].endswith(suffix) and "jdk" in a["name"]
              and "debugimage" not in a["name"]]
    if not assets:
        raise SystemExit(f"no Temurin {feature} build for {wants_arm} in {release.get('tag_name')}")
    asset = assets[0]
    JDK_ROOT.mkdir(parents=True, exist_ok=True)
    target = JDK_ROOT / asset["name"].replace(".tar.gz", "").replace(".zip", "")
    if java_home_of(target):
        return java_home_of(target)  # type: ignore[return-value]
    log(f"  downloading {asset['name']} ({asset['size'] // 1048576} MB) from {release['tag_name']}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = pathlib.Path(tmp) / asset["name"]
        with urllib.request.urlopen(asset["browser_download_url"], timeout=1800) as source, \
                open(archive, "wb") as out:
            shutil.copyfileobj(source, out)
        log("  unpacking ...")
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as jar:
                jar.extractall(target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            subprocess.run(["tar", "-xzf", str(archive), "-C", str(target)], check=True)
    home = java_home_of(target)
    if not home:
        raise SystemExit(f"could not find bin/java in {target}")
    log(f"  JDK ready: {home}")
    return home


# --------------------------------------------------------------------------- #
# planning (the same rules the runner uses)
# --------------------------------------------------------------------------- #

def required_java(version: str) -> int:
    """The JDK the game itself asks for (Mojang's own version json), with a local fallback."""
    try:
        import session
        major = session.mojang_java_major(version)
        if major:
            return major
    except Exception:  # noqa: BLE001
        pass
    if version.startswith(("26.", "25.")):
        return 25
    parts = [int(p) for p in version.split(".")[:2] if p.isdigit()]
    if len(parts) == 2 and (parts > [1, 20] or version not in
                            ("1.20", "1.20.1", "1.20.2", "1.20.3", "1.20.4")):
        return 21
    return 17


# Settings that suit a machine you are sitting at, instead of the ones the CPU-only
# runner needs. Written only if the game has not been played here before, so a
# hand-tuned options.txt is never overwritten.
LOCAL_OPTIONS = """maxFps:260
renderDistance:12
simulationDistance:10
graphicsMode:1
ao:1
particles:0
entityShadows:true
clouds:true
renderClouds:true
mipmapLevels:4
biomeBlendRadius:2
enableVsync:false
pauseOnLostFocus:true
narrator:0
guiScale:0
fullscreen:false
"""


def jar_mod_info(path: pathlib.Path) -> dict:
    """id, name and the Minecraft version a Fabric/Quilt jar asks for.

    Kept here (not imported) so --check answers without the launcher library installed.
    The install step uses the runner's own copy of this, so the two cannot drift apart
    in what they do with the answer.
    """
    try:
        with zipfile.ZipFile(path) as jar:
            if "fabric.mod.json" not in jar.namelist():
                return {}
            meta = json.loads(jar.read("fabric.mod.json"))
    except Exception:  # noqa: BLE001
        return {}
    depends = meta.get("depends") or {}
    return {"id": meta.get("id"), "name": meta.get("name") or meta.get("id"),
            "minecraft": depends.get("minecraft")}


def plan(mods_dir: pathlib.Path, config: dict, version_arg: str | None,
         loader_arg: str | None) -> tuple[str, str, list[pathlib.Path]]:
    jars = sorted(mods_dir.glob("*.jar"))
    declared = []
    for jar in jars:
        info = jar_mod_info(jar)
        if info.get("minecraft"):
            declared += [str(v) for v in (info["minecraft"] if isinstance(info["minecraft"], list)
                                          else [info["minecraft"]])]

    version = (version_arg or str(config.get("minecraft_version") or "").strip() or None)
    if not version:
        version = max(declared, key=lambda v: [int(p) for p in v.split(".") if p.isdigit()] or [0]) \
            if declared else "1.21.5"
    loader = (loader_arg or str(config.get("loader") or "").strip() or "fabric")
    return version, loader, jars


def main() -> int:
    parser = argparse.ArgumentParser(description="Play this session locally, no server, no stream")
    parser.add_argument("--check", action="store_true", help="print the plan and stop")
    parser.add_argument("--version", default=None, help="Minecraft version (default: from mods/config)")
    parser.add_argument("--loader", default=None, help="fabric, quilt or vanilla")
    parser.add_argument("--xmx", type=int, default=None, help="heap in GB")
    parser.add_argument("--username", default=None, help="offline player name")
    parser.add_argument("--java-home", default=None, help="use this JDK instead of downloading one")
    parser.add_argument("--mc-dir", default=str(DEFAULT_MC_DIR), help="where the game lives")
    parser.add_argument("--seconds", type=int, default=0, help="stop after N seconds (0 = play until closed)")
    args = parser.parse_args()

    config = read_config()
    mods_dir = ROOT / "mods"
    version, loader, jars = plan(mods_dir, config, args.version, args.loader)
    xmx = args.xmx or int(config.get("xmx_gb") or 4)
    username = args.username or str(config.get("username") or "Player")

    print()
    print("  WebMC, on this machine")
    print("  " + "-" * 46)
    print(f"  Minecraft   {version} + {loader}")
    print(f"  mods        {len(jars)} jar(s) in {mods_dir.relative_to(ROOT)}")
    for jar in jars:
        print(f"                {jar.name}")
    print(f"  game folder {args.mc_dir}")
    print(f"  heap        {xmx} GB")
    print(f"  java        {required_java(version)} (downloaded here if this machine has none)")
    print(f"  display     this screen (no stream, no tunnel, no runner)")
    print()

    if args.check:
        print("  --check: nothing downloaded, nothing installed")
        return 0

    try:
        import minecraft_launcher_lib  # noqa: F401
    except ImportError:
        print("  The Minecraft launcher library is missing. Install it once with:")
        print()
        print('    python3 -m pip install --user "minecraft-launcher-lib>=8.0"')
        print()
        print("  (the streamed edition installs the same library on the runner)")
        return 2

    # 1. the JDK the game needs: this machine's, one downloaded earlier, or Adoptium's
    needed = required_java(version)
    java = find_java(needed, args.java_home)
    if not java:
        home = download_java(needed)
        java = home / "bin" / "java"
    log(f"java: {java} (major {java_major(java)})")

    # 2. install Minecraft + Fabric + the mods (no state is published, nothing is committed)
    log(f"installing Minecraft {version} + {loader} into {args.mc_dir} ...")
    os.environ["JAVA_HOME"] = str(java.parent.parent)
    prepare = [
        sys.executable, str(HERE / "launch.py"), "prepare",
        "--version", version, "--loader", loader,
        "--mc-dir", args.mc_dir, "--mods", str(mods_dir),
        "--config", str(ROOT / "docs" / "config.json"),
        "--fabric-api", "true", "--optimization-mods", str(config.get("optimization_mods") or "true"),
        "--no-state",
    ]
    if subprocess.run(prepare, cwd=ROOT).returncode != 0:
        raise SystemExit("install failed")

    options_path = pathlib.Path(args.mc_dir) / "options.txt"
    if not options_path.exists():
        options_path.write_text(LOCAL_OPTIONS)
        log("wrote local options.txt: 260 fps cap, 12 chunks, fancy graphics (change it in-game)")

    # 3. launch it here
    import launch
    import minecraft_launcher_lib

    profile = launch.find_profile(args.mc_dir, version, loader)
    natives = pathlib.Path(args.mc_dir) / "versions" / version / "natives"
    options = {
        "username": username,
        "uuid": launch.offline_uuid(username),
        "token": "",
        "launcherName": "webmc-local",
        "launcherVersion": "1.0",
        "gameDirectory": args.mc_dir,
        "executablePath": str(java),
        "jvmArguments": [f"-Xmx{xmx}G", "-Xms1G",
                         f"-Djava.library.path={natives}",
                         f"-Dorg.lwjgl.librarypath={natives}"],
    }
    command = minecraft_launcher_lib.command.get_minecraft_command(profile, args.mc_dir, options)
    log("starting Minecraft ...")
    if args.seconds:
        finished = subprocess.run(command, cwd=args.mc_dir, timeout=args.seconds)
        return finished.returncode
    return subprocess.call(command, cwd=args.mc_dir)


if __name__ == "__main__":
    sys.exit(main())
