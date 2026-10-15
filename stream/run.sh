#!/usr/bin/env bash
#
# Run the WebMC stream on the machine you are on.
#
#   bash stream/run.sh --password pick-something    # http://127.0.0.1:8080
#   bash stream/run.sh --password ... --public      # on every interface
#   WEBMC_STREAM_PASSWORD=... bash stream/run.sh --tunnel
#
# This is the only way this project's streaming half is meant to run: on a
# machine you own or rent. It is deliberately not a workflow — see STREAMING.md
# for what a session on a CI runner costs, and why nothing in this repository
# does it.
#
# The port is bound to 127.0.0.1 unless you say --public. A streaming server
# reachable by whoever finds it, with a password somebody chose in a hurry, is
# how a hobby project becomes somebody else's machine; the default should be the
# one that is quiet.
#
set -uo pipefail

IMAGE="${WEBMC_IMAGE:-ghcr.io/amphibbiandev/webmc-launcher-stream:latest}"
NAME="${WEBMC_NAME:-webmc-stream}"
PORT="${WEBMC_STREAM_PORT:-8080}"

password="${WEBMC_STREAM_PASSWORD:-}"
public=0
tunnel=0
while [ $# -gt 0 ]; do
  case "$1" in
    --password) password="${2:-}"; shift 2 ;;
    --port)     PORT="${2:-}"; shift 2 ;;
    --image)    IMAGE="${2:-}"; shift 2 ;;
    --seconds)  seconds="${2:-}"; WEBMC_SECONDS="${2:-}"; shift 2 ;;
    --public)   public=1; shift ;;
    --tunnel)   tunnel=1; public=1; shift ;;
    -h|--help)  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

say() { printf '  %s\n' "$*"; }
fail() { printf '  %s\n' "$*" >&2; exit 1; }

# ---- what this machine can give the container --------------------------------------
command -v docker >/dev/null 2>&1 || fail "no docker on this machine.
  macOS:      install Docker Desktop (or colima) and start it
  Debian/Ubuntu: sudo apt-get install -y docker.io && sudo usermod -aG docker \$USER
  A VM you rent usually has it, or one apt-get away."

docker info >/dev/null 2>&1 || fail "docker is installed but not answering. On macOS that
means Docker Desktop is not running; on Linux it is usually 'sudo systemctl start docker'
or a session that needs to be re-logged-in after being added to the docker group."

# Hardware GL if the host offers a render node, software (llvmpipe, which is
# what this ran on a runner and is genuinely usable at 720p) if it does not.
gl=()
if [ -e /dev/dri ]; then
  gl=(--device /dev/dri)
  render="$(ls /dev/dri/renderD* 2>/dev/null | head -1 || true)"
  if [ -n "${render:-}" ]; then
    gid="$(stat -c %g "$render" 2>/dev/null || stat -f %g "$render" 2>/dev/null || true)"
    [ -n "${gid:-}" ] && gl+=(--group-add "$gid")
  fi
  say "GPU: /dev/dri passed in — hardware GL"
else
  say "GPU: none on this host — software GL (llvmpipe), which is what 720p/30 was tuned for"
fi

# ---- the password is not optional --------------------------------------------------
if [ -z "$password" ]; then
  fail "no password. Pass --password <something> or set WEBMC_STREAM_PASSWORD.
  The image publishes no login of its own on purpose: the tunnel address (or the
  port, with --public) is the only thing in front of the session."
fi

# ---- where the game lives -----------------------------------------------------------
game="${WEBMC_GAME_DIR:-$HOME/.webmc-stream}"
mkdir -p "$game" || fail "cannot create $game"
say "game folder: $game (the JDK, the client, the loader and the assets land here)"

# ---- run -----------------------------------------------------------------------------
if [ "$public" = 1 ]; then
  publish=(-p "${PORT}:8080")
  say "binding: 0.0.0.0:${PORT} — anyone who can reach this port can reach the login"
else
  publish=(-p "127.0.0.1:${PORT}:8080")
  say "binding: 127.0.0.1:${PORT} — this machine only (--public to open it)"
fi

env_args=(
  -e "SELKIES_BASIC_AUTH_PASSWORD=${password}"
  -e "WEBMC_MC_DIR=/home/ubuntu/.webmc/minecraft"
)
# Pass the session dials through to the container unchanged, if they were set
# on this command line (or in the environment this was launched from).
for dial in WEBMC_VERSION WEBMC_LOADER WEBMC_USERNAME WEBMC_XMX WEBMC_JAVA_HOME WEBMC_SECONDS; do
  [ -n "${!dial:-}" ] || continue
  env_args+=(-e "${dial}=${!dial}")
done

docker rm -f "$NAME" >/dev/null 2>&1 || true

say "starting ${IMAGE}"
# ${arr[@]+...} rather than ${arr[@]}: macOS ships bash 3.2, where an empty
# array under `set -u` is an unbound variable — and "no /dev/dri" is the common
# case on a machine that is not a VM.
docker run -d --name "$NAME" --shm-size=2g \
  "${publish[@]}" ${gl[@]+"${gl[@]}"} "${env_args[@]}" \
  -v "${game}:/home/ubuntu/.webmc" \
  "$IMAGE" >/dev/null || fail "docker run failed — read the message above"

say ""
say "The session is up. Open http://localhost:${PORT} and log in as webmc."
say "It takes a minute on the first run: the JDK, then Minecraft, then the mods."
say ""
say "  logs:    docker logs -f ${NAME}          (Selkies and the session)"
say "  the game: docker exec -u ubuntu ${NAME} tail -n 40 /home/ubuntu/webmc-play.log"
say "  stop:    docker rm -f ${NAME}"

if [ "$tunnel" = 1 ]; then
  command -v cloudflared >/dev/null 2>&1 || fail "no cloudflared on this machine.
  Install it, then run:  cloudflared tunnel --url http://127.0.0.1:${PORT}
  (or skip the tunnel and reach this machine over your own network/VPN)"
  say ""
  say "tunnel: the address prints below and is temporary — it changes every run"
  exec cloudflared tunnel --url "http://127.0.0.1:${PORT}"
fi
