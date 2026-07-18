#!/bin/sh
# Generate a self-signed cert on first run when TLS is requested, then start
# the server. The cert lives inside the container only (nothing on the host).
set -e
if [ "$WEBUI_TLS" = "1" ] && [ ! -f /app/certs/server.crt ]; then
  mkdir -p /app/certs
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -subj "/CN=hermes-webui" \
    -keyout /app/certs/server.key -out /app/certs/server.crt >/dev/null 2>&1
  echo "generated self-signed TLS cert (CN=hermes-webui)"
fi
exec python server.py
