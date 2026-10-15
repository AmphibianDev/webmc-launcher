#!/usr/bin/env python3
"""
WebMC state helper.

Two jobs:

  detect   Look at every jar in mods/, work out which Minecraft version and
           which mod loader they need, filter out mods that do not fit, warn
           about missing dependencies, then write:
                docs/mods.json     (what is installed)
                docs/session.json  (initial "preparing" state)
           and expose the results as GitHub Actions outputs.

  publish  Merge new values into docs/session.json and optionally commit +
           push it, so the GitHub Pages site can show live status without
           anybody needing a token.

Only the Python standard library is used here, so `detect` can run before
anything is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOJANG_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

# ids that are always available and never count as "missing"
BUILTIN_DEP_IDS = {
    "minecraft", "java", "fabricloader", "fabric-loader", "fabric", "fabric-api-base",
    "quilt_loader", "quilt_base", "quilted_fabric_api", "forge", "neoforge", "neoforge_loader",
}
# dependencies we know how to add automatically
AUTO_DEP_IDS = {"fabric-api"}

STATUS_IDLE = "idle"


# --------------------------------------------------------------------------- #
# version helpers
# --------------------------------------------------------------------------- #
def vkey(value: str) -> tuple:
    """Sortable key for versions like 1.21.5, 26.2, 1.21.5-rc1."""
    parts = []
    for chunk in re.split(r"[.\-_+]", str(value)):
        if chunk.isdigit():
            parts.append((0, int(chunk), ""))
        else:
            parts.append((1, 0, chunk))
    return tuple(parts)


def bare_versions(spec) -> list:
    """Every literal version mentioned in a dependency spec."""
    found = []
    if spec is None:
        return found
    if isinstance(spec, list):
        for item in spec:
            found.extend(bare_versions(item))
        return found
    for part in re.split(r"\s*\|\|\s*|\s+", str(spec)):
        part = part.strip()
        if not part or part == "*":
            continue
        if re.match(r"^(\^|~|>=|<=|>|<|=|==)", part):
            part = re.sub(r"^(\^|~|>=|<=|>|<|=|==)\s*", "", part).strip()
        if re.match(r"^\d", part):
            found.append(part)
    return found


def spec_matches(spec, version: str) -> bool:
    """Fabric-style version predicate matching, deliberately a little forgiving."""
    if not version:
        return False
    if spec is None:
        return True
    if isinstance(spec, list):
        return any(spec_matches(item, version) for item in spec)
    text = str(spec).strip()
    if text in ("", "*"):
        return True
    if "||" in text:
        return any(spec_matches(part, version) for part in text.split("||"))
    for part in text.split():
        part = part.strip()
        if part in ("", "*"):
            continue
        match = re.match(r"^(\^|~|>=|<=|>|<|=|==)?\s*(\d[^\s]*)$", part)
        if not match:
            continue
        op, want = match.group(1), match.group(2)
        have_key, want_key = vkey(version), vkey(want)
        if op == "^":
            if have_key < want_key or have_key[0] != want_key[0]:
                return False
        elif op == "~":
            if have_key < want_key or have_key[:2] != want_key[:2]:
                return False
        elif op in (None, "=", "=="):
            # a bare "1.21" means "any 1.21.x", a full "1.21.5" means exactly that
            if version != want and not version.startswith(want + "."):
                return False
        elif op == ">=" and not have_key >= want_key:
            return False
        elif op == ">" and not have_key > want_key:
            return False
        elif op == "<=" and not have_key <= want_key:
            return False
        elif op == "<" and not have_key < want_key:
            return False
    return True


def java_major_from_spec(spec) -> int | None:
    for version in bare_versions(spec):
        match = re.match(r"^(\d+)", version)
        if match:
            return int(match.group(1))
    return None


def http_json(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def mojang_java_major(version: str) -> int | None:
    """Authoritative Java requirement straight from Mojang's version json."""
    try:
        manifest = http_json(MOJANG_MANIFEST)
        entry = next((v for v in manifest["versions"] if v["id"] == version), None)
        if not entry:
            return None
        data = http_json(entry["url"])
        return data.get("javaVersion", {}).get("majorVersion")
    except Exception as error:  # noqa: BLE001 - network is best effort here
        print(f"note: could not read Java requirement from Mojang ({error})")
        return None


