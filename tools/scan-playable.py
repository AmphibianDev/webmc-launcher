#!/usr/bin/env python3
"""Which of Mojang's 918 versions this machine can actually play.

The launcher lists all of Mojang's catalogue, but the local play path runs the real
client natively on an Apple Silicon Mac, and the catalogue does not say whether a
version can survive that. One fact decides almost all of it:

    LWJGL 2.x, and LWJGL 3.0-3.2, ship **x86_64 macOS natives only**.
    LWJGL 3.3.0 added `natives-macos-arm64` (see its release notes), so from the
    Minecraft version that first used 3.3 - 1.19 - the game runs natively on arm64.

So a version is one of:

    native       its LWJGL has macOS arm64 natives; it runs here as it is
    rosetta      x86_64 natives only; playable, but through an x64 JDK under Rosetta
    unsupported  no macOS natives at all
    no-client    Mojang publishes no client jar for it

Two ways to answer that, and this script does whichever it can:

    python3 tools/scan-playable.py             # ask Mojang and the loaders themselves
    python3 tools/scan-playable.py --offline   # answer from what this repo already cached

The live mode reads each version's own json (`libraries[].downloads.classifiers` and
the older `natives` map) - the real classifier list, not a rule of thumb - plus
Fabric/Quilt/Legacy Fabric's meta services and Adoptium for the JDK. That is the
authoritative answer and it is what the script tries first.

This machine cannot do that right now: its resolver answers 0.0.0.0 for
launchermeta/piston-meta/piston-data.mojang.com, libraries.minecraft.net,
resources.download.minecraft.net, meta.fabricmc.net, meta.quiltmc.org,
meta.legacyfabric.net and api.adoptium.net, so every one of those requests fails at
connect time. The launcher cannot download a game here either, for the same reason.

Offline mode therefore answers from evidence already in the workspace and labels every
field with where it came from, so nothing here is presented as verified when it is not:

    document   parsed out of that version's own Mojang json by webmc-port/tools/route.py
               (kept in webmc-port/site/routes.json: java + lwjgl per version)
    boundary   not in a cached document; filled in from the LWJGL release boundary
               above, anchored on Mojang's own ordering of the catalogue
    cache      loader support lists cached from the loaders' meta services
               (webmc-port/site/fabric.json, legacy-fabric.json)
    unchecked  could not be checked without the network (client jars)

The result is written to launcher/playable.json, which the launcher page reads to
decide what to offer. Nothing here writes into docs/ - that is what gets deployed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import pathlib
import platform
import subprocess
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
WORKSPACE = ROOT.parent
OUT = ROOT / "launcher" / "playable.json"
CACHE = pathlib.Path.home() / ".webmc" / "cache" / "scan"

CATALOGUE = ROOT / "docs" / "versions.json"          # the manifest, as this repo fetched it
ROUTES = WORKSPACE / "webmc-port" / "site" / "routes.json"
LOADER_CACHE = {
    "fabric": WORKSPACE / "webmc-port" / "site" / "fabric.json",
    "legacy-fabric": WORKSPACE / "webmc-port" / "site" / "legacy-fabric.json",
}

MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
LOADER_META = {
    "fabric": "https://meta.fabricmc.net/v2/versions/game",
    "quilt": "https://meta.quiltmc.org/v3/versions/game",
    "legacy-fabric": "https://meta.legacyfabric.net/v2/versions/game",
}
ADOPTIUM = ("https://api.adoptium.net/v3/assets/latest/{feature}/hotspot"
            "?os=mac&architecture={arch}&image_type=jdk")
LWJGL_ARM64_NOTES = "https://github.com/LWJGL/lwjgl3/blob/master/doc/notes/3.3.0.md"
WORKERS = 10
TIMEOUT = 40

# The LWJGL boundary, newest first. A version takes the class of the first anchor it is
# not older than, using Mojang's own ordering of the catalogue rather than a version
# parser: snapshots, "13w41b", "b1.8.1" and "in-20100223" all sort correctly that way,
# and none of them parse cleanly as a dotted number.
BOUNDARIES: list[tuple[str, str]] = [
    # anchor id     lwjgl it shipped with (the verdict follows from it)
    ("1.20.5",      "3.3.3"),
    ("1.19",        "3.3.1"),
    ("1.17",        "3.2.2"),
    ("1.15",        "3.2.1"),
    ("1.13",        "3.1.2"),
    ("1.6.1",       "2.9.4-nightly-20150209"),
    ("1.0",         "2.9.1"),
]
OLDEST = "2.9.0"

# The Java boundary, same idea, from Mojang's own version documents (the same values
# webmc-port/fixtures/java-levels.json records, which is where these came from).
JAVA_BOUNDARIES: list[tuple[str, int]] = [
    ("26.1", 25), ("1.20.5", 21), ("1.18", 17), ("1.17", 16), ("1.13", 8),
]
JAVA_OLDEST = 8


# --------------------------------------------------------------------------- #
# what this repo already knows
# --------------------------------------------------------------------------- #

def catalogue() -> list[dict]:
    """Mojang's catalogue, newest first, as this repo cached it."""
    return json.loads(CATALOGUE.read_text())["versions"]


