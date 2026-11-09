# The agent's container: the worker that keeps our database in step with the
# bridge (history and request syncs on a timer) and answers /health, with
# Tailscale in userspace mode so the bridge is reachable from Azure.
#
#   docker build -t ghcr.io/marshall-strong/agentic-vending:dev .
#   docker run --rm -p 8080:80 --env-file .env -e DATABASE_URL=... ghcr.io/marshall-strong/agentic-vending:dev
#
# The MCP server itself speaks stdio, so it is started by whatever drives the
# agent, not by this image's default command. `docker run ... python -m
# vending.server` runs it in the same image when that harness exists.
# See docs/deploy.md.

FROM python:3.12-slim

# Tailscale's own image carries static binaries; copying them avoids pinning a
# version here. Userspace networking needs no TUN device or iptables.
COPY --from=tailscale/tailscale:stable /usr/local/bin/tailscaled /usr/local/bin/tailscale /usr/local/bin/

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vending/ vending/
COPY db/ db/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /var/lib/tailscale

ENV PYTHONUNBUFFERED=1 \
    PORT=80
EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "vending.worker"]
