#!/bin/bash
# Scanner supervisor for Linux hosts without systemd (containers, sandbox VMs). Keeps
# Scanner and Aria2 running, plus an optional Cloudflare tunnel (cloudflared) and sshd, and
# repairs what a host rebuild wipes. Installed and started by install/install-nosystemd.sh.
#
#   bash install/supervise.sh [start]          start the watchdog loop (idempotent, detaches)
#   bash install/supervise.sh status           one line per managed process
#   bash install/supervise.sh repair           fix env/deps/links/sshd/hosts now, then start
#   bash install/supervise.sh stop [--all]     stop watchdog + Scanner + Aria2 (--all: + tunnel, sshd)
#   bash install/supervise.sh restart          stop (no --all) then start
#   bash install/supervise.sh install-keepalive  (re)install shell hooks and cron entries
#   bash install/supervise.sh ensure-env [--force]  create/update the Python env only
#
# Optional pieces are enabled by files in <project>/.supervisor/:
#   tunnel.token       Cloudflare tunnel token -> runs bin/cloudflared tunnel run
#   authorized_keys    public keys for root    -> runs sshd (127.0.0.1 only, key login only)
#
# Rebuild safety: some hosts wipe any directory literally named "venv" on rebuild, so the
# real environment lives in <project>/pyenv and <project>/venv is a symlink to it.
set -u

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE="$DIR/.supervisor"
LOG="$DIR/supervisor.log"
SELF="$DIR/install/supervise.sh"
LOCK="/run/scanner-supervise-$(printf '%s' "$DIR" | cksum | cut -d' ' -f1).lock"
PIDFILE="$STATE/supervise.pid"
ENV_LOCK="$STATE/env.lock"
PYENV="$DIR/pyenv"
VENV="$DIR/venv"
INTERVAL=20
RETRY_SECONDS=300            # minimum gap between automatic env/sshd repair attempts
HOOK_MARK="# scanner-supervise:$DIR"

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
log() { echo "$(date '+%F %T') $*"; }
# Foreground commands print and also keep a record in supervisor.log.
flog() { log "$*" | tee -a "$LOG"; }

supervisor_running() {
    [ -e "$LOCK" ] || return 1
    ! flock -n "$LOCK" true 2>/dev/null
}

status() {
    st() { if pgrep -f "$2" >/dev/null; then echo "$1: RUNNING"; else echo "$1: DOWN"; fi; }
    if supervisor_running || pgrep -f "$SUPERVISOR_PATTERN" >/dev/null; then
        echo "supervisor: RUNNING"
    else
        echo "supervisor: DOWN"
    fi
    st scanner "$SCANNER_PATTERN"
    st aria2 "$ARIA2_PATTERN"
    if tunnel_enabled; then st cloudflared "$TUNNEL_PATTERN"; else echo "cloudflared: DISABLED"; fi
    if ssh_enabled; then st sshd "$SSHD_PATTERN"; else echo "sshd: DISABLED"; fi
}

load_env() {
    set -a
    # shellcheck disable=SC1091
    [ -f "$DIR/scanner.env" ] && . "$DIR/scanner.env"
    set +a
}

apt_install() {
    # 9>&-: a package postinst may start a daemon, which must not inherit the watchdog lock.
    { DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null; } 9>&-
}

# Packages a rebuilt host may have lost (only /workspace-style dirs survive there).
ensure_system_deps() {
    local pkgs=() cmd
    for cmd in curl flock setsid pgrep python3; do
        command -v "$cmd" >/dev/null 2>&1 && continue
        case "$cmd" in
            flock|setsid) pkgs+=(util-linux) ;;
            pgrep) pkgs+=(procps) ;;
            *) pkgs+=("$cmd") ;;
        esac
    done
    python3 -c 'import ensurepip, venv' >/dev/null 2>&1 || pkgs+=(python3-venv)
    if [ ! -x "$DIR/bin/ffmpeg" ] && ! command -v ffmpeg >/dev/null 2>&1; then pkgs+=(ffmpeg); fi
    [ "${#pkgs[@]}" -gt 0 ] || return 0
    log "installing system packages: ${pkgs[*]}"
    apt_install "${pkgs[@]}" ca-certificates || { log "apt install failed: ${pkgs[*]}"; return 1; }
}

python_ok() { [ -x "$1/bin/python" ] && "$1/bin/python" -c 'import sys' >/dev/null 2>&1; }