def cached_documents() -> dict[str, dict]:
    """java + lwjgl for the versions webmc-port parsed a real Mojang json for."""
    if not ROUTES.is_file():
        return {}
    versions = json.loads(ROUTES.read_text()).get("versions", {})
    return {vid: info for vid, info in versions.items()
            if info.get("metadata") == "document" and (info.get("java") or info.get("lwjgl"))}


def cached_loaders() -> dict[str, set[str]]:
    support: dict[str, set[str]] = {}
    for loader, path in LOADER_CACHE.items():
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            support[loader] = set()
            continue
        listed = data.get("supported") or data.get("stable") or []
        support[loader] = {str(item.get("version") if isinstance(item, dict) else item)
                           for item in listed}
    return support


def mods_asked_for() -> list[str]:
    try:
        spec = importlib.util.spec_from_file_location("webmc_play_local", HERE / "play-local.py")
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        info = module.jar_mod_info
    except Exception:  # noqa: BLE001
        return []
    wanted: list[str] = []
    for jar in sorted((ROOT / "mods").glob("*.jar")):
        detail = info(jar) or {}
        value = detail.get("minecraft")
        for part in (value if isinstance(value, list) else [value]):
            if part:
                wanted += [piece.strip() for piece in str(part).split(",") if piece.strip()]
    return sorted(set(wanted))


def mods_fit(version: str, asked: list[str]) -> bool:
    """A jar that asks for ~26.2 does not fit 1.21.5, and saying so is the point."""
    for want in asked:
        bare = want.lstrip("~^>=")
        if bare == version:
            return True
        if "." in bare and version.startswith(bare.rsplit(".", 1)[0] + "."):
            return True
    return False


# --------------------------------------------------------------------------- #
# the boundary answer
# --------------------------------------------------------------------------- #

def boundary_for(version_id: str, anchors: list[tuple[str, object]],
                 oldest: object, order: dict[str, int]) -> tuple[object, str]:
    """Step down the anchors until one is at or before this version in the catalogue.

    An anchor that the catalogue does not list is skipped, so a boundary named for a
    version this repo's copy does not carry cannot silently classify everything as
    'oldest'.
    """
    place = order.get(version_id)
    for anchor, value in anchors:
        anchor_place = order.get(str(anchor))
        if anchor_place is None:
            continue
        if place is None or place <= anchor_place:
            return value, anchor
    return oldest, "the oldest anchor"


