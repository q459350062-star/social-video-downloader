#!/bin/sh
set -eu

: "${COBALT_API_KEY:?COBALT_API_KEY must be set}"
: "${API_URL:?API_URL must be set to the public Render URL}"

# Keep Node on IPv4 first; this Render region has no working IPv6 route to YouTube.
export NODE_OPTIONS="${NODE_OPTIONS:-} --dns-result-order=ipv4first"

# Build the private API-key database at runtime; the key is never committed to Git.
printf '{"%s":{"name":"social-video-downloader","limit":20,"allowedServices":"all"}}\n' "$COBALT_API_KEY" > /tmp/keys.json
export API_KEY_URL="file:///tmp/keys.json"
export API_AUTH_REQUIRED=1
export API_LISTEN_ADDRESS="0.0.0.0"
export API_PORT="${PORT:-9000}"
export YOUTUBE_ALLOW_BETTER_AUDIO="0"
export PYTHONUNBUFFERED=1

# YouTube blocks this Render datacenter egress with LOGIN_REQUIRED. Only run the
# browser token generator when a trusted residential/mobile proxy is configured.
PROXY_URL="${RESIDENTIAL_PROXY_URL:-}"
if [ -n "$PROXY_URL" ]; then
  case "$PROXY_URL" in
    http://*|https://*) ;;
    *) PROXY_URL="http://$PROXY_URL" ;;
  esac
  export RESIDENTIAL_PROXY_URL="$PROXY_URL"
  export HTTP_PROXY="$PROXY_URL"
  export HTTPS_PROXY="$PROXY_URL"
  export http_proxy="$PROXY_URL"
  export https_proxy="$PROXY_URL"
  export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1}"
  export no_proxy="$NO_PROXY"
  export YT_SESSION_PROXY="http://127.0.0.1:3128"

  # Chrome cannot authenticate to an upstream proxy directly; bridge it locally.
  node /app/proxy-bridge.mjs &
  PROXY_PID=$!
  sleep 1
  if kill -0 "$PROXY_PID" 2>/dev/null; then
    export YOUTUBE_SESSION_SERVER="http://127.0.0.1:8080/get_pot"
    export YOUTUBE_SESSION_INNERTUBE_CLIENT="WEB_EMBEDDED"

    Xvfb :99 -ac -screen 0 1280x720x16 -nolisten tcp &
    XVFB_PID=$!
    export DISPLAY=:99
    XVFB_READY=0
    for attempt in $(seq 1 20); do
      if [ -S /tmp/.X11-unix/X99 ]; then XVFB_READY=1; break; fi
      if ! kill -0 "$XVFB_PID" 2>/dev/null; then break; fi
      sleep 0.25
    done
    echo "[yt-session] xvfb_ready=$XVFB_READY"

    # Refresh tokens slowly to avoid repeatedly hitting YouTube's bot checks.
    (
      while true; do
        if /opt/yt-session-venv/bin/python /app/yt-session-generator/potoken-generator.py --port 8080 --bind 127.0.0.1 --update-interval 300; then
          echo "[yt-session] generator exited normally; restarting in 10 seconds"
        else
          exit_code=$?
          echo "[yt-session] generator exited code=$exit_code; restarting in 10 seconds"
        fi
        pkill -x chromium 2>/dev/null || true
        pkill -x chromium-browser 2>/dev/null || true
        sleep 10
      done
    ) &
    SESSION_PID=$!

    TOKEN_READY=0
    for attempt in $(seq 1 12); do
      if /opt/yt-session-venv/bin/python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8080/token", timeout=2)); assert len(d.get("potoken", "")) >= 160 and d.get("visitor_data")' >/dev/null 2>&1; then
        TOKEN_READY=1
        break
      fi
      sleep 3
    done
    echo "[yt-session] initial_token_ready=$TOKEN_READY"
  else
    echo "[proxy-bridge] failed to start; disabling YouTube session generator"
    unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy YT_SESSION_PROXY
    unset YOUTUBE_SESSION_SERVER YOUTUBE_SESSION_INNERTUBE_CLIENT
  fi
else
  unset YOUTUBE_SESSION_SERVER YOUTUBE_SESSION_INNERTUBE_CLIENT
  echo "[yt-session] no RESIDENTIAL_PROXY_URL configured; browser generator disabled to avoid repeated bot-wall requests"
fi

# Run the public API as the unprivileged node user.
exec su-exec node node /app/src/cobalt
