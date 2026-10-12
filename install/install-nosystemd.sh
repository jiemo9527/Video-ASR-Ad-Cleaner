#!/bin/bash
# Scanner installer/updater for Linux hosts WITHOUT systemd (containers, sandbox VMs such as
# Grok Bot computers, where only one directory survives rebuilds). Non-interactive, so an
# agent can run it. Safe to re-run: a second run updates code and dependencies and keeps
# data, settings, scanner.env, Aria2 config/rpc-secret and the tunnel/SSH files.
#
#   sudo bash -c 'bash <(curl -fsSL https://raw.githubusercontent.com/jiemo9527/Video-ASR-Ad-Cleaner/main/install/install-nosystemd.sh)'
#
# Environment (all optional):
#   SCANNER_DIR            install dir                    (default /workspace/scanner_web)
#   SCANNER_DOWNLOAD_DIR   Aria2 dir and Scanner scan_path (default /workspace/downloads)
#   SCANNER_HOST           bind address                   (default 0.0.0.0)
#   SCANNER_PORT           Scanner port                   (default 5000)
#   ARIA2_RPC_PORT         Aria2 RPC port, 127.0.0.1 only (default 6802)
#   TUNNEL_TOKEN           Cloudflare tunnel token; installs cloudflared and keeps the tunnel up
#   SSH_AUTHORIZED_KEY     public key for root; installs sshd (127.0.0.1:22, key login only)
#                          so a tunnel route "SSH -> localhost:22" can reach the box
#   SCANNER_PROJECT_ARCHIVE_URL  project zip to deploy (default: GitHub main)
#
# The supervisor (install/supervise.sh) is started immediately and re-started from
# /etc/bash.bashrc whenever a shell opens, because there is no init system to do it.
# A box that hibernates only resumes the processes when something runs a shell on it.
set -u

