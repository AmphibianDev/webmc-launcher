# WebMC stream host — Selkies plus this project's own launcher, in one image.
#
# What this is. The streaming half of this project, packaged once so that any
# machine can serve it with a single `docker run`. Selkies
# (https://github.com/selkies-project/selkies) is the part that turns a Linux
# X11 screen into something a browser can watch: it captures the display,
# encodes it, and serves the player, the audio and the input channel on one
# port. The desktop around it comes from Selkies' own reference image.
#
# What this is not, and why the file next to it matters. Nothing here starts on
# its own. A session begins only when somebody runs this container on hardware
# they own or rent. Building an image is "production ... of the software
# project"; running a game session on a CI runner for other people to play is
# not, under GitHub's Additional Product Terms — which is what
# `.github/workflows/publish-stream-image.yml` builds and pushes this for, and
# all it does. See STREAMING.md for the whole argument.
#
# The game is not in this image, for the same reasons it is not in the
# repository. tools/play-local.py fetches the JDK (Adoptium), the client
# (Mojang) and the loader (Fabric) on first run into a volume you mount. No
# client jar, no mod jars, nothing redistributed.

FROM ghcr.io/selkies-project/selkies/desktop:main-ubuntu26.04

LABEL org.opencontainers.image.title="WebMC stream host"
LABEL org.opencontainers.image.description="A Selkies session that runs the real Minecraft: Java Edition client on a machine you own. The game and the JDK are fetched at first run, not carried here."
LABEL org.opencontainers.image.source="https://github.com/AmphibianDev/webmc-launcher"
LABEL org.opencontainers.image.licenses="MPL-2.0"

USER 0

# The base already carries Xvfb, Mesa (so an llvmpipe software GL, no GPU
# required), the audio stack and Selkies itself. What it does not carry is what
# the launcher needs: an interpreter with pip to install its library, and
# xdotool, which tools/launch.py uses to keep the game window the size of the
# desktop — the desktop resizes to the browser window, and without xdotool the
# game stays at its launch resolution in a corner of it.
RUN apt-get update && apt-get install --no-install-recommends -y \
        python3 \
        python3-venv \
        python3-pip \
        xdotool \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/debconf/* /var/log/* /tmp/* /var/tmp/*

# play-local.py imports minecraft_launcher_lib, and runs launch.py with
# sys.executable, so both want one interpreter that has it. This image's
# python3 is new enough for the library, which is why there is no first-run venv
# dance here the way tools/local-ui.py needs on a Mac whose python3 is 3.9.
RUN python3 -m venv /opt/webmc/venv \
    && /opt/webmc/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/webmc/venv/bin/pip install --no-cache-dir "minecraft-launcher-lib>=8.0" \
    && rm -rf /root/.cache/pip

# This project's own code, copied as it is. tools/ needs to sit beside mods/ and
# docs/, because play-local.py resolves both as ROOT/"mods" and ROOT/"docs"
# relative to its own directory: /opt/webmc/tools/play-local.py makes ROOT
# /opt/webmc. docs/ is deliberately absent — read_config() treats a missing
# config.json as "no defaults", and WEBMC_* on the container is the clearer
# place for them.
COPY --chown=1000:1000 tools/ /opt/webmc/tools/
COPY --chown=1000:1000 mods/ /opt/webmc/mods/
COPY --chown=1000:1000 stream/webmc-play.sh /opt/webmc/webmc-play.sh
RUN chmod 0755 /opt/webmc/webmc-play.sh

# The session starts the game from the system autostart directory rather than
# from a home directory: /etc/xdg/autostart is read by lxqt-session for every
# user (the XDG autostart spec puts it there for exactly this), so this does not
# depend on guessing which home the base image gives its session user.
COPY stream/webmc-play.desktop /etc/xdg/autostart/webmc-play.desktop
RUN chmod 0644 /etc/xdg/autostart/webmc-play.desktop

# The stream settings tools/stream.sh arrived at, spelled as environment
# variables. Selkies resolves SELKIES_<NAME> for every setting it has a flag
# for, so each of these is the flag of the same name seen there:
#
#   browser->server is one websocket on one port; no TURN server, no UDP holes
#   TLS is off because the thing in front of this terminates it (a tunnel or a
#   reverse proxy) — put it back to true if you will expose :8080 directly
#   1280x720 at 30fps and a 4000kbps cap is the shape a 4-core CPU-only host can
#   encode while it also renders; resolution is the cheapest dial to turn down
#   css_scaling is what fills the window: the client stretches the picture, so
#   the encoder and the game stay at 720p while the browser gets full screen
#   both cursor switches off, because the game draws its own crosshair and
#   grabs the pointer; a streamed cursor on top of it is interference (this is
#   what made the camera spin), while raw_pointer_motion stays on, because that
#   is what asks the browser for unaccelerated movement under pointer lock
ENV SELKIES_MODE="websockets" \
    SELKIES_ENABLE_HTTPS="false" \
    SELKIES_ENCODER="h264enc" \
    SELKIES_FRAMERATE="30" \
    SELKIES_VIDEO_BITRATE="4000" \
    SELKIES_VIDEO_CRF="28" \
    SELKIES_USE_CSS_SCALING="true" \
    SELKIES_VIDEO_ON_START="true" \
    SELKIES_AUDIO_ON_START="true" \
    SELKIES_ENABLE_CURSORS="false" \
    SELKIES_USE_BROWSER_CURSORS="false" \
    SELKIES_RAW_POINTER_MOTION="true" \
    SELKIES_MANUAL_RESOLUTION="true" \
    SELKIES_MANUAL_WIDTH="1280" \
    SELKIES_MANUAL_HEIGHT="720"

# Where the game goes. Mount something at this path or the download happens
# again every time the container is replaced.
ENV WEBMC_MC_DIR="/home/ubuntu/.webmc/minecraft"

# No password is baked in, deliberately: an image with a password in it is an
# image with the same password in everyone's session. The base image warns at
# startup while its published placeholder login is still in use. stream/run.sh
# passes SELKIES_BASIC_AUTH_USER and SELKIES_BASIC_AUTH_PASSWORD at `docker run`
# time; set the password before anything other than this machine can reach the
# port.
#
# Neither is set here with ENV, and not only for tidiness: Docker's build linter
# flags any ENV whose name mentions auth, because the moment somebody turns one
# of these into an actual secret it would be readable in `docker history`
# forever. Keeping credentials out of the image means the image has none to leak.

# Back to the session user the base image ships. The `USER 0` above is only for
# installing packages; the container has to run, and the session has to log in,
# as uid 1000 with HOME=/home/ubuntu — that is the home the mounted volume lands
# on, and the uid the base's s6 services and its Xvfb expect to hand the display
# to. Leaving it at 0 would put the game in /root and the volume nowhere.
USER 1000