def offline_report() -> dict:
    versions = catalogue()
    order = {entry["id"]: index for index, entry in enumerate(versions)}
    documents = cached_documents()
    loaders = cached_loaders()
    asked = mods_asked_for()
    arm = platform.machine().lower() in ("arm64", "aarch64")

    rows: list[dict] = []
    for entry in versions:
        version = entry["id"]
        document = documents.get(version, {})
        if document.get("lwjgl"):
            lwjgl, lwjgl_from = str(document["lwjgl"]), "document"
        else:
            lwjgl, anchor = boundary_for(version, BOUNDARIES, OLDEST, order)
            lwjgl_from = f"boundary ({anchor})"
        if document.get("java"):
            java, java_from = document["java"], "document"
        else:
            java, anchor = boundary_for(version, JAVA_BOUNDARIES, JAVA_OLDEST, order)
            java_from = f"boundary ({anchor})"

        verdict, why = verdict_from_lwjgl(lwjgl, arm)
        rows.append({
            "id": version, "type": entry.get("type"), "released": entry.get("released"),
            "java": java, "lwjgl": lwjgl,
            "verdict": verdict, "reason": why,
            "loaders": sorted(name for name, supported in loaders.items() if version in supported),
            "loader_unknown": [] if "quilt" in loaders else ["quilt"],
            "mods": mods_fit(version, asked),
            "provenance": {"java": java_from, "lwjgl": lwjgl_from,
                           "natives": "document" if lwjgl_from == "document" else "lwjgl boundary",
                           "client": "unchecked (no network)"},
        })

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    playable = counts.get("native", 0) + counts.get("rosetta", 0)
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "offline",
        "machine": {"system": platform.system(), "arch": platform.machine(),
                    "play_path": "native client, no stream"},
        "counts": {"total": len(rows), "playable": playable, **counts},
        "evidence": {
            "documents": f"{len(documents)} versions have java+lwjgl from their own Mojang json"
                         " (webmc-port/site/routes.json)",
            "loaders": {name: len(supported) for name, supported in loaders.items()},
            "lwjgl_arm64_from": "LWJGL 3.3.0 added natives-macos-arm64 " + LWJGL_ARM64_NOTES,
            "not_checked": ["each version's client jar (needs Mojang's own json)",
                            "Quilt's supported list (no cached copy here)",
                            "the JDK Adoptium still publishes per major (needs api.adoptium.net)"],
            "why_offline": "this machine's resolver answers 0.0.0.0 for Mojang, the loaders' "
                           "meta services and Adoptium, so the live scan cannot connect",
        },
        "mods_asked_for": asked,
        "versions": rows,
    }


def verdict_from_lwjgl(lwjgl: str, arm: bool) -> tuple[str, str]:
    if lwjgl.startswith("2"):
        family = f"LWJGL {lwjgl}, which has no macOS arm64 natives"
        return ("rosetta" if arm else "native"), family
    parts = [int(p) for p in lwjgl.split(".")[:2] if p.isdigit()]
    if len(parts) == 2 and parts >= [3, 3]:
        return "native", f"LWJGL {lwjgl}, which ships natives-macos-arm64"
    return ("rosetta" if arm else "native"), f"LWJGL {lwjgl}, whose macOS natives are x86_64 only"


# --------------------------------------------------------------------------- #
# the live answer, for when the network is there
# --------------------------------------------------------------------------- #

def cache_path(url: str) -> pathlib.Path:
    name = url.replace("https://", "").replace("/", "_").replace("?", "_").replace(":", "_")
    return CACHE / name[:180]


def get(url: str, refresh: bool = False) -> bytes:
    target = cache_path(url)
    if target.is_file() and not refresh:
        return target.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "webmc-playable-scan/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
        body = answer.read()
    CACHE.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return body


def get_json(url: str, refresh: bool = False):
    return json.loads(get(url, refresh))


