#!/usr/bin/env bash
#
# Turns a machine you own into a streamed desktop:
#   Xvfb virtual screen  ->  PulseAudio  ->  Selkies
#   ->  temporary Cloudflare tunnel  ->  an https URL a browser can open.
#
# Run this on your own machine or a VM you pay for, never on a CI service: using
# GitHub-hosted runners as a game host is the activity GitHub's Additional Product Terms
# rule out ("any other activity unrelated to the production, testing, deployment, or
# publication of the software project"), and it is what got an earlier account of this
# project suspended. That is also why this repository contains no workflow that runs it.
#
# The URL is printed at the end (and published into docs/session.json when there is one,
# which the launcher page can read without anybody needing a token). Set
# WEBMC_STREAM_PASSWORD - the link is otherwise open to whoever finds it.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DISPLAY_NUM="${WEBMC_DISPLAY:-:99}"
WIDTH="${WEBMC_WIDTH:-1280}"
HEIGHT="${WEBMC_HEIGHT:-720}"
PASSWORD="${WEBMC_STREAM_PASSWORD:-}"
SELKIES_USER="webmc"

# --- which frontend carries the picture -------------------------------------
# selkies (default) - WebSocket video with audio, cheapest on a thin line.
# novnc            - x11vnc + noVNC over the same port and tunnel: no video codec at
#                    all, so there is nothing to negotiate and nothing to fail. No audio.
# The chosen one is tried first; if it does not answer, the other one starts instead.
FRONTEND="${WEBMC_STREAM_CLIENT:-selkies}"
WEB_PORT="${WEBMC_WEB_PORT:-8080}"
VNC_PORT="${WEBMC_VNC_PORT:-5900}"
NOVNC_QUALITY="${WEBMC_NOVNC_QUALITY:-6}"
NOVNC_COMPRESSION="${WEBMC_NOVNC_COMPRESSION:-1}"
NOVNC_WEB="${WEBMC_NOVNC_WEB:-/usr/share/novnc}"
[ -f "$NOVNC_WEB/vnc.html" ] || NOVNC_WEB=/opt/novnc
[ -f "$NOVNC_WEB/vnc.html" ] || NOVNC_WEB=""

# --- how the picture is encoded ---------------------------------------------
# The encoder is a LIST: Selkies offers the client these in order and the client walks
# the list until it finds a codec it can really decode, so a browser without WebCodecs
# H.264 lands on JPEG instead of sitting on "waiting for stream" - which is exactly what
# the "sometimes the game never shows up" report was.
STREAM_FPS="${WEBMC_STREAM_FPS:-30}"
BITRATE="${WEBMC_VIDEO_BITRATE:-4000}"
CRF="${WEBMC_VIDEO_CRF:-28}"
# Frames of video allowed to queue before dropping. Selkies' own default is 120 frames,
# i.e. ~4 seconds of delay at 30fps: the queue is what "feels like 1000ms of input lag".
QUEUE="${WEBMC_BACKPRESSURE_QUEUE:-4}"
ENCODER="${WEBMC_STREAM_ENCODER:-h264enc-striped,jpeg}"
JPEG_Q="${WEBMC_STREAM_JPEG_QUALITY:-35}"
case ",$ENCODER," in
  *,jpeg,*) ;;
  *) ENCODER="$ENCODER,jpeg" ;;   # every browser can show JPEG; never leave no fallback
esac
export DISPLAY="$DISPLAY_NUM"

say() { echo "[stream] $*"; }
publish() { python3 "$ROOT/tools/session.py" publish --state "$ROOT/docs" "$@" || true; }

# Say why on the site as well as in the log, then stop.
die() {
  local message="$1" logfile="${2:-}"
  local detail=""
  if [ -n "$logfile" ] && [ -f "$logfile" ]; then
    detail="$(grep -aE 'ERROR|Traceback|error:' "$logfile" | tail -n 1 | cut -c1-180 || true)"
  fi
  say "FAILED: $message${detail:+ - $detail}"
  publish --status error --message "$message${detail:+ (${detail})}" --progress 0 --commit
  exit 1
}

# --------------------------------------------------------------------------- #
# 1. virtual screen
# --------------------------------------------------------------------------- #
say "starting Xvfb at ${WIDTH}x${HEIGHT} on $DISPLAY"
RUNNER_TRACKING_ID="" setsid Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -ac \
  +extension GLX +extension RANDR -nolisten tcp >/tmp/xvfb.log 2>&1 &
