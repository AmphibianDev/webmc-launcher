# WebMC launcher

A launcher page for **Minecraft: Java Edition** that runs the real client on the machine
you are sitting at. Pick a version, press Play; `tools/play-local.py` installs it under
`~/.webmc/minecraft` and starts it with `java`. Nothing is streamed, nothing runs on a CI
service, and no part of the game is kept in this repository.

**The published page is the same page with no launcher behind it** — GitHub Pages cannot
start a game on a visitor's computer, so it disables Play and tells you how to run it for
real. What it does show honestly is Mojang's whole catalogue and a scan of which versions
run on which machine:

| | |
| --- | --- |
| 918 versions listed by Mojang | read from the manifest Mojang publishes |
| 279 run natively on an Apple Silicon Mac | their LWJGL ships `natives-macos-arm64` |
| 639 need Rosetta | LWJGL 2.x and 3.0–3.2 have x86_64 macOS natives only |

## Run it

```sh
python3 tools/local-ui.py            # serves 127.0.0.1:8130 and opens a browser
python3 tools/local-ui.py --port 9000
```

The first Play on a new version downloads a JDK (Adoptium) and the game from Mojang, so it
needs a network that can reach `piston-meta.mojang.com`, `piston-data.mojang.com`,
`libraries.minecraft.net`, `resources.download.minecraft.net`, `meta.fabricmc.net` and
`api.adoptium.net`. On this machine's own network those are filtered, and the launcher says
so instead of failing silently.

`tools/local-ui.py` needs Python 3.10+ for `minecraft-launcher-lib`; it does not need your
Python to have it — the page's one button creates `~/.webmc/venv` with the newest
interpreter it can find and installs the library there.

## What the launcher does

1. plans the session from `mods/` and the version you picked,
2. makes sure a JDK of the version the game asks for is present (Temurin, from Adoptium),
3. installs Minecraft, the loader and the mods into `~/.webmc/minecraft`,
4. starts the real client on this machine: `java -cp ... net.fabricmc.loader.impl.launch.knot.KnotClient`.

A jar in `mods/` that was built for another Minecraft version is left out by name, with the
reason on the page, rather than letting Fabric refuse to start.

## Where the game lives

```
~/.webmc/jdk/…                     the JDK, if this machine had none
~/.webmc/venv/                     the launcher library
~/.webmc/minecraft/versions/<v>/   version json, client jar, natives
~/.webmc/minecraft/libraries/…     LWJGL and friends
~/.webmc/minecraft/assets/…        textures and sounds, by hash
~/.webmc/minecraft/mods/…          copies of mods/ plus Fabric API
```

## The playability scan

`tools/scan-playable.py` answers "which of these can this machine actually run?" from
evidence, and writes `playable.json`:

* **live** (when the network allows): every version's own json is read, and the answer
  comes from the `natives-macos-arm64` classifiers Mojang itself lists, plus Fabric, Quilt
  and Legacy Fabric's meta services and Adoptium's availability;
* **offline** (what `playable.json` here is): versions whose json this project had already
  parsed contribute java + lwjgl directly (326 of them), the rest are classified against
  the LWJGL boundary — 3.3.0 added macOS arm64 natives, so 1.19 and newer run natively —
  and every row says which of the two it was.

One thing is deliberately not wired up: versions in the Rosetta column need an x86_64 JDK,
and the play path fetches an arm64 one today. The page says so instead of starting a game
that cannot load its own graphics libraries.

## What is not here, on purpose

No workflow runs the game. Running a Minecraft session on GitHub-hosted runners is
"activity unrelated to the production, testing, deployment, or publication of the software
project" under GitHub's Additional Product Terms, it is what got an earlier account of this
project suspended, and a previous attempt at this project streamed a session off a runner.
The pages here are static files; the game runs on your hardware.

The mods are not redistributed either: `mods/` is a folder with a README, and the jars come
from their own authors.

## Licence

There is no `LICENSE` file yet, which means all rights reserved by default. The code here is
this project's own; if you want to reuse it, ask, and it will be licensed properly.
