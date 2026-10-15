# Streaming a session, on a machine you own

Two shapes live in this project, and they are deliberately different things:

| | |
| --- | --- |
| **the launcher page** (`index.html`) | starts Minecraft on the machine that opened the page. Latency is whatever your own machine is. |
| **this** (`tools/stream.sh`) | runs the game on one machine and lets a browser somewhere else watch it, through a temporary https URL. |

`tools/stream.sh` is the whole stack: a virtual screen (`Xvfb`) → the encoder (Selkies, or
`x11vnc` + noVNC when the browser cannot decode H.264) → a temporary Cloudflare tunnel →
an https URL. It prints that URL at the end, and writes it into `docs/session.json` when
that file exists, so a launcher page can pick it up without anybody needing a token.

## Where it may run

**A machine you own, or a VM you pay for.** That is the rule the whole project is built
around, and it is written out in full in `webmc-port`'s `COMPLIANCE.md`:

* your own machine, or a friend's — the only shape with no third party in the middle;
* **Oracle Cloud Always Free** (4 ARM cores, 24 GB RAM, 10 TB egress a month, permanently);
* **Azure for Students** (no card, credit runs a 4 vCPU box for weeks);
* a VPS you pay for (a stream costs roughly $33/month CPU, ~$1.25/hour GPU).

**Not a CI service.** GitHub Actions and any hosted runner may be used for building,
testing, deploying and publishing this software — not as a game host or a streaming box.
The terms in force say it plainly:

> "If using GitHub-hosted runners, any other activity unrelated to the production, testing,
> deployment, or publication of the software project associated with the repository where
> GitHub Actions are used." … "Misuse of GitHub Actions may result in termination of jobs,
> restrictions in your ability to use GitHub Actions, disabling of repositories created to
> run Actions in a way that violates these Terms, or in some cases, suspension or
> termination of your GitHub account."
> — [Additional Product Terms § Actions](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features)

That clause is why this repository contains **no workflow that runs the game**, and why
`play.yml` from the project's earlier life is not in it. A suspended account is not
recoverable by argument, and a stream is not worth one.

## The host needs

A Linux box, because the stack needs a virtual X display:

```sh
sudo apt-get update
sudo apt-get install -y xvfb x11vnc novnc ffmpeg \
  libgl1-mesa-dri libglx-mesa0 libgbm1   # llvmpipe, so no GPU is required
# and cloudflared, for the tunnel:
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
```

macOS has no `Xvfb`. Run the stream on a Linux VM instead, or skip `stream.sh` and share the
desktop with macOS' own Screen Sharing.

The host also has to be able to reach Mojang: `piston-meta.mojang.com`,
`piston-data.mojang.com`, `libraries.minecraft.net`, `resources.download.minecraft.net`,
`meta.fabricmc.net` (to install the game) and `api.adoptium.net` (to fetch a JDK).

## Running it

```sh
# 1. install the game on the host (same code the launcher page uses)
python3 tools/launch.py prepare --version 1.21.5 --loader fabric \
        --mc-dir ~/.webmc/minecraft --mods mods --fabric-api true

# 2. put the game on a virtual screen and open a tunnel to it
WEBMC_STREAM_PASSWORD=pick-something \
WEBMC_WIDTH=1280 WEBMC_HEIGHT=720 \
WEBMC_STREAM_CLIENT=selkies \
bash tools/stream.sh
```

Useful dials, all environment variables:

| variable | default | what it does |
| --- | --- | --- |
| `WEBMC_STREAM_CLIENT` | `selkies` | `selkies` (websocket video, audio) or `novnc` (`x11vnc` + noVNC, no codec at all — the fallback when a browser cannot decode H.264) |
| `WEBMC_STREAM_PASSWORD` | *(none)* | set it. Without it the URL is open to anyone who finds it |
| `WEBMC_WIDTH` / `WEBMC_HEIGHT` | `1280` / `720` | the virtual screen; resolution is cheap when the host is CPU-bound |
| `WEBMC_STREAM_FPS` | `30` | frame cap |
| `WEBMC_VIDEO_BITRATE` | `4000` | kbps cap for the encoder |
| `WEBMC_VIDEO_CRF` | `28` | higher is a cheaper picture |

## Worth knowing

* The tunnel address is **temporary and public**. It changes every run, and it is the only
  thing between the internet and the session — hence the password.
* The picture travels to the viewer, so the viewer's click-to-change is the round trip to
  the host, not to their own machine. If the viewer is the same person as the host, the
  launcher page is strictly better: same machine, no encoder, no tunnel, no latency.
* One session per host is realistic on CPU-only machines; the runner measurements in the
  project's notes put x264 at ~18× realtime for 480p, so encoding is not the bottleneck —
  Minecraft's own renderer is.
