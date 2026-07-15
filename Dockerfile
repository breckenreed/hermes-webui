FROM python:3.12-slim

# The webui drives Hermes via `docker exec`, so it needs the Docker CLI.
# We install only the static client binary (no daemon) — it talks to the
# host's Docker Engine through the mounted socket.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && install -m0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static ./static

EXPOSE 8000
CMD ["python", "server.py"]