for _ in $(seq 1 30); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  die "the virtual screen would not start" /tmp/xvfb.log
fi
xset -display "$DISPLAY" m 0 0 || true
say "screen ready: $(xdpyinfo -display "$DISPLAY" | awk '/dimensions/ {print $2}')"

# No window manager on purpose. dwm would draw a status bar over the top of the
# screen and tile the game window under it, which shifts everything the user
# clicks on. One fullscreen window needs no WM: Minecraft gets the whole
# 854x480 screen and X gives it the keyboard focus.
say "window manager: none (Minecraft owns the screen)"

# --------------------------------------------------------------------------- #
# 2. audio (Selkies streams PulseAudio)
# --------------------------------------------------------------------------- #
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/webmc-runtime}"
export PULSE_RUNTIME_PATH="$XDG_RUNTIME_DIR/pulse"
mkdir -p "$XDG_RUNTIME_DIR" "$PULSE_RUNTIME_PATH"
chmod 700 "$XDG_RUNTIME_DIR"
pulseaudio --kill >/dev/null 2>&1 || true
rm -f "$PULSE_RUNTIME_PATH/native"
(
  unset PULSE_SERVER
  pulseaudio --start --daemonize=yes --system=false --exit-idle-time=-1 \
    --log-target=file:/tmp/pulseaudio.log
) || say "! pulseaudio failed to start, the stream will be silent"
export PULSE_SERVER="unix:$PULSE_RUNTIME_PATH/native"
for _ in $(seq 1 20); do
  [ -S "$PULSE_RUNTIME_PATH/native" ] && break
  sleep 1
done
if [ -S "$PULSE_RUNTIME_PATH/native" ]; then
  pactl load-module module-virtual-sink sink_name=webmc sink_properties=device.description=WebMC >/dev/null 2>&1 || true
  pactl set-default-sink webmc >/dev/null 2>&1 || true
  pactl set-default-source webmc.monitor >/dev/null 2>&1 || true
  pactl set-sink-volume webmc 100% >/dev/null 2>&1 || true
  say "audio ready"
else
  say "! no pulse socket, the stream will be silent"
fi
{
  echo "DISPLAY=$DISPLAY"
  echo "WEBMC_XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
  echo "PULSE_SERVER=$PULSE_SERVER"
  # keep the game's audio on pulse, and stop llvmpipe from waiting on vsync
  echo "ALSOFT_DRIVERS=pulse"
  echo "SDL_AUDIODRIVER=pulseaudio"
  echo "PULSE_LATENCY_MSEC=60"
  echo "vblank_mode=0"
} >> "${GITHUB_ENV:-/dev/null}"

# --------------------------------------------------------------------------- #
# 3. the frontend: how the picture leaves this machine
# --------------------------------------------------------------------------- #
#
# Both frontends listen on the same port and are reached through the same tunnel, so
# switching between them (or falling back) changes nothing the player has to know about.
#
# Selkies: the encoder above is a LIST, and that is the single most important line in
# this file. Selkies offers the client those encoders in order and the client steps
# through them until it finds a codec it can really decode, so a browser without
# WebCodecs H.264 lands on JPEG. With one encoder name it instead sat on "waiting for
# stream" forever - that was the "sometimes the game never shows up" report.
#
# The display, in the order it went wrong:
#   * --manual-resolution with the configured width/height keeps the game at 854x480. On a
#     4-core CPU-only host that is the difference between smooth and a slideshow, and the
#     alternative - letting the client resize the desktop to a 2K browser window - makes the
#     game render four times the pixels.
#   * --use-css-scaling=true is what fills the window anyway: the client stretches the
#     854x480 picture across the canvas with CSS, so the picture is full screen while the
#     encoder and the game stay cheap. Without it the client drew the framebuffer 1:1 and
#     the game sat in a corner of the page (the "display is 10% of the screen" report).
#   * --video-on-start=true so a session starts with the stream on instead of waiting for
#     somebody to find the toggle in the side menu.
#   * --enable-cursors=false --use-browser-cursors=false: two cursors - the client's CSS one
#     and the remote X one - are what made the camera spin. The game draws its own crosshair
#     and grabs the pointer; passing a cursor through as well is pure interference.
#   * --raw-pointer-motion stays ON: it asks the browser for unaccelerated movement under
#     pointer lock, which is how Minecraft wants to be driven.
#
# Selkies 2.x refuses to start when basic auth is enabled without a password, so an
# empty password is passed explicitly as --enable-basic-auth=false.
start_selkies() {
  local password="$1"
  local auth=(--enable-basic-auth=false)
  if [ -n "$password" ]; then
    auth=(--basic-auth-user="$SELKIES_USER" --basic-auth-password="$password")
  fi
  RUNNER_TRACKING_ID="" setsid selkies \
    --public --port="$WEB_PORT" --mode=websockets --enable-https=false \
    "${auth[@]}" \
    --encoder="$ENCODER" --jpeg-quality="$JPEG_Q" --raw-pointer-motion=true \
    --framerate="$STREAM_FPS" --video-bitrate="$BITRATE" --video-crf="$CRF" \
    --backpressure-queue-size="$QUEUE" \
    --manual-resolution=true --manual-width="$WIDTH" --manual-height="$HEIGHT" \
    --use-css-scaling=true --video-on-start=true --audio-on-start=true \
    --enable-cursors=false --use-browser-cursors=false \
    >/tmp/selkies.log 2>&1 &
  echo $! > /tmp/selkies.pid
}