def latest_release() -> str | None:
    try:
        manifest = http_json(MOJANG_MANIFEST)
        return manifest["latest"]["release"]
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# jar inspection
# --------------------------------------------------------------------------- #
def dep_spec(depends: dict, *names):
    for name in names:
        if name in depends:
            return depends[name]
    return None


def inspect_jar(path: str) -> dict:
    """Read mod metadata out of a jar without running anything from it."""
    info = {
        "file": os.path.basename(path),
        "size_kb": round(os.path.getsize(path) / 1024),
        "id": None,
        "name": None,
        "version": None,
        "loader": None,
        "minecraft": None,
        "java": None,
        "depends": {},
        "problem": None,
    }
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names:
                meta = json.loads(jar.read("fabric.mod.json").decode("utf-8", "replace"))
                depends = meta.get("depends") or {}
                info.update(
                    loader="fabric",
                    id=meta.get("id"),
                    name=meta.get("name") or meta.get("id"),
                    version=meta.get("version"),
                    minecraft=dep_spec(depends, "minecraft"),
                    java=dep_spec(depends, "java"),
                    depends=depends,
                )
            elif "quilt.mod.json" in names:
                meta = json.loads(jar.read("quilt.mod.json").decode("utf-8", "replace"))
                loader_block = meta.get("quilt_loader") or {}
                depends_list = loader_block.get("depends") or []
                depends = {}
                if isinstance(depends_list, list):
                    for entry in depends_list:
                        if isinstance(entry, dict) and entry.get("id"):
                            depends[entry["id"]] = entry.get("versions", "*")
                elif isinstance(depends_list, dict):
                    depends = depends_list
                info.update(
                    loader="quilt",
                    id=loader_block.get("id"),
                    name=(loader_block.get("metadata") or {}).get("name") or loader_block.get("id"),
                    version=loader_block.get("version"),
                    minecraft=dep_spec(depends, "minecraft"),
                    java=dep_spec(depends, "java"),
                    depends=depends,
                )
            elif "META-INF/neoforge.mods.toml" in names or "META-INF/mods.toml" in names:
                raw = jar.read("META-INF/neoforge.mods.toml" if "META-INF/neoforge.mods.toml" in names else "META-INF/mods.toml")
                text = raw.decode("utf-8", "replace")
                loader = "neoforge" if "neoforge" in text.lower() else "forge"
                mod_id = re.search(r'modId\s*=\s*"([^"]+)"', text)
                info.update(
                    loader=loader,
                    id=mod_id.group(1) if mod_id else None,
                    name=mod_id.group(1) if mod_id else None,
                    version=(re.search(r'version\s*=\s*"([^"]+)"', text) or [None, None])[1]
                    if re.search(r'version\s*=\s*"([^"]+)"', text)
                    else None,
                    problem=f"{loader} jars are not supported by the simple flow (Fabric only)",
                )
            else:
                info["problem"] = "no fabric.mod.json / quilt.mod.json / mods.toml (not a normal mod jar?)"
    except zipfile.BadZipFile:
        info["problem"] = "corrupt zip file"
    except Exception as error:  # noqa: BLE001
        info["problem"] = f"could not read jar ({error})"
    return info


def collect_jars(mods_dir: str) -> tuple[list, list]:
    jars, notices = [], []
    if not os.path.isdir(mods_dir):
        return jars, notices
    for name in sorted(os.listdir(mods_dir)):
        path = os.path.join(mods_dir, name)
        if os.path.isdir(path):
            continue
        lower = name.lower()
        if lower.endswith(".jar") or lower.endswith(".disabled"):
            jars.append(path)
        elif lower.endswith((".zip", ".mrpack")):
            notices.append(
                f"{name}: mod packs are not supported here - unzip it and drop the mod .jar files into mods/"
            )
    return jars, notices


