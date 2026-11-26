#!/bin/sh
set -eu

: "${COBALT_API_KEY:?COBALT_API_KEY must be set}"
: "${API_URL:?API_URL must be set to the public Render URL}"

# Build the private API-key database at runtime; the key is never committed to Git.
printf '{"%s":{"name":"social-video-downloader","limit":20,"allowedServices":"all"}}\n' "$COBALT_API_KEY" > /tmp/keys.json
export API_KEY_URL="file:///tmp/keys.json"
export API_AUTH_REQUIRED=1
export API_LISTEN_ADDRESS="0.0.0.0"
export API_PORT="${PORT:-9000}"
# Generate YouTube's session-bound PO token from the same outbound IP as Cobalt.
# Cobalt 11.7.1 calls POST /get_pot; the bundled generator includes this compatibility route.
export YOUTUBE_SESSION_SERVER="http://127.0.0.1:8080/"
export YOUTUBE_SESSION_INNERTUBE_CLIENT="WEB_EMBEDDED"
export YOUTUBE_ALLOW_BETTER_AUDIO="0"
export PYTHONUNBUFFERED=1

# Compare YouTube reachability over IPv4 and IPv6 from the actual runtime container.
node -e '
const https = require("node:https");
async function probe(family) {
  await new Promise((resolve) => {
    const req = https.get({
      hostname: "www.youtube.com",
      path: "/generate_204",
      family,
      timeout: 8000,
      headers: { "User-Agent": "Mozilla/5.0" },
    }, (res) => {
      console.log("[netcheck] youtube_ipv" + family + "_http=" + res.statusCode);
      res.resume();
      resolve();
    });
    req.on("timeout", () => {
      console.log("[netcheck] youtube_ipv" + family + "_error=timeout");
      req.destroy();
      resolve();
    });
    req.on("error", (error) => {
      console.log("[netcheck] youtube_ipv" + family + "_error=" + (error.code || error.message));
      resolve();
    });
  });
}
Promise.all([probe(4), probe(6)]).then(() => process.exit(0));
'

# The generator requires a virtual display because it runs Chromium with a real browser context.
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
# Keep the generator supervised: nodriver/Chromium can fail transiently on startup.
# Restart every 3 seconds and remove orphaned browser processes between attempts.
(
  while true; do
    if /opt/yt-session-venv/bin/python /app/yt-session-generator/potoken-generator.py --port 8080 --bind 127.0.0.1 --update-interval 30; then
      echo "[yt-session] generator exited normally; restarting in 3 seconds"
    else
      exit_code=$?
      echo "[yt-session] generator exited code=$exit_code; restarting in 3 seconds"
    fi
    pkill -x chromium 2>/dev/null || true
    pkill -x chromium-browser 2>/dev/null || true
    sleep 3
  done
) &
SESSION_PID=$!

# Wait briefly for the first valid token; keep Cobalt available if YouTube blocks token creation.
TOKEN_READY=0
for attempt in $(seq 1 5); do
  if /opt/yt-session-venv/bin/python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8080/token", timeout=1)); assert len(d.get("potoken", "")) >= 160 and d.get("visitor_data")' >/dev/null 2>&1; then
    TOKEN_READY=1
    break
  fi
  sleep 1
done
echo "[yt-session] initial_token_ready=$TOKEN_READY"

# Run the public API as the unprivileged node user; Chromium stays under the
# root account as required by the upstream session-generator Docker instructions.
exec su-exec node node /app/src/cobalt