# Create/repair <project>/pyenv and point <project>/venv at it. With --force (or when the
# requirements changed) also re-runs pip install. Serialized so repair and the loop never
# build the env at the same time.
ensure_env() {
    local force="${1:-}" stamp want
    mkdir -p "$STATE" && chmod 700 "$STATE"
    (
        flock -w 900 8 || { log "env lock busy, skipping"; exit 1; }
        if [ -d "$VENV" ] && [ ! -L "$VENV" ]; then
            if [ ! -e "$PYENV" ]; then
                # Scripts inside keep working: their shebang path still resolves via the symlink.
                mv "$VENV" "$PYENV" && log "moved venv/ to pyenv/ (rebuild-safe)"
            elif python_ok "$PYENV"; then
                rm -rf "$VENV" && log "removed stray venv/ dir, pyenv/ is healthy"
            else
                rm -rf "$PYENV" && mv "$VENV" "$PYENV" && log "replaced broken pyenv/ with venv/"
            fi
        fi
        if ! python_ok "$PYENV"; then
            log "creating Python env at $PYENV"
            rm -rf "$PYENV"
            python3 -m venv "$PYENV" || { log "python3 -m venv failed"; exit 1; }
            force=--force
        fi
        if [ "$(readlink "$VENV" 2>/dev/null)" != pyenv ]; then
            rm -rf "$VENV" && ln -s pyenv "$VENV" && log "linked venv -> pyenv"
        fi
        [ -f "$DIR/requirements.txt" ] || exit 0
        want=$( { cat "$DIR/requirements.txt"; "$PYENV/bin/python" -V 2>&1; } | cksum | cut -d' ' -f1)
        stamp=$(cat "$PYENV/.scanner-requirements" 2>/dev/null)
        if [ "$force" = --force ] || [ "$stamp" != "$want" ]; then
            log "installing Python requirements"
            "$PYENV/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1
            if "$PYENV/bin/python" -m pip install -q -r "$DIR/requirements.txt"; then
                echo "$want" > "$PYENV/.scanner-requirements"
                log "Python requirements OK"
            else
                rm -f "$PYENV/.scanner-requirements"
                log "pip install failed"; exit 1
            fi
        fi
    ) 8>"$ENV_LOCK" 9>&-
}

