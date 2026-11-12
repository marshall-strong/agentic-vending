# The agent's container: the worker that keeps our database in step with the
# bridge (history, request and catalog syncs on a timer) and answers /health,
# with Tailscale in userspace mode so the bridge is reachable from Azure.
#
#   docker build -t ghcr.io/marshall-strong/agentic-vending:dev .
#   docker run --rm -p 8080:8080 -e DATABASE_URL=... -e DEX_BASE_URL=... -e DEX_API_KEY=... ghcr.io/marshall-strong/agentic-vending:dev
#
# Not `--env-file .env`: Docker keeps quotes and ` # comments` in the values,
# which the app's own `.env` parser strips.
#
# The MCP server itself speaks stdio, so it is started by whatever drives the
# agent, not by this image's default command. `docker run ... python -m
# vending.server` runs it in the same image when that harness exists.
# See docs/deploy.md.

# Both base images are pinned by tag and digest, so a rebuild of the same
# commit gets the same Python and Tailscale. Bump them deliberately: look up a
# digest with `docker buildx imagetools inspect <image>:<tag>`.
#
# Python is the newest version any of us develops on (3.14), so code that runs
# on a laptop runs here too: a 3.13+ API that passed locally would otherwise
# crash the hourly job in Azure. Keep it in step with the team's venvs.
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

# Tailscale's own image carries static binaries. Userspace networking needs no
# TUN device, iptables or root.
COPY --from=tailscale/tailscale:v1.104.4@sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c \
    /usr/local/bin/tailscaled /usr/local/bin/tailscale /usr/local/bin/

# tini runs as PID 1: it passes Azure's SIGTERM on to the worker and reaps the
# entrypoint's background loops when they end, which Python as PID 1 never does.
# Container Apps has no `docker run --init`, so it is in the image.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Runtime packages only; pytest is in requirements-dev.txt.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vending/ vending/
COPY db/ db/
# get_project_rules reads these notes at run time (vending/tools/project_rules.py).
COPY docs/agent/ docs/agent/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd --system --uid 10001 --no-create-home vending

# Not root. Tailscale keeps its state in memory and its socket in /tmp, and the
# health check listens above 1024, so nothing needs privileges.
USER vending
ENV PYTHONUNBUFFERED=1 \
    PORT=8080
EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "vending.worker"]