def lwjgl_facts(libraries: list[dict]) -> dict:
    """The LWJGL a version links against, and which macOS natives its own json lists."""
    versions: list[str] = []
    arm64 = macos_x64 = False
    for library in libraries or []:
        name = str(library.get("name") or "")
        group, _, rest = name.partition(":")
        artifact, _, version = rest.partition(":")
        if group.startswith("org.lwjgl") and artifact in ("lwjgl", "lwjgl-platform",
                                                          "lwjgl-glfw", "lwjgl-opengl"):
            if version and version not in versions:
                versions.append(version)
        keys = list((library.get("downloads") or {}).get("classifiers", {}).keys())
        keys += [str(v) for v in (library.get("natives") or {}).values()]
        for key in keys:
            low = key.lower()
            if "arm64" in low and "macos" in low:
                arm64 = True
            elif "macos" in low or "osx" in low:
                macos_x64 = True
    return {"lwjgl": versions[0] if versions else None,
            "natives_macos_arm64": arm64, "natives_macos_x64": macos_x64}


def version_facts(entry: dict, refresh: bool = False) -> dict:
    facts = {"id": entry["id"], "type": entry.get("type"), "released": entry.get("released")}
    try:
        detail = get_json(entry["url"], refresh)
    except Exception as problem:  # noqa: BLE001
        facts.update({"verdict": "unknown", "reason": f"its own json could not be read: {problem}"})
        return facts
    client = ((detail.get("downloads") or {}).get("client") or {})
    facts["client"] = bool(client.get("url"))
    facts["client_mb"] = round((client.get("size") or 0) / 1048576, 1)
    facts["java"] = (detail.get("javaVersion") or {}).get("majorVersion")
    facts.update(lwjgl_facts(detail.get("libraries") or []))
    return facts


def jdk_available(feature: int, arch: str, refresh: bool = False) -> bool:
    try:
        return bool(get_json(ADOPTIUM.format(feature=feature, arch=arch), refresh))
    except Exception:  # noqa: BLE001
        return False


def rosetta_installed() -> bool:
    try:
        return subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def live_report(refresh: bool = False, quiet: bool = False) -> dict:
    manifest = get_json(MANIFEST, refresh)
    versions = manifest["versions"]
    if not quiet:
        print(f"  {len(versions)} versions in Mojang's manifest; asking each one about itself")
    facts: list[dict] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(version_facts, entry, refresh) for entry in versions]
        for future in concurrent.futures.as_completed(futures):
            facts.append(future.result())
            done += 1
            if not quiet and done % 150 == 0:
                print(f"    {done}/{len(versions)}")
    order = {entry["id"]: index for index, entry in enumerate(versions)}
    facts.sort(key=lambda item: order.get(item["id"], 0))

    support: dict[str, set[str]] = {}
    for loader, url in LOADER_META.items():
        try:
            entries = get_json(url, refresh)
        except Exception:  # noqa: BLE001
            support[loader] = set()
            continue
        if isinstance(entries, dict):
            entries = entries.get("game_versions", [])
        support[loader] = {str(item.get("version") if isinstance(item, dict) else item)
                           for item in entries}

    arm = platform.machine().lower() in ("arm64", "aarch64")
    rosetta = rosetta_installed() if arm else True
    majors = sorted({item.get("java") for item in facts if item.get("java")})
    jdks = {"aarch64": {f: jdk_available(f, "aarch64", refresh) for f in majors},
            "x64": {f: jdk_available(f, "x64", refresh) for f in majors}}
    asked = mods_asked_for()
    if not quiet:
        print(f"  this machine: {platform.system()} {platform.machine()}, rosetta={rosetta}")

    for item in facts:
        item["loaders"] = sorted(name for name, supported in support.items()
                                 if item["id"] in supported)
        item["loader_unknown"] = []
        item["mods"] = mods_fit(item["id"], asked)
        item["provenance"] = {"java": "document", "lwjgl": "document",
                              "natives": "document", "client": "document"}
        if not item.get("client"):
            item.update({"verdict": "no-client", "reason": "Mojang publishes no client jar for it"})
            continue
        if item.get("natives_macos_arm64") or not arm:
            arch, why = "aarch64", "its own json lists macOS arm64 natives"
        elif item.get("natives_macos_x64"):
            if not rosetta:
                item.update({"verdict": "no-rosetta",
                             "reason": "x86_64 natives only, and Rosetta is not installed here"})
                continue
            arch, why = "x64", f"x86_64 natives only (LWJGL {item.get('lwjgl') or '?'}), so Rosetta"
        else:
            item.update({"verdict": "unsupported", "reason": "no macOS natives in its json"})
            continue
        feature = item.get("java")
        if feature and not jdks.get(arch, {}).get(feature):
            item.update({"verdict": "no-jdk",
                         "reason": f"asks for Java {feature}, which Adoptium has no macOS {arch} build of"})
            continue
        item.update({"verdict": "native" if arch == "aarch64" else "rosetta",
                     "java_arch": arch, "reason": why})

    counts: dict[str, int] = {}
    for item in facts:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "live",
        "machine": {"system": platform.system(), "arch": platform.machine(),
                    "rosetta": rosetta, "play_path": "native client, no stream"},
        "counts": {"total": len(facts), "playable": counts.get("native", 0) + counts.get("rosetta", 0),
                   **counts},
        "evidence": {"documents": "every version read its own json",
                     "loaders": {name: len(s) for name, s in support.items()},
                     "jdks": jdks,
                     "lwjgl_arm64_from": "natives-macos-arm64 classifiers in each version json"},
        "mods_asked_for": asked,
        "versions": facts,
    }