PROJECT_ARCHIVE_URL="${SCANNER_PROJECT_ARCHIVE_URL:-https://github.com/jiemo9527/Video-ASR-Ad-Cleaner/archive/refs/heads/main.zip}"
DIR="${SCANNER_DIR:-/workspace/scanner_web}"
DIR="${DIR%/}"
DOWNLOAD_DIR="${SCANNER_DOWNLOAD_DIR:-/workspace/downloads}"
DOWNLOAD_DIR="${DOWNLOAD_DIR%/}"
HOST="${SCANNER_HOST:-0.0.0.0}"
PORT="${SCANNER_PORT:-5000}"
RPC_PORT="${ARIA2_RPC_PORT:-6802}"
STATE="$DIR/.supervisor"
HOOK_MARK="# scanner-supervise:$DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; NC='\033[0m'
step() { echo -e "${GREEN}>>> $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
die() { echo -e "${RED}❌ $*${NC}" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "请使用 root 运行（sudo bash ...）。"
case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) die "不支持的 CPU 架构: $(uname -m)" ;;
esac
[[ "$PORT" =~ ^[0-9]+$ ]] && [[ "$RPC_PORT" =~ ^[0-9]+$ ]] || die "SCANNER_PORT / ARIA2_RPC_PORT 必须是数字。"
if [ -d /run/systemd/system ]; then
    warn "检测到 systemd：常规主机请改用 install/install.sh（systemd 服务）。继续按无 systemd 方式安装。"
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
fetch() { curl -fL --retry 3 -sS -o "$1" "$2"; }

step "[1/7] 检查系统依赖"
missing=()
for cmd in curl unzip tar openssl ffmpeg ffprobe python3 flock setsid pgrep; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
python3 -c 'import ensurepip, venv' >/dev/null 2>&1 || missing+=(python3-venv)
if [ "${#missing[@]}" -gt 0 ]; then
    echo "缺少: ${missing[*]}，通过 apt 安装..."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            curl unzip tar openssl ffmpeg python3 python3-venv util-linux procps ca-certificates >/dev/null \
        || die "系统依赖安装失败。"
fi

step "[2/7] 下载并部署项目到 $DIR"
fetch "$WORK/scanner.zip" "$PROJECT_ARCHIVE_URL" || die "项目下载失败: $PROJECT_ARCHIVE_URL"
unzip -q "$WORK/scanner.zip" -d "$WORK/src" || die "项目压缩包解压失败。"
APP_PY=$(find "$WORK/src" -type f -name app.py -print -quit)
[ -n "$APP_PY" ] || die "项目压缩包中未找到 app.py。"
mkdir -p "$DIR" "$DOWNLOAD_DIR" "$DIR/bin"
# Code files only; runtime data (instance/, scanner.env, aria2/, .supervisor/, logs) is not in the archive.
cp -a "$(dirname "$APP_PY")"/. "$DIR/" || die "无法部署项目文件。"
chmod +x "$DIR/trigger.sh" "$DIR/install/supervise.sh"

step "[3/7] 安装 Python 依赖"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv" || die "venv 创建失败。"
"$DIR/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt" || die "Python 依赖安装失败。"

step "[4/7] 安装 aria2 / rclone / cloudflared 到 $DIR/bin"
if [ ! -x "$DIR/bin/aria2c" ]; then
    release=$(curl -fsSL https://api.github.com/repos/P3TERX/Aria2-Pro-Core/releases/latest \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p')
    [ -n "$release" ] || die "无法获取 P3TERX Aria2 版本。"
    fetch "$WORK/aria2.tgz" "https://github.com/P3TERX/Aria2-Pro-Core/releases/download/$release/aria2-${release%_*}-static-linux-$ARCH.tar.gz" \
        || die "Aria2 下载失败。"
    mkdir -p "$WORK/aria2" && tar -xzf "$WORK/aria2.tgz" -C "$WORK/aria2"
    install -m 755 "$(find "$WORK/aria2" -type f -name aria2c -print -quit)" "$DIR/bin/aria2c" || die "Aria2 安装失败。"
fi
if [ ! -x "$DIR/bin/rclone" ]; then
    fetch "$WORK/rclone.zip" "https://downloads.rclone.org/rclone-current-linux-$ARCH.zip" || die "rclone 下载失败。"
    unzip -q "$WORK/rclone.zip" -d "$WORK/rclone"
    install -m 755 "$(find "$WORK/rclone" -type f -name rclone -print -quit)" "$DIR/bin/rclone" || die "rclone 安装失败。"
else
    "$DIR/bin/rclone" selfupdate >/dev/null 2>&1 || warn "rclone selfupdate 失败，保留现有版本。"
fi
mkdir -p "$STATE" && chmod 700 "$STATE"
if [ -n "${TUNNEL_TOKEN:-}" ]; then
    (umask 077; printf '%s\n' "$TUNNEL_TOKEN" > "$STATE/tunnel.token")
fi
if [ -s "$STATE/tunnel.token" ] && [ ! -x "$DIR/bin/cloudflared" ]; then
    fetch "$DIR/bin/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH" \
        || die "cloudflared 下载失败。"
    chmod 755 "$DIR/bin/cloudflared"
fi
if [ -n "${SSH_AUTHORIZED_KEY:-}" ]; then
    touch "$STATE/authorized_keys" && chmod 600 "$STATE/authorized_keys"
    grep -qxF "$SSH_AUTHORIZED_KEY" "$STATE/authorized_keys" || printf '%s\n' "$SSH_AUTHORIZED_KEY" >> "$STATE/authorized_keys"
fi

step "[5/7] 配置 Aria2 与 scanner.env"
A="$DIR/aria2"
if [ ! -f "$A/aria2.conf" ]; then
    tar -xf "$DIR/install/aria2-config/default.tar" -C "$WORK" || die "Aria2 配置模板解压失败。"
    rm -rf "$A" && mv "$WORK/.aria2c" "$A"
    touch "$A/.scanner-managed"
    for f in "$A/aria2.conf" "$A/script.conf" "$A"/*.sh; do
        [ -f "$f" ] && sed -i "s|/root/.aria2c|$A|g" "$f"
    done
    sed -i -E "s|^dir=.*|dir=$DOWNLOAD_DIR|; s|^rpc-listen-port=.*|rpc-listen-port=$RPC_PORT|; s|^rpc-listen-all=.*|rpc-listen-all=false|" "$A/aria2.conf"
    sed -i -E "s|^on-download-complete=.*|on-download-complete=$DIR/trigger.sh|" "$A/aria2.conf"
    grep -q '^on-download-complete=' "$A/aria2.conf" || echo "on-download-complete=$DIR/trigger.sh" >> "$A/aria2.conf"
    secret=$(openssl rand -hex 32)
    if grep -q '^rpc-secret=' "$A/aria2.conf"; then
        sed -i "s|^rpc-secret=.*|rpc-secret=$secret|" "$A/aria2.conf"
    else
        echo "rpc-secret=$secret" >> "$A/aria2.conf"
    fi
    touch "$A/aria2.session"
    chmod 600 "$A/aria2.conf"
    chmod +x "$A"/*.sh 2>/dev/null
    echo "Aria2 配置已生成（新 rpc-secret，RPC 仅监听 127.0.0.1:$RPC_PORT）。"
else
    echo "保留现有 Aria2 配置。"
fi
if [ ! -f "$DIR/scanner.env" ]; then
    cat > "$DIR/scanner.env" <<EOF
SCANNER_HOST=$HOST
SCANNER_PORT=$PORT
SCANNER_ARIA2_CONFIG_DIR=$A
RCLONE_CONFIG=$DIR/rclone.conf
PATH=$DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PYTHONUNBUFFERED=1
EOF
    echo "scanner.env 已生成（$HOST:$PORT）。"
else
    echo "保留现有 scanner.env。"
fi
[ -f "$DIR/rclone.conf" ] || { touch "$DIR/rclone.conf"; chmod 600 "$DIR/rclone.conf"; }

step "[6/7] 初始化数据库默认路径"
(cd "$DIR" && set -a && . ./scanner.env && set +a && SCANNER_DOWNLOAD_DIR="$DOWNLOAD_DIR" ./venv/bin/python - <<'PY' 2>/dev/null) \
    || warn "数据库初始化跳过（首次启动时会自动创建）。"
import os
import app
with app.app.app_context():
    app.db.create_all()
    if not app.Config.query.get('scan_path'):
        app.db.session.add(app.Config(key='scan_path', value=os.environ['SCANNER_DOWNLOAD_DIR']))
        app.db.session.commit()
        print('scan_path =', os.environ['SCANNER_DOWNLOAD_DIR'])
PY

step "[7/7] 启动守护进程"
for rc in /etc/bash.bashrc /root/.bashrc; do
    [ -f "$rc" ] || continue
    grep -qF "$HOOK_MARK" "$rc" || \
        echo "[ -x $DIR/install/supervise.sh ] && ( sudo -n bash $DIR/install/supervise.sh >/dev/null 2>&1 & ) $HOOK_MARK" >> "$rc"
done
was_running=0
pgrep -f "$DIR/venv/bin/python app.py" >/dev/null && was_running=1
bash "$DIR/install/supervise.sh"
if [ "$was_running" = 1 ]; then
    pkill -f "$DIR/venv/bin/python app.py"   # restart onto the new code; the supervisor brings it back
    echo "Scanner 将在 20 秒内以新代码重启。"
fi
for _ in $(seq 1 30); do
    [ -s "$DIR/.initial_admin_credentials" ] && break
    [ "$was_running" = 1 ] && break
    sleep 1
done
sleep 3

echo -e "------------------------------------------------"
echo -e "${GREEN}🎉 完成${NC}"
bash "$DIR/install/supervise.sh" status
if [ -s "$DIR/.initial_admin_credentials" ]; then
    echo -e "初始账号: $(sed -n 's/^username=//p' "$DIR/.initial_admin_credentials") / $(sed -n 's/^password=//p' "$DIR/.initial_admin_credentials")"
    rm -f "$DIR/.initial_admin_credentials"
fi
echo -e "📂 安装路径: $DIR    📥 下载目录: $DOWNLOAD_DIR"
echo -e "📡 Scanner: http://$HOST:$PORT （隧道路由: HTTP -> localhost:$PORT）"
[ -s "$STATE/tunnel.token" ] || echo -e "${CYAN}未提供 TUNNEL_TOKEN：如需外网访问，设置后重跑本脚本。${NC}"
[ -s "$DIR/rclone.conf" ] || echo -e "${CYAN}rclone.conf 为空：请写入 $DIR/rclone.conf 后在设置页填写 Remote。${NC}"
echo -e "🔁 状态: bash $DIR/install/supervise.sh status    日志: $DIR/supervisor.log"
echo -e "------------------------------------------------"
