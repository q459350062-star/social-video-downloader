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

exec node /app/src/cobalt
