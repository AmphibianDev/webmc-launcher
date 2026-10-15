# Selkies + Minecraft, in a container

A streaming host for this project: [Selkies](https://github.com/selkies-project/selkies)
turns a Linux X11 screen into an HTML5 stream, and this image puts the launcher's own
`play-local.py` on that screen. The result is one `docker run` on a machine you own, and a
Minecraft session in a browser tab.

It is the container edition of `tools/stream.sh`. That script is the same stack assembled by
hand (Xvfb → encoder → tunnel) and is still the right thing when the host is a plain Linux
box with no Docker. This is the right thing when the host has Docker — which is most VMs,
and an Apple Silicon Mac.

| | |
| --- | --- |
| **the launcher page** (`index.html`) | Minecraft starts on the machine that opened the page. No encoder, no tunnel, no latency. |
| **`tools/stream.sh`** | Minecraft runs on a Linux host and a browser elsewhere watches it, through a temporary tunnel. |
| **this image** | The same as `stream.sh`, packaged: Selkies, its desktop, and this project's tools. |

## Run it

On a host with Docker:

```sh
bash stream/run.sh --password pick-something        # http://127.0.0.1:8080
WEBMC_STREAM_PASSWORD=... bash stream/run.sh --public
WEBMC_STREAM_PASSWORD=... bash stream/run.sh --tunnel   # needs cloudflared
```

Then open <http://localhost:8080>, log in as `webmc`, and the desktop comes up with Minecraft
starting in it. The first run downloads a JDK (Adoptium), the client (Mojang), Fabric and the
assets, which takes a few minutes; after that they are in the mounted volume and it is quick.

The raw command, if you would rather see it:

```sh
docker run -d --name webmc-stream --shm-size=2g \
  -p 127.0.0.1:8080:8080 \
  -e SELKIES_BASIC_AUTH_USER=webmc \
  -e SELKIES_BASIC_AUTH_PASSWORD=pick-something \
  -v ~/.webmc-stream:/home/ubuntu/.webmc \
  ghcr.io/amphibbiandev/webmc-launcher-stream:latest
```

`--shm-size=2g` is not optional: the desktop's browsers crash on Docker's 64 MB default.

### Dials

Environment variables on the container. The stream settings are Selkies' own, and the
defaults here are the ones `tools/stream.sh` arrived at.

| variable | default | what it does |
| --- | --- | --- |
| `WEBMC_VERSION` | from the mods, else `1.21.5` | which Minecraft to install |
| `WEBMC_LOADER` | `fabric` | `fabric`, `quilt` or `vanilla` |
| `WEBMC_USERNAME` | `Player` | the offline player name in game |
| `WEBMC_XMX` | `4` | heap in GB |
| `WEBMC_SECONDS` | `0` | stop the game after N seconds — a smoke test, not a way to run a session |
| `SELKIES_MANUAL_WIDTH` / `..._HEIGHT` | `1280` / `720` | the streamed resolution; the cheapest dial there is |
| `SELKIES_FRAMERATE` | `30` | frame cap |
| `SELKIES_VIDEO_BITRATE` | `4000` | kbps cap |
| `SELKIES_VIDEO_CRF` | `28` | higher is a cheaper picture |
| `SELKIES_ENCODER` | `h264enc` | `h264enc` is x264 on a CPU-only host; `jpeg` is the last-resort one |
| `SELKIES_MODE` | `websockets` | `webrtc` is opt-in and needs the TURN ports published as well |
| `SELKIES_ENABLE_HTTPS` | `false` | off because whatever is in front terminates TLS; set it true to expose `:8080` directly |

`SELKIES_RAW_POINTER_MOTION=true`, `SELKIES_ENABLE_CURSORS=false` and
`SELKIES_USE_BROWSER_CURSORS=false` are set in the image and are load-bearing rather than
cosmetic: two cursors — the client's CSS one and the remote X one — are what made the camera
spin, and pointer lock is how Minecraft wants to be driven.

## Where it may run

**A machine you own or rent.** That is the rule this whole project is built around, and
`webmc-port`'s `COMPLIANCE.md` writes it out in full:

* your own machine, or a friend's;
* **Oracle Cloud Always Free** — 4 ARM cores, 24 GB RAM, 10 TB egress a month, permanently.
  The image is published for `linux/arm64` for exactly this;
* **Azure for Students** — no card, and the credit runs a 4 vCPU box for weeks;
* a VPS you pay for. A stream costs roughly $33/month of CPU, or ~$1.25/hour of GPU.

**Not a CI service, and not this repository's workflows.** GitHub Actions and any hosted
runner may be used for building, testing, deploying and publishing this software — not as a
game host or a streaming box:

> "If using GitHub-hosted runners, any other activity unrelated to the production, testing,
> deployment, or publication of the software project associated with the repository where
> GitHub Actions are used." … "Misuse of GitHub Actions may result in termination of jobs,
> restrictions in your ability to use GitHub Actions, disabling of repositories created to
> run Actions in a way that violates these Terms, or in some cases, suspension or
> termination of your GitHub account."
> — [Additional Product Terms § Actions](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features)

A suspended account is not recoverable by argument, and a stream is not worth one. So
`.github/workflows/publish-stream-image.yml` **builds and publishes this image and stops
there** — there is no job in it that a player could reach. The session runs where you run it.

The other half of the same rule: the image carries no game. `play-local.py` fetches the JDK,
the client and the loader from their own authors at first run, and `mods/` here is a README
rather than a folder of jars. Nothing compiled by or for Minecraft is redistributed.

## Comfortable, or a slideshow

The host's CPU is the whole story at 720p/30 on one core:

* `x264` encodes about 18× realtime at 480p, so the encoder is not the bottleneck;
  Minecraft's own renderer is;
* software GL (llvmpipe) is what runs with no `/dev/dri`, and it is genuinely usable at
  720p — it is what the earlier session on a 4-core runner used. `run.sh` passes
  `/dev/dri` in automatically when the host has one, which is the difference between
  smooth and a slideshow;
* one session per host is realistic on CPU-only machines.

## Building it yourself

```sh
docker build -f stream/Dockerfile -t webmc-launcher-stream:local .
bash stream/run.sh --image webmc-launcher-stream:local --password ...
```

For a host with no registry access — or no wish for one — `docker save` the image, copy it
over, and `docker load` it there. It is a normal OCI image for `linux/amd64` and
`linux/arm64` either way.

## What is in the image

| | |
| --- | --- |
| `ghcr.io/selkies-project/selkies/desktop` | Selkies, Xvfb, Mesa (llvmpipe), PipeWire, s6, an LXQt desktop |
| `python3` + `minecraft-launcher-lib` | in a venv at `/opt/webmc/venv`, so `play-local.py` and the `launch.py` it calls share one interpreter |
| `xdotool` | `launch.py` keeps the game window the size of the desktop with it |
| `tools/`, `mods/` | this repository's own code, at `/opt/webmc`, so `play-local.py` resolves `ROOT` the way it does on a desktop |
| `/etc/xdg/autostart/webmc-play.desktop` | starts the game inside the session, through `stream/webmc-play.sh` |

No password, no game, no jars, and no session that starts on its own.

## If something goes wrong

```sh
docker logs -f webmc-stream                              # Selkies and the container
docker exec -u ubuntu webmc-stream tail -n 40 ~/webmc-play.log   # the launcher and the game
docker exec webmc-stream selkies-gpu-probe                # what GPU the session actually found
```

The usual ones: the game at the wrong size (no `xdotool`, or the desktop has not resized
yet), a black picture (software GL without `--device /dev/dri`, or an encoder the browser
will not decode — the page steps through them), and `piston-meta.mojang.com` unreachable
from the host, which the launcher reports by name rather than failing silently.

If the browser cannot decode H.264 at all, `tools/stream.sh` in this repository has the
`novnc` fallback (x11vnc + noVNC, no codec to negotiate). A container for that variant does
not exist yet; the script is the path.
