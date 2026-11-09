# The agent's container: the worker that keeps our database in step with the
# bridge (history, request and catalog syncs on a timer) and answers /health,
# with Tailscale in userspace mode so the bridge is reachable from Azure.
#
#   docker build -t ghcr.io/marshall-strong/agentic-vending:dev .
#   docker run --rm -p 8080:8080 --env-file .env -e DATABASE_URL=... ghcr.io/marshall-strong/agentic-vending:dev
#
# The MCP server itself speaks stdio, so it is started by whatever drives the
# agent, not by this image's default command. `docker run ... python -m
# vending.server` runs it in the same image when that harness exists.
# See docs/deploy.md.

# Both base images are pinned by tag and digest, so a rebuild of the same
# commit gets the same Python and Tailscale. Bump them deliberately: look up a
# digest with `docker buildx imagetools inspect <image>:<tag>`.
FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# Tailscale's own image carries static binaries. Userspace networking needs no
# TUN device, iptables or root.
COPY --from=tailscale/tailscale:v1.104.4@sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c \
    /usr/local/bin/tailscaled /usr/local/bin/tailscale /usr/local/bin/

WORKDIR /app
# Runtime packages only; pytest is in requirements-dev.txt.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vending/ vending/
COPY db/ db/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd --system --uid 10001 --no-create-home vending

# Not root. Tailscale keeps its state in memory and its socket in /tmp, and the
# health check listens above 1024, so nothing needs privileges.
USER vending
ENV PYTHONUNBUFFERED=1 \
    PORT=8080
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "vending.worker"]