# --------------------------------------------------------------------------- #
# state files
# --------------------------------------------------------------------------- #
def read_json(path: str, fallback: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as error:  # noqa: BLE001
        print(f"warning: {path} is not valid json ({error}), using defaults")
    return dict(fallback)


def publish_version_list(state_dir: str) -> None:
    """Write every Minecraft version the launcher can install (best effort).

    The page reads this to offer a picker like a normal launcher: the workflow's
    version input is free text, so anything in this list can be started with a
    Fabric loader (assuming Fabric supports that version)."""
    try:
        manifest = http_json(MOJANG_MANIFEST, timeout=30)
    except Exception as error:  # noqa: BLE001
        print(f"note: could not publish the version list ({error})")
        return
    versions = [
        {
            "id": entry.get("id"),
            "type": entry.get("type"),
            "released": (entry.get("releaseTime") or "")[:10],
        }
        for entry in manifest.get("versions", [])
        if entry.get("id")
    ]
    write_json(
        os.path.join(state_dir, "versions.json"),
        {
            "latest": manifest.get("latest", {}),
            "count": len(versions),
            "versions": versions,
        },
    )
    print(f"published {len(versions)} Minecraft versions to the site")


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


DEFAULT_SESSION = {
    "status": STATUS_IDLE,
    "message": "No session running yet. Add a mod to mods/ (or run the Play session workflow) to start one.",
    "progress": 0,
    "url": None,
    "user": None,
    "password": None,
    "minecraft_version": None,
    "loader": None,
    "mods_count": 0,
    "started": None,
    "updated": None,
    "updated_iso": None,
    "run_url": None,
    "run_id": None,
    "max_runtime": None,
    "ends_at": None,
}

# values that should stay numbers in session.json even when they arrive as text
NUMERIC_KEYS = {
    "progress", "mods_count", "max_runtime", "ends_at", "started", "stream_fps",
    "video_bitrate", "video_crf", "fps_limit", "render_distance", "world_save_minutes",
    "client", "stream_client", "stream_encoder", "backpressure_queue", "skipped_mods",
}


def run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def gh_output(key: str, value) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    text = "true" if value is True else "false" if value is False else str(value)
    text = text.replace("\n", " ").strip()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={text}\n")


def gh_env(key: str, value) -> None:
    path = os.environ.get("GITHUB_ENV")
    if not path:
        return
    text = "true" if value is True else "false" if value is False else str(value)
    text = text.replace("\n", " ").strip()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={text}\n")


def clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


# --------------------------------------------------------------------------- #
# detect
# --------------------------------------------------------------------------- #
def git_headers() -> None:
    print("n/a")


def cmd_detect(args) -> int:
    config = read_json(args.config, {})
    session_path = os.path.join(args.state, "session.json")
    mods_manifest_path = os.path.join(args.state, "mods.json")

    jars, notices = collect_jars(args.mods)
    warnings = list(notices)
    inspected = [inspect_jar(path) for path in jars]

    # ---------------------------------------------------------------- loader
    loader_counts: dict[str, int] = {}
    for mod in inspected:
        if mod["loader"] and not mod["problem"]:
            loader_counts[mod["loader"]] = loader_counts.get(mod["loader"], 0) + 1
    auto_loader = None
    if loader_counts:
        auto_loader = max(loader_counts, key=lambda key: (loader_counts[key], key == "fabric"))
    loader = (args.loader_override or "").strip().lower() or (config.get("loader") or "").strip().lower() or auto_loader or "vanilla"
    loader = {"vanilla": "vanilla", "default": "vanilla", "fabric-loader": "fabric"}.get(loader, loader)

    usable = [mod for mod in inspected if not mod["problem"]]
    if loader == "fabric":
        skipped_other = [mod for mod in usable if mod["loader"] != "fabric"]
    else:
        skipped_other = []
    for mod in usable:
        if mod["problem"]:
            skipped_other.append(mod)

    # ------------------------------------------------------- minecraft version
    literals = []
    for mod in usable:
        if mod["loader"] != loader:
            continue
        literals.extend(bare_versions(mod["minecraft"]))
    version_override = (args.version_override or "").strip()
    config_version = str(config.get("minecraft_version") or "").strip()
    if version_override:
        version = version_override
        # The button on the site (or a manual dispatch) sends this, and it wins over the
        # file. Say so, because otherwise a pinned version in docs/config.json looks
        # ignored: the last run started 26.2 while the config said 1.21.5, and nothing
        # anywhere on the page explained why.
        version_source = "the run button (forced; it overrides docs/config.json)"
        warnings.append(
            f"{version} was forced by the run button, not by docs/config.json "
            f"({config_version or 'unset'}). Use Actions -> Play session -> Run workflow with "
            f"the version box empty to let the file decide."
        )
    elif config_version:
        version = config_version
        version_source = "docs/config.json"
    elif literals:
        version = max(set(literals), key=vkey)
        version_source = "detected from your mods"
    else:
        version = latest_release() or "1.21.5"
        version_source = "the newest release (no mod or config said which)"
        if not literals:
            warnings.append(
                "No mod declared a Minecraft version, so the newest release was used. "
                "Set minecraft_version in docs/config.json to pin one."
            )

    # ------------------------------------------------------------ pick the mods
    included, skipped = [], []
    for mod in usable:
        if mod["loader"] != loader:
            skipped.append({"file": mod["file"], "reason": f"{mod['loader']} mod, this session runs {loader}"})
            continue
        if mod["minecraft"] is not None and not spec_matches(mod["minecraft"], version):
            skipped.append(
                {"file": mod["file"], "reason": f"needs Minecraft {mod['minecraft']}, this session runs {version}"}
            )
            continue
        included.append(mod)
    for mod in inspected:
        if mod["problem"] and mod not in usable:
            skipped.append({"file": mod["file"], "reason": mod["problem"]})

    if not included:
        warnings.append(
            f"No usable {loader} mods for Minecraft {version} were found - starting a plain client. "
            "Drop a Fabric .jar into the mods/ folder."
        )

    # ------------------------------------------------------- dependency checks
    provided = set(BUILTIN_DEP_IDS)
    for mod in included:
        if mod["id"]:
            provided.add(str(mod["id"]).lower())
    missing: dict[str, list] = {}
    for mod in included:
        for dep_id, dep_spec_value in (mod["depends"] or {}).items():
            dep_key = str(dep_id).lower()
            if dep_key in provided:
                continue
            if spec_matches(dep_spec_value, version) is False and dep_id not in ("minecraft", "java"):
                continue
            missing.setdefault(dep_key, []).append(mod["file"])
    auto_api_needed = "fabric-api" in missing and loader == "fabric"
    fabric_api_enabled = bool(config.get("fabric_api", True)) and loader == "fabric"
    raw_optimization = config.get("optimization_mods", False)
    if isinstance(raw_optimization, bool):
        optimization_mods = "true" if raw_optimization else "false"
    else:
        optimization_mods = str(raw_optimization).strip() or "false"
    if loader != "fabric":
        optimization_mods = "false"
    for dep_id, files in sorted(missing.items()):
        if dep_id in AUTO_DEP_IDS and fabric_api_enabled:
            continue
        warnings.append(f"missing dependency '{dep_id}' required by: {', '.join(files)}")
    if auto_api_needed and fabric_api_enabled:
        warnings.append("Fabric API is required by one of your mods and will be downloaded automatically.")

    # --------------------------------------------------------------- java level
    java_needed = mojang_java_major(version) or 21
    for mod in included:
        wanted = java_major_from_spec(mod["java"])
        if wanted and wanted > java_needed:
            java_needed = wanted

    # ------------------------------------------------------------------- config
    xmx = clamp_int(args.xmx_override or config.get("xmx_gb"), 4, 1, 8)
    width = clamp_int(config.get("width"), 1280, 320, 3840)
    height = clamp_int(config.get("height"), 720, 180, 2160)
    username = str(config.get("username") or "Player")[:16]
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", username):
        warnings.append(f"username '{username}' is not a valid Minecraft name; using Player")
        username = "Player"
    # performance knobs: the runner has no GPU, so every one of these is real
    max_runtime = clamp_int(config.get("max_runtime_minutes"), 300, 5, 320)
    fps_limit = clamp_int(config.get("fps_limit"), 30, 5, 260)
    render_distance = clamp_int(config.get("render_distance"), 8, 2, 32)
    graphics = str(config.get("graphics") or "fast").strip().lower()
    if graphics not in ("fast", "fancy", "fabulous"):
        graphics = "fast"
    stream_fps = clamp_int(config.get("stream_fps"), 30, 5, 120)
    video_bitrate = clamp_int(config.get("video_bitrate"), 4000, 250, 100000)
    video_crf = clamp_int(config.get("video_crf"), 28, 5, 50)
    world_save_minutes = clamp_int(config.get("world_save_minutes"), 20, 0, 120)
    # CPU-only hosts do better with the striped encoder; a GPU host wants plain h264enc
    stream_encoder = str(config.get("stream_encoder") or "h264enc-striped,jpeg").strip()
    # A list, not a single name: Selkies offers the client these encoders in order and
    # the client walks them until it finds a codec it can decode, so a browser whose
    # H.264 decoder is missing falls to JPEG instead of showing "waiting for stream".
    known_encoders = ("h264enc", "h264enc-striped", "h265enc", "vp8enc", "vp9enc", "av1enc", "jpeg")
    chosen = [name.strip() for name in stream_encoder.split(",") if name.strip()]
    unknown = [name for name in chosen if name not in known_encoders]
    if not chosen or unknown:
        warnings.append(
            f"stream_encoder '{stream_encoder}' has entries Selkies does not offer "
            f"({', '.join(unknown) or 'empty'}); using h264enc-striped,jpeg"
        )
        stream_encoder = "h264enc-striped,jpeg"
    elif "jpeg" not in chosen:
        # JPEG is the encoder every browser can show without WebCodecs. Leaving it out
        # means a client that cannot decode the others has nothing to fall back to.
        stream_encoder = ",".join(chosen + ["jpeg"])

    stream_client = str(config.get("stream_client") or "selkies").strip().lower()
    if stream_client not in ("selkies", "novnc"):
        warnings.append(f"stream_client '{stream_client}' is not a frontend this project has; using selkies")
        stream_client = "selkies"
    auto_join = str(config.get("auto_join") or "").strip()
    password_setting = config.get("stream_password")
    if password_setting == "random":
        stream_password = os.urandom(9).hex()
    elif password_setting:
        stream_password = str(password_setting)
    else:
        stream_password = ""

    # -------------------------------------------------------------- write state
    mods_manifest = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minecraft_version": version,
        "loader": loader,
        "java": java_needed,
        "mods": [
            {
                "file": mod["file"],
                "id": mod["id"],
                "name": mod["name"] or mod["file"],
                "version": mod["version"],
                "minecraft": mod["minecraft"] if not isinstance(mod["minecraft"], list) else ", ".join(map(str, mod["minecraft"])),
                "loader": mod["loader"],
                "size_kb": mod["size_kb"],
                "auto": False,
            }
            for mod in included
        ],
        "skipped": skipped,
        "warnings": warnings,
    }
    write_json(mods_manifest_path, mods_manifest)

    session = read_json(session_path, DEFAULT_SESSION)
    session.update(
        {
            "status": "preparing",
            "message": f"Preparing Minecraft {version} with {len(included)} mod(s)...",
            "progress": 5,
            "minecraft_version": version,
            "loader": loader,
            "mods_count": len(included),
            "started": int(time.time()),
            "updated": int(time.time()),
            "updated_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_url": run_url(),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "max_runtime": max_runtime,
            "ends_at": int(time.time()) + max_runtime * 60,
            "url": None,
            "user": None,
            "password": None,
        }
    )
    write_json(session_path, session)

    # ------------------------------------------------------------------ summary
    print("=" * 62)
    print(f"WebMC session plan: Minecraft {version} / {loader} / Java {java_needed}")
    print(f"mods folder: {args.mods}")
    for mod in included:
        print(f"  + {mod['file']}  [{mod['id']} {mod['version']}]")
    for entry in skipped:
        print(f"  - {entry['file']}  skipped: {entry['reason']}")
    for warning in warnings:
        print(f"  ! {warning}")
    print("=" * 62)

    publish_version_list(args.state)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## WebMC session plan", "", f"* Minecraft **{version}** on **{loader}** (Java {java_needed}, {xmx} GB heap)", ""]
        lines.append(f"Mods going in ({len(included)}):" if included else "No mods going in (plain client).")
        lines += [f"* `{mod['file']}` - {mod['id']} {mod['version']}" for mod in included]
        if skipped:
            lines += ["", "Skipped:"] + [f"* `{entry['file']}` - {entry['reason']}" for entry in skipped]
        if warnings:
            lines += ["", "Warnings:"] + [f"* {text}" for text in warnings]
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError:
            pass

    outputs = {
        "version": version,
        "loader": loader,
        "java": java_needed,
        "xmx": xmx,
        "width": width,
        "height": height,
        "username": username,
        "stream_password": stream_password,
        "max_runtime": max_runtime,
        "auto_join": auto_join,
        "fabric_api": fabric_api_enabled,
        "optimization_mods": optimization_mods,
        "fps_limit": fps_limit,
        "render_distance": render_distance,
        "graphics": graphics,
        "stream_fps": stream_fps,
        "video_bitrate": video_bitrate,
        "video_crf": video_crf,
        "world_save_minutes": world_save_minutes,
        "stream_encoder": stream_encoder,
        "stream_client": stream_client,
        "stream_jpeg_quality": clamp_int(config.get("stream_jpeg_quality"), 35, 10, 95),
        "backpressure_queue": clamp_int(config.get("stream_queue"), 4, 1, 60),
        "mods_count": len(included),
        "version_source": version_source,
        "warnings_count": len(warnings),
    }
    for key, value in outputs.items():
        gh_output(key, value)
        gh_env(f"WEBMC_{key.upper()}", value)
    return 0


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def git(args: list, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def commit_state(paths: list, message: str, attempts: int = 5, rewrite=None) -> bool:
    """Commit + push the state files. Never fatal: a failed push is only a warning.

    `rewrite` recreates the files this runner's state lives in, and is called after every
    rejected push, because a rejected push here is almost never a network problem: the
    session state file conflicts with the copy the *previous* session published (one is
    cancelled the moment a new one starts). Rebasing that without rewriting leaves the
    runner with someone else's state, so the site shows the dead session's URL - which is
    how a live session ended up answering Cloudflare 1033 with no new link on the page.

    The push happens on every attempt, not only after a fresh commit: after a failed push
    the rebase may leave the commit already made, and returning early there is how a
    correct local state never reaches the repository.
    """
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    # a state file that does not exist yet (a first run) must not fail the whole publish
    paths = [path for path in paths if os.path.exists(path)]
    for attempt in range(1, attempts + 1):
        add = git(["add", *paths])
        if add.returncode != 0:
            print(f"warning: git add failed: {add.stderr.strip()}")
            return False
        if git(["diff", "--cached", "--quiet"]).returncode != 0:
            commit = git(
                [
                    "-c", "user.name=webmc-bot",
                    "-c", "user.email=webmc-bot@users.noreply.github.com",
                    "commit", "-m", message,
                ]
            )
            if commit.returncode != 0:
                print(f"warning: git commit failed: {commit.stderr.strip()}")
                return False
        else:
            # nothing to stage this time, but a previous attempt may have left a commit
            # that never made it out - ask git whether there is anything to push at all
            ahead = git(["rev-list", "--count", f"origin/{branch}..HEAD"])
            if ahead.returncode == 0 and ahead.stdout.strip() in ("", "0"):
                return True
        push = git(["push", "origin", f"HEAD:{branch}"])
        if push.returncode == 0:
            return True
        print(f"warning: git push attempt {attempt} failed: {push.stderr.strip().splitlines()[-1] if push.stderr.strip() else ''}")
        if attempt == attempts:
            break
        if git(["fetch", "origin", branch]).returncode == 0:
            # Their state wins the conflict, then this runner writes its own over it.
            if git(["rebase", "-X", "theirs", f"origin/{branch}"]).returncode != 0:
                git(["rebase", "--abort"])
                git(["reset", "--hard", f"origin/{branch}"])
            if rewrite:
                rewrite()
        time.sleep(2)
    print("warning: could not publish the state to the repository; the site may look stale")
    return False


def cmd_publish(args) -> int:
    session_path = os.path.join(args.state, "session.json")
    session = read_json(session_path, DEFAULT_SESSION)
    now = int(time.time())
    run_id = os.environ.get("GITHUB_RUN_ID")
    if getattr(args, "only_if_mine", False) and run_id:
        owner = str(session.get("run_id") or "")
        if owner and owner != run_id:
            print(f"state belongs to run {owner}, not this run ({run_id}) - not overwriting it")
            return 0
    session["updated"] = now
    session["updated_iso"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.status:  # omitted means "just merge the values below"
        session["status"] = args.status
        session["progress"] = max(0, min(int(args.progress), 100))
    if args.message:
        session["message"] = args.message
    for field in ("url", "user", "password"):
        value = getattr(args, field)
        if value is not None:
            session[field] = value or None
    if args.variables:
        for item in args.variables:
            key, _, value = item.partition("=")
            if key in NUMERIC_KEYS and re.fullmatch(r"\d+", value or ""):
                session[key] = int(value)
            else:
                session[key] = value
    if args.clear_session:
        session["url"] = session["user"] = session["password"] = None
        session["started"] = None
    if not session.get("started") and args.status and args.status != STATUS_IDLE:
        session["started"] = now
    if args.run_url and not session.get("run_url"):
        session["run_url"] = run_url()
    if run_id:
        session["run_id"] = run_id
    write_json(session_path, session)

    if args.status:
        print(f"state: {args.status} ({session['progress']}%) {session.get('message')}")
    else:
        print(f"state: values updated only ({args.variables})")
    if args.commit:
        commit_state(
            [
                session_path,
                os.path.join(args.state, "mods.json"),
                os.path.join(args.state, "versions.json"),
            ],
            f"session: {args.status}",
            rewrite=lambda: write_json(session_path, session),
        )
    return 0


# --------------------------------------------------------------------------- #
# save-world
# --------------------------------------------------------------------------- #
def cmd_save_world(args) -> int:
    """Copy the game's saves/ folder into the repository so a world outlives the
    runner. Called after the session ends (and every so often while it runs)."""
    saves, target = args.saves, args.world_dir
    if not os.path.isdir(saves):
        print(f"no saves yet ({saves} does not exist)")
        return 0
    worlds = sorted(
        name for name in os.listdir(saves) if os.path.isdir(os.path.join(saves, name))
    )
    if not worlds:
        print("no worlds to save")
        return 0
    if args.only and not any(name in args.only for name in worlds):
        return 0

    os.makedirs(target, exist_ok=True)
    for name in os.listdir(target):
        stale = os.path.join(target, name)
        if os.path.isdir(stale) and name not in worlds:
            shutil.rmtree(stale)
            print(f"removed deleted world '{name}'")

    total = 0
    for name in worlds:
        source = os.path.join(saves, name)
        destination = os.path.join(target, name)
        if os.path.isdir(destination):
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=True)
        size = sum(
            os.path.getsize(os.path.join(root, item))
            for root, _, items in os.walk(destination)
            for item in items
        )
        total += size
        print(f"saved world '{name}' ({round(size / 1048576, 1)} MB)")
    if total > 400 * 1048576:
        print(f"warning: {round(total / 1048576)} MB of worlds is a lot to keep in git")

    if args.commit:
        commit_state([target], args.message or f"world: save {len(worlds)} world(s)")
    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="WebMC state helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser("detect", help="scan mods and plan the session")
    detect.add_argument("--mods", default=os.path.join(ROOT, "mods"))
    detect.add_argument("--config", default=os.path.join(ROOT, "docs", "config.json"))
    detect.add_argument("--state", default=os.path.join(ROOT, "docs"))
    detect.add_argument("--version-override", default="")
    detect.add_argument("--loader-override", default="")
    detect.add_argument("--xmx-override", default="")
    detect.set_defaults(func=cmd_detect)

    publish = subparsers.add_parser("publish", help="update docs/session.json")
    publish.add_argument("--state", default=os.path.join(ROOT, "docs"))
    publish.add_argument("--status", default=None)
    publish.add_argument("--message", default="")
    publish.add_argument("--progress", default=0)
    publish.add_argument("--url", default=None)
    publish.add_argument("--user", default=None)
    publish.add_argument("--password", default=None)
    publish.add_argument("--set", dest="variables", action="append", default=[])
    publish.add_argument("--clear-session", action="store_true")
    publish.add_argument("--run-url", action="store_true")
    publish.add_argument("--only-if-mine", action="store_true",
                         help="do nothing if a newer workflow run already owns the state")
    publish.add_argument("--commit", action="store_true")
    publish.set_defaults(func=cmd_publish)

    save_world = subparsers.add_parser("save-world", help="copy saves/ into the repository")
    save_world.add_argument("--saves", default=os.path.join(os.path.expanduser("~"), "minecraft", "saves"))
    save_world.add_argument("--world-dir", default=os.path.join(ROOT, "world"))
    save_world.add_argument("--message", default="")
    save_world.add_argument("--only", action="append", default=[],
                            help="only save if one of these world names exists")
    save_world.add_argument("--commit", action="store_true")
    save_world.set_defaults(func=cmd_save_world)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