wait_for_selkies() {
  local creds="$1" code
  local auth=()
  [ -n "$creds" ] && auth=(-u "$creds")
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "${auth[@]}" --max-time 5 "http://127.0.0.1:$WEB_PORT/" || true)"
    case "$code" in
      200|401|403) return 0 ;;
    esac
    if ! kill -0 "$(cat /tmp/selkies.pid 2>/dev/null)" 2>/dev/null; then
      return 1
    fi
    sleep 1
  done
  return 1
}

# x11vnc carries the picture to noVNC through websockify. It captures the *existing*
# display, so the GLX/llvmpipe path Minecraft already runs on stays untouched - and it
# has no video codec at all, so there is nothing to negotiate and nothing to fail.
start_novnc() {
  local password="$1"
  local auth=()
  [ -n "$password" ] && auth=(-passwd "$password")
  # -xdamage: send only the regions X says changed, instead of comparing the whole
  # 854x480 buffer every few milliseconds - on a 4-core box that CPU belongs to the game.
  # -threads: decode/encode on its own thread so polling never waits for a client.
  # -localhost: the VNC port itself is never exposed; only websockify is, and that is what
  # the tunnel points at.
  RUNNER_TRACKING_ID="" setsid x11vnc \
    -display "$DISPLAY" -rfbport "$VNC_PORT" -localhost -forever -shared -repeat \
    -xdamage -wait 2 -defer 2 -threads -quiet ${auth[@]+"${auth[@]}"} \
    >/tmp/x11vnc.log 2>&1 &
  echo $! > /tmp/x11vnc.pid
  local web=()
  [ -n "$NOVNC_WEB" ] && web=(--web="$NOVNC_WEB")
  RUNNER_TRACKING_ID="" setsid websockify ${web[@]+"${web[@]}"} "$WEB_PORT" "127.0.0.1:$VNC_PORT" \
    >/tmp/websockify.log 2>&1 &
  echo $! > /tmp/websockify.pid
}

compose_play_url() {
  local base="$1" query
  case "$FRONTEND" in
    novnc)
      # straight into the game: connect, scale the 854x480 desktop to the window, and
      # reconnect by itself if the tunnel drops. quality/compression are noVNC's own
      # 0-9 dials, adjustable live in its settings panel as well.
      query="autoconnect=1&resize=scale&reconnect=1&reconnect_delay=2000&path=websockify"
      query="$query&quality=$NOVNC_QUALITY&compression=$NOVNC_COMPRESSION"
      if [ -n "$PASSWORD" ]; then
        query="$query&password=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$PASSWORD")"
      fi
      printf '%s/vnc.html?%s' "$base" "$query"
      ;;
    *)
      printf '%s' "$base"
      ;;
  esac
}

wait_for_novnc() {
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:$WEB_PORT/vnc.html"; then
      return 0
    fi
    if ! kill -0 "$(cat /tmp/websockify.pid 2>/dev/null)" 2>/dev/null; then
      return 1
    fi
    sleep 1
  done
  return 1
}

try_start_selkies() {
  local password="$1" creds=""
  kill "$(cat /tmp/selkies.pid 2>/dev/null)" 2>/dev/null || true
  sleep 1
  start_selkies "$password"
  [ -n "$password" ] && creds="$SELKIES_USER:$password"
  wait_for_selkies "$creds"
}

selkies_up() {
  local password="${1:-}"
  say "starting Selkies (${STREAM_FPS} fps, ${BITRATE} kbps, CRF ${CRF}, queue ${QUEUE}, encoders ${ENCODER})"
  if try_start_selkies "$password"; then
    return 0
  fi
  tail -n 15 /tmp/selkies.log || true
  if [ -n "$password" ]; then
    say "! Selkies would not start with a login, retrying without one"
    PASSWORD=""
  else
    PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
    say "! Selkies would not start without a login, retrying with a generated password"
  fi
  try_start_selkies "$PASSWORD"
}

