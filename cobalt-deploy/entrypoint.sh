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

exec node /app/src/cobalt
