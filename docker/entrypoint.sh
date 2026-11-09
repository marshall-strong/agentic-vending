#!/bin/sh
# Join the class tailnet, then run the command.
#
# The bridge (aihub-pi:8000) is reachable only on the tailnet. A Container App
# has no TUN device, so tailscaled runs in userspace mode and offers an HTTP
# proxy on localhost:1055 that resolves MagicDNS names and routes tailnet
# traffic. Only plain-http traffic is sent through it (http_proxy, not
# https_proxy), so the bridge goes over Tailscale while the Anthropic API and
# Postgres go straight out.
#
# Without TS_AUTHKEY the command still runs, for local tries; the bridge is
# then unreachable and the syncs report that.
set -eu

if [ -n "${TS_AUTHKEY:-}" ]; then
    tailscaled --tun=userspace-networking \
        --socks5-server=localhost:1055 \
        --outbound-http-proxy-listen=localhost:1055 \
        --state="${TS_STATE_DIR:-/var/lib/tailscale}/tailscaled.state" \
        >/var/log/tailscaled.log 2>&1 &
    # Up to 30 s for the daemon's socket, then join. The key is pre-authorized
    # and tagged, so no approval step.
    i=0
    until tailscale up --authkey="$TS_AUTHKEY" --hostname="${TS_HOSTNAME:-agentic-vending}" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 15 ]; then
            echo "tailscale up failed after 30 s; see /var/log/tailscaled.log" >&2
            cat /var/log/tailscaled.log >&2 || true
            exit 1
        fi
        sleep 2
    done
    echo "tailscale: $(tailscale status --peers=false 2>/dev/null | head -1)"
    export http_proxy="http://localhost:1055"
    export no_proxy="${no_proxy:-localhost,127.0.0.1}"
else
    echo "TS_AUTHKEY is not set: running without Tailscale, so the bridge is unreachable" >&2
fi

exec "$@"