novnc_up() {
  if [ -z "$NOVNC_WEB" ]; then
    say "! noVNC is not installed here (looked in /usr/share/novnc and /opt/novnc)"
    return 1
  fi
  say "starting noVNC (x11vnc + websockify on :$WEB_PORT, quality ${NOVNC_QUALITY})"
  start_novnc "$PASSWORD"
  if wait_for_novnc; then
    return 0
  fi
  tail -n 10 /tmp/x11vnc.log 2>/dev/null || true
  tail -n 10 /tmp/websockify.log 2>/dev/null || true
  return 1
}

# The chosen frontend first, then the other one: whichever answers carries the session,
# and which one it was is published, so "no picture" is never a mystery again.
start_frontend() {
  if [ "$FRONTEND" = "novnc" ]; then
    if novnc_up; then return 0; fi
    say "! noVNC did not come up, falling back to Selkies"
    FRONTEND=selkies
    if selkies_up "$PASSWORD"; then return 0; fi
  else
    if selkies_up "$PASSWORD"; then return 0; fi
    say "! Selkies did not come up, falling back to noVNC"
    FRONTEND=novnc
    if novnc_up; then return 0; fi
  fi
  return 1
}

if ! start_frontend; then
  say "FAILED: neither frontend would start"
  die "the streaming server would not start (tried both Selkies and noVNC)" /tmp/selkies.log
fi
say "frontend up: $FRONTEND on 127.0.0.1:$WEB_PORT (login: ${PASSWORD:+set}${PASSWORD:-none})"

# --------------------------------------------------------------------------- #
# 4. public tunnel
# --------------------------------------------------------------------------- #
say "opening a Cloudflare tunnel"
RUNNER_TRACKING_ID="" setsid cloudflared tunnel --no-autoupdate \
  --url "http://127.0.0.1:$WEB_PORT" >/tmp/cloudflared.log 2>&1 &
TUNNEL_PID=$!
URL=""
for _ in $(seq 1 90); do
  URL="$(grep -aEo 'https://[-a-z0-9]+\.trycloudflare\.com' /tmp/cloudflared.log | head -n 1 || true)"
  [ -n "$URL" ] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    die "the tunnel program (cloudflared) exited" /tmp/cloudflared.log
  fi
  sleep 1
done
[ -n "$URL" ] || die "no public tunnel address appeared within 90s" /tmp/cloudflared.log
say "tunnel: $URL"

# --------------------------------------------------------------------------- #
# 5. tell the world
# --------------------------------------------------------------------------- #
# The play link is frontend specific: noVNC lives at /vnc.html and takes its quality
# and compression in the query string, so the published URL opens straight into the
# game instead of a connection dialog.
PLAY_URL="$(compose_play_url "$URL")"

publish --status streaming --message "Stream is live. Starting Minecraft..." --progress 70 \
  --url "$PLAY_URL" --user "$SELKIES_USER" --password "$PASSWORD" \
  --set "client=$FRONTEND" --set "encoder=$ENCODER" --commit
echo "WEBMC_URL=$PLAY_URL" >> "${GITHUB_ENV:-/dev/null}"
echo "$PLAY_URL" > /tmp/webmc-url.txt
say "play link: $PLAY_URL"

# Live window into the streaming server, published with the session state: the
# clients it thinks are connected, and its own complaints. Without this we are
# guessing about "waiting for stream".
(
  last=""
  while true; do
    sleep 20
    if [ "$FRONTEND" = "selkies" ]; then
      clients="$(curl -s --max-time 5 "http://127.0.0.1:$WEB_PORT/api/sessions" 2>/dev/null | tr -d '\n' | cut -c1-200)"
      problems="$(grep -aE "ERROR|WARNING|Traceback|disabled|stopped|No display" /tmp/selkies.log 2>/dev/null | tail -3 | tr '\n' ';' | cut -c1-400)"
      summary="selkies clients=${clients:-?} | ${problems:-clean}"
    else
      problems="$(grep -aiE "error|fatal|cannot" /tmp/x11vnc.log /tmp/websockify.log 2>/dev/null | tail -2 | tr '\n' ';' | cut -c1-300)"
      summary="novnc x11vnc+websockify | ${problems:-clean}"
    fi
    if [ "$summary" != "$last" ]; then
      last="$summary"
      python3 "$ROOT/tools/session.py" publish --state "$ROOT/docs" --set "stream_diag=$summary" --commit || true
    fi
  done
) &

say "done - waiting for Minecraft"
