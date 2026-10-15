#!/usr/bin/env bash
#
# What the streamed session runs: the real Minecraft client, on the machine this
# container is on.
#
# It is the Exec of an LXQt autostart entry (stream/webmc-play.desktop), so it
# starts inside the streamed desktop with that session's DISPLAY, audio and
# D-Bus already in place. Everything below the argument list is the same code
# the launcher page runs on a desktop: tools/play-local.py plans the session and
# tools/launch.py installs and starts it. Nothing about it is stream-specific —
# which is the point, because a stream that behaves differently from the local
# game is a stream you cannot debug.
#
# Dials, all of them environment variables on the container:
#
#   WEBMC_VERSION    Minecraft version (default: from the mods, else 1.21.5)
#   WEBMC_LOADER     fabric, quilt or vanilla (default: fabric)
#   WEBMC_USERNAME   the offline player name shown in game
#   WEBMC_XMX        heap in GB (default: 4)
#   WEBMC_JAVA_HOME  use a JDK already on this machine instead of downloading one
#   WEBMC_SECONDS    stop after N seconds (0 = play until the game is closed);
#                    this is the smoke test, not the way to run a session
#   WEBMC_MC_DIR     where the game lives (default: /home/ubuntu/.webmc/minecraft)
#
set -uo pipefail

PY=/opt/webmc/venv/bin/python
TOOLS=/opt/webmc/tools

if [ ! -x "$PY" ]; then
  echo "webmc-play: no interpreter at $PY — this is not the WebMC stream image" >&2
  exit 1
fi
if [ ! -f "$TOOLS/play-local.py" ]; then
  echo "webmc-play: no launcher at $TOOLS/play-local.py" >&2
  exit 1
fi

export HOME="${HOME:-/home/ubuntu}"
MC_DIR="${WEBMC_MC_DIR:-$HOME/.webmc/minecraft}"

# The session's own log, because an autostart entry's output has nowhere useful
# to go: this is the file to read when the game does not appear. It is inside
# the mounted volume's sibling, so a container replacement does not lose it.
mkdir -p "$HOME" 2>/dev/null || true
LOG="${WEBMC_LOG:-$HOME/webmc-play.log}"
exec > >(tee -a "$LOG") 2>&1

echo "webmc-play: $(date -Is) on $(uname -srm), display ${DISPLAY:-unset}"

args=(--mc-dir "$MC_DIR")
[ -n "${WEBMC_VERSION:-}" ]   && args+=(--version "$WEBMC_VERSION")
[ -n "${WEBMC_LOADER:-}" ]    && args+=(--loader "$WEBMC_LOADER")
[ -n "${WEBMC_USERNAME:-}" ]  && args+=(--username "$WEBMC_USERNAME")
[ -n "${WEBMC_XMX:-}" ]       && args+=(--xmx "$WEBMC_XMX")
[ -n "${WEBMC_JAVA_HOME:-}" ] && args+=(--java-home "$WEBMC_JAVA_HOME")
[ -n "${WEBMC_SECONDS:-}" ]   && args+=(--seconds "$WEBMC_SECONDS")

echo "webmc-play: $PY $TOOLS/play-local.py ${args[*]}"
"$PY" "$TOOLS/play-local.py" "${args[@]}"
status=$?
echo "webmc-play: the launcher exited with status $status at $(date -Is)"
exit "$status"