# --------------------------------------------------------------------------- #

def summarise(report: dict) -> None:
    counts = report["counts"]
    print()
    print(f"  what this machine can play  ({report['mode']} scan)")
    print("  " + "-" * 54)
    print(f"  {counts['total']} versions listed by Mojang")
    print(f"  {counts.get('native', 0):>4}  native here (macOS arm64 natives, LWJGL 3.3+)")
    print(f"  {counts.get('rosetta', 0):>4}  under Rosetta (x86_64 natives: LWJGL 2 and 3.0-3.2)")
    for verdict in ("no-rosetta", "no-jdk", "unsupported", "no-client", "unknown"):
        if counts.get(verdict):
            print(f"  {counts[verdict]:>4}  {verdict}")
    print()
    evidence = report["evidence"]
    for name, count in evidence.get("loaders", {}).items():
        print(f"  loader {name:<14} supports {count} of them")
    native = [row["id"] for row in report["versions"] if row["verdict"] == "native"]
    if native:
        print(f"  newest native: {', '.join(native[:5])}")
    print(f"  evidence: {evidence.get('documents')}")
    if report["mode"] == "offline":
        print(f"  not checked: {', '.join(evidence.get('not_checked', []))}")
        print(f"  why: {evidence.get('why_offline')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan which Minecraft versions this Mac can play")
    parser.add_argument("--offline", action="store_true", help="answer from local evidence only")
    parser.add_argument("--live", action="store_true", help="refuse to fall back to local evidence")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache and re-ask")
    parser.add_argument("--summary", action="store_true", help="only report the last scan")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.summary:
        if not OUT.is_file():
            print(f"  no scan yet: {OUT} does not exist")
            return 2
        summarise(json.loads(OUT.read_text()))
        return 0

    report: dict
    if args.offline:
        report = offline_report()
    else:
        try:
            report = live_report(refresh=args.refresh, quiet=args.quiet)
        except Exception as problem:  # noqa: BLE001
            if args.live:
                print(f"  the live scan failed: {problem}")
                return 1
            if not args.quiet:
                print(f"  cannot reach Mojang from here ({problem})")
                print("  falling back to the evidence already in this workspace")
            report = offline_report()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1) + "\n")
    if not args.quiet:
        print(f"  wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
    summarise(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
