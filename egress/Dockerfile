# A forward proxy that only lets through hosts you have named.
#
# Built from source packages rather than pulled from a third-party proxy image
# on purpose: this container is the single door out of the agent's network, so
# what is in it should be auditable in five lines.
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY allowlist.txt  /etc/tinyproxy/allowlist.txt

EXPOSE 8888
# Foreground: the container IS the proxy, so its lifetime should be the
# proxy's, not a daemon's that can die while the container stays "up".
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
