#!/bin/bash
# Scanner supervisor for Linux hosts without systemd (containers, sandbox VMs). Keeps
# Scanner and Aria2 running, plus an optional Cloudflare
# tunnel (cloudflared) and sshd. Installed and started by install/install-nosystemd.sh.
#
#   bash install/supervise.sh          start the supervisor (idempotent, detaches)
#   bash install/supervise.sh status   one line per managed process
#
# Optional pieces are enabled by files in <project>/.supervisor/:
#   tunnel.token       Cloudflare tunnel token -> runs bin/cloudflared tunnel run
#   authorized_keys    public keys for root    -> runs sshd (127.0.0.1 only, key login only)
set -u

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE="$DIR/.supervisor"
LOG="$DIR/supervisor.log"
SELF="$DIR/install/supervise.sh"
LOCK="/run/scanner-supervise-$(printf '%s' "$DIR" | cksum | cut -d' ' -f1).lock"
INTERVAL=20

if [ "$(id -u)" != 0 ]; then
    exec sudo -n bash "$SELF" "$@"
fi

SCANNER_PATTERN="$DIR/venv/bin/python app.py"
ARIA2_PATTERN="$DIR/bin/aria2c --conf-path="
TUNNEL_PATTERN="$DIR/bin/cloudflared .*--token-file $STATE/tunnel.token"
SSHD_PATTERN='^sshd: /usr/sbin/sshd'
SUPERVISOR_PATTERN="^bash $SELF\$"

tunnel_enabled() { [ -s "$STATE/tunnel.token" ]; }
ssh_enabled() { [ -s "$STATE/authorized_keys" ]; }

if [ "${1:-}" = status ]; then
    st() { if pgrep -f "$2" >/dev/null; then echo "$1: RUNNING"; else echo "$1: DOWN"; fi; }
    st supervisor "$SUPERVISOR_PATTERN"
    st scanner "$SCANNER_PATTERN"
    st aria2 "$ARIA2_PATTERN"
    if tunnel_enabled; then st cloudflared "$TUNNEL_PATTERN"; else echo "cloudflared: DISABLED"; fi
    if ssh_enabled; then st sshd "$SSHD_PATTERN"; else echo "sshd: DISABLED"; fi
    exit 0
fi

# Run detached from the caller: a shell hook or an agent's exec channel may hand us pipes
# that must not stay open, so close every inherited descriptor and log to a file instead.
if [ -z "${SCANNER_SUPERVISE_DETACHED:-}" ]; then
    SCANNER_SUPERVISE_DETACHED=1 setsid bash "$SELF" "$@" </dev/null >/dev/null 2>&1 &
    exit 0
fi
for fd in $(seq 3 63); do eval "exec $fd>&-" 2>/dev/null; done
exec </dev/null >>"$LOG" 2>&1

# One supervisor per project directory. Children get 9>&- so they never hold the lock.
exec 9>"$LOCK"
flock -n 9 || exit 0
log() { echo "$(date '+%F %T') $*"; }
log "supervisor start (pid $$)"

load_env() {
    set -a
    # shellcheck disable=SC1091
    [ -f "$DIR/scanner.env" ] && . "$DIR/scanner.env"
    set +a
}

# Some sandboxes resolve every name to a fake-ip proxy range (198.18.0.0/15), so cloudflared
# cannot reach the real tunnel edge. Pin the edge names in /etc/hosts (idempotent; /etc may
# be reset, so this also runs before every cloudflared start).
TUNNEL_EDGE_HOSTS='198.41.192.27 region1.v2.argotunnel.com
198.41.200.53 region2.v2.argotunnel.com'
fix_tunnel_edge_dns() {
    local ip host
    while read -r ip host; do
        grep -qxF "$ip $host" /etc/hosts 2>/dev/null && continue
        sed -i "/[[:space:]]$host\$/d" /etc/hosts
        echo "$ip $host" >> /etc/hosts
        log "pinned $host -> $ip in /etc/hosts"
    done <<< "$TUNNEL_EDGE_HOSTS"
}

prepare_sshd() {
    local key
    if [ ! -x /usr/sbin/sshd ]; then
        log "installing openssh-server"
        DEBIAN_FRONTEND=noninteractive apt-get update -qq \
            && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server \
            || { log "openssh-server install failed"; return 1; }
    fi
    install -d -m 700 "$STATE/ssh"
    # Keep host keys in the project dir so a rebuilt box presents the same fingerprint.
    if ls "$STATE"/ssh/ssh_host_*_key >/dev/null 2>&1; then
        for key in "$STATE"/ssh/ssh_host_*_key; do
            install -m 600 "$key" "/etc/ssh/${key##*/}"
            [ -f "$key.pub" ] && install -m 644 "$key.pub" "/etc/ssh/${key##*/}.pub"
        done
    else
        ssh-keygen -A >/dev/null 2>&1
        cp -p /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub "$STATE/ssh/" 2>/dev/null
    fi
    install -d -m 755 /etc/ssh/sshd_config.d /run/sshd
    printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\nListenAddress 127.0.0.1\n' \
        > /etc/ssh/sshd_config.d/99-scanner-tunnel.conf
    install -d -m 700 /root/.ssh
    touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
    while IFS= read -r key; do
        [ -n "$key" ] && ! grep -qxF "$key" /root/.ssh/authorized_keys && echo "$key" >> /root/.ssh/authorized_keys
    done < "$STATE/authorized_keys"
}

ssh_ready=0
if ssh_enabled && prepare_sshd; then ssh_ready=1; fi
tunnel_enabled && fix_tunnel_edge_dns

while true; do
    if [ "$ssh_ready" = 1 ] && ! pgrep -f "$SSHD_PATTERN" >/dev/null; then
        log "starting sshd"
        /usr/sbin/sshd 9>&- || log "sshd failed to start"
    fi
    if tunnel_enabled && [ -x "$DIR/bin/cloudflared" ] && ! pgrep -f "$TUNNEL_PATTERN" >/dev/null; then
        log "starting cloudflared"
        fix_tunnel_edge_dns
        setsid "$DIR/bin/cloudflared" --no-autoupdate tunnel run --token-file "$STATE/tunnel.token" \
            9>&- </dev/null >>"$DIR/cloudflared.log" 2>&1 &
    fi
    if [ -f "$DIR/app.py" ] && [ -x "$DIR/venv/bin/python" ]; then
        if [ -x "$DIR/bin/aria2c" ] && [ -f "$DIR/aria2/aria2.conf" ] && ! pgrep -f "$ARIA2_PATTERN" >/dev/null; then
            log "starting aria2"
            # exec: the subshell becomes the service, so no copy of this script lingers holding fd 9
            (cd "$DIR" && load_env && exec setsid "$DIR/bin/aria2c" --conf-path="$DIR/aria2/aria2.conf" --daemon=false) \
                9>&- </dev/null >>"$DIR/aria2.log" 2>&1 &
        fi
        if ! pgrep -f "$SCANNER_PATTERN" >/dev/null; then
            log "starting scanner"
            (cd "$DIR" && load_env && exec setsid "$DIR/venv/bin/python" app.py) \
                9>&- </dev/null >>"$DIR/scanner.log" 2>>"$DIR/scanner_error.log" &
        fi
    fi
    sleep "$INTERVAL" 9>&-
done
