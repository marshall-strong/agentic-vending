#!/bin/sh
# Join the class tailnet, then run the command.
#
# The bridge (aihub-pi:8000) is reachable only on the tailnet. A Container App
# has no TUN device, so tailscaled runs in userspace mode and offers an HTTP
# proxy on localhost:1055 that resolves MagicDNS names and routes tailnet
# traffic. Only plain-http traffic is sent through it (http_proxy, not
# https_proxy), so the bridge goes over Tailscale while the Anthropic API and
# Postgres go straight out. An https DEX_BASE_URL would therefore bypass
# Tailscale; the deploy script refuses one, and this warns.
#
# The node is ephemeral (`--state=mem:`): nothing is kept between restarts, and
# Tailscale removes the node once it goes offline, so restarts and new
# revisions don't pile up duplicates. Each start joins again, so the key must
# be reusable; a single-use key works once.
#
# The key reaches `tailscale up` as a file, never in argv, and is removed from
# the environment before the command starts.
#
# If joining fails or hangs, the command runs anyway, so /health stays up and
# reports the bridge as unreachable, and a background loop keeps trying to join.
#
# Without TS_AUTHKEY the command still runs, for local tries; the bridge is
# then unreachable and the syncs report that.
set -eu

TS_SOCKET=/tmp/tailscaled.sock
TS_LOG=/tmp/tailscaled.log

# `tailscale up` waits for ever by default, and with a rejected key (spent,
# expired, revoked) it never returns, so each attempt gets 15 s.
join() {
    tailscale --socket="$TS_SOCKET" up --auth-key="file:$key_file" --timeout=15s \
        --hostname="${TS_HOSTNAME:-agentic-vending}" >/dev/null 2>&1
}

if [ -n "${TS_AUTHKEY:-}" ]; then
    key_file=$(mktemp)
    printf '%s' "$TS_AUTHKEY" >"$key_file"
    unset TS_AUTHKEY
    case "${DEX_BASE_URL:-}" in
        https://*) echo "warning: DEX_BASE_URL is https, which bypasses Tailscale, so the bridge is unreachable" >&2 ;;
    esac
    tailscaled --tun=userspace-networking --state=mem: --socket="$TS_SOCKET" \
        --socks5-server=localhost:1055 \
        --outbound-http-proxy-listen=localhost:1055 \
        >"$TS_LOG" 2>&1 &
    # Keep trying for 30 s (the daemon's socket may not be up yet), so at most
    # about 45 s before the command starts. The key is pre-authorized and
    # tagged, so no approval step.
    deadline=$(($(date +%s) + 30))
    joined=yes
    until join; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            joined=no
            echo "tailscale up failed; running without the tailnet and retrying every 60 s. tailscaled says:" >&2
            tail -n 20 "$TS_LOG" >&2 || true
            (
                until join; do sleep 60; done
                rm -f "$key_file"
                echo "tailscale: joined after retrying: $(tailscale --socket="$TS_SOCKET" status --peers=false 2>/dev/null | head -1)"
            ) &
            break
        fi
        sleep 2
    done
    if [ "$joined" = yes ]; then
        rm -f "$key_file"
        echo "tailscale: $(tailscale --socket="$TS_SOCKET" status --peers=false 2>/dev/null | head -1)"
    fi
    # Set either way: until the node joins, the proxy answers 502 or can't
    # connect, and `vending.bridge` reports either as unreachable once
    # /health fails too, so the syncs stop instead of trying every day.
    export http_proxy="http://localhost:1055"
    export no_proxy="${no_proxy:-localhost,127.0.0.1}"
else
    echo "TS_AUTHKEY is not set: running without Tailscale, so the bridge is unreachable" >&2
fi

# exec, so the command is PID 1 and gets Azure's SIGTERM itself; the worker
# handles it. Nothing logs tailscaled out: the node is ephemeral, so Tailscale
# removes it once it goes offline.
exec "$@"