# A project-local ffmpeg (bin/ffmpeg) is the default on PATH; /usr/local/bin may be reset.
ensure_ffmpeg_links() {
    local b
    for b in ffmpeg ffprobe; do
        [ -x "$DIR/bin/$b" ] || continue
        [ "$(readlink "/usr/local/bin/$b" 2>/dev/null)" = "$DIR/bin/$b" ] && continue
        if [ -e "/usr/local/bin/$b" ] && [ ! -L "/usr/local/bin/$b" ]; then continue; fi
        mkdir -p /usr/local/bin && ln -sfn "$DIR/bin/$b" "/usr/local/bin/$b" \
            && log "linked /usr/local/bin/$b -> $DIR/bin/$b"
    done
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

sshd_prepared() {
    [ -x /usr/sbin/sshd ] && [ -f /etc/ssh/sshd_config.d/99-scanner-tunnel.conf ] && [ -d /run/sshd ] \
        && ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1
}

prepare_sshd() {
    local key
    if [ ! -x /usr/sbin/sshd ]; then
        log "installing openssh-server"
        apt_install openssh-server || { log "openssh-server install failed"; return 1; }
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
    /usr/sbin/sshd -t 2>/dev/null || { log "sshd config test failed"; return 1; }
}

# No init system: any shell (bash.bashrc / root .bashrc) restarts the watchdog, and so does
# cron when the host has it. Hosts that reset /etc lose these; the next start re-adds them.
install_keepalive() {
    local rc hook="[ -x $SELF ] && ( sudo -n bash $SELF >/dev/null 2>&1 & ) $HOOK_MARK" cur
    for rc in /etc/bash.bashrc /root/.bashrc; do
        [ -f "$rc" ] || continue
        grep -qF "$HOOK_MARK" "$rc" && continue
        echo "$hook" >> "$rc" && log "added keep-alive hook to $rc"
    done
    if command -v crontab >/dev/null 2>&1; then
        cur=$(crontab -l 2>/dev/null)
        if ! grep -qF "$HOOK_MARK" <<< "$cur"; then
            printf '%s\n@reboot bash %s %s\n*/5 * * * * bash %s >/dev/null 2>&1 %s\n' \
                "$cur" "$SELF" "$HOOK_MARK" "$SELF" "$HOOK_MARK" | sed '/^$/d' | crontab - \
                && log "added cron keep-alive (@reboot + every 5 min)"
        fi
        if ! pgrep -x cron >/dev/null && ! pgrep -x crond >/dev/null; then
            { command -v cron >/dev/null && cron 9>&-; } || { command -v crond >/dev/null && crond 9>&-; } || true
        fi
    fi
}

start_detached() {
    SCANNER_SUPERVISE_DETACHED=1 setsid bash "$SELF" </dev/null >/dev/null 2>&1 &
}

wait_healthy() {
    local _
    for _ in $(seq 1 "${1:-30}"); do
        status | grep -q ': DOWN' || return 0
        sleep 1
    done
    return 1
}

stop_all() {
    local pid all="${1:-}"
    pid=$(cat "$PIDFILE" 2>/dev/null)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    pkill -f "$SUPERVISOR_PATTERN" 2>/dev/null
    rm -f "$PIDFILE"
    pkill -f "$SCANNER_PATTERN" 2>/dev/null
    pkill -f "$ARIA2_PATTERN" 2>/dev/null
    if [ "$all" = --all ]; then
        pkill -f "$TUNNEL_PATTERN" 2>/dev/null
        pkill -f "$SSHD_PATTERN \[listener\]" 2>/dev/null
    fi
    flog "stopped watchdog + scanner + aria2${all:+ + cloudflared + sshd}"
}

repair() {
    log "repair: start"
    ensure_system_deps
    ensure_env --force
    ensure_ffmpeg_links
    if ssh_enabled; then prepare_sshd || log "sshd prepare failed"; fi
    tunnel_enabled && fix_tunnel_edge_dns
    install_keepalive
    if supervisor_running; then
        log "repair: watchdog running, it restarts DOWN services within ${INTERVAL}s"
    else
        log "repair: starting watchdog"
        start_detached
    fi
    if wait_healthy 45; then log "repair: all services up"; else log "repair: some services still DOWN, see logs in $DIR"; fi
    status
}

case "${1:-start}" in
    status) status; exit 0 ;;
    repair) repair 2>&1 | tee -a "$LOG"; exit "${PIPESTATUS[0]}" ;;
    stop) stop_all "${2:-}"; exit 0 ;;
    restart) stop_all; sleep 1; start_detached; wait_healthy 30; status; exit 0 ;;
    install-keepalive) install_keepalive 2>&1 | tee -a "$LOG"; exit 0 ;;
    ensure-env) ensure_env "${2:-}" 2>&1 | tee -a "$LOG"; exit "${PIPESTATUS[0]}" ;;
    start) ;;
    -h|--help|help) sed -n '2,20p' "$SELF"; exit 0 ;;
    *) echo "usage: $0 [start|status|repair|stop [--all]|restart|install-keepalive|ensure-env [--force]]" >&2; exit 2 ;;
esac

# start: run detached from the caller. A shell hook or an agent's exec channel may hand us
# pipes that must not stay open, so close every inherited descriptor and log to a file.
if [ -z "${SCANNER_SUPERVISE_DETACHED:-}" ]; then
    supervisor_running && exit 0
    start_detached
    exit 0
fi
for fd in $(seq 3 63); do eval "exec $fd>&-" 2>/dev/null; done
exec </dev/null >>"$LOG" 2>&1

# One supervisor per project directory. Children get 9>&- so they never hold the lock.
exec 9>"$LOCK"
flock -n 9 || exit 0
mkdir -p "$STATE" && chmod 700 "$STATE"
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' TERM INT
log "supervisor start (pid $$)"

install_keepalive
ensure_ffmpeg_links
ssh_ready=0
if ssh_enabled && prepare_sshd; then ssh_ready=1; fi
tunnel_enabled && fix_tunnel_edge_dns
last_env_try=0
last_ssh_try=$(date +%s)

while true; do
    now=$(date +%s)
    # Watchdog repairs: a host rebuild can wipe the env, /usr/local/bin links and /etc/ssh.
    if [ -f "$DIR/app.py" ] && ! python_ok "$VENV" && [ $((now - last_env_try)) -ge "$RETRY_SECONDS" ]; then
        last_env_try=$now
        log "Python env missing/broken, repairing"
        ensure_system_deps
        ensure_env
    fi
    ensure_ffmpeg_links
    if ssh_enabled && { [ "$ssh_ready" = 0 ] || ! sshd_prepared; } && [ $((now - last_ssh_try)) -ge "$RETRY_SECONDS" ]; then
        last_ssh_try=$now
        if prepare_sshd; then ssh_ready=1; else ssh_ready=0; fi
    fi
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
    sleep "$INTERVAL" 9>&- &
    wait $! 2>/dev/null
done
