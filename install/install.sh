#!/bin/bash

# ================= 默认配置 =================
PROJECT_ARCHIVE_URL="${SCANNER_PROJECT_ARCHIVE_URL:-https://github.com/jiemo9527/Video-ASR-Ad-Cleaner/archive/refs/heads/main.zip}"
DEFAULT_INSTALL_DIR="/www/wwwroot/scanner_web"
DEFAULT_ARIA2_CONF="/root/.aria2c/aria2.conf"
DEFAULT_ARIA2_CONFIG_DIR="/root/.aria2c"
ARIA2_CONFIG_ARCHIVE_RELATIVE="install/aria2-config/default.tar"
ARIA2_CONFIG_MANAGED_MARKER=".scanner-managed"
SERVICE_NAME="scanner"
DEFAULT_SCANNER_PORT=5000
DEFAULT_NGINX_HTTPS_PORT=5001
DEFAULT_ARIA2_RPC_PORT=6802
# ===========================================

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Run a temporary copy so an update can safely replace the installed script itself.
if [ -z "${SCANNER_INSTALLER_REEXEC:-}" ] && [ -f "$0" ]; then
    installer_copy=$(mktemp) || exit 1
    if ! cp "$0" "$installer_copy"; then
        rm -f "$installer_copy"
        exit 1
    fi
    SCANNER_INSTALLER_REEXEC=1 bash "$installer_copy" "$@"
    installer_status=$?
    rm -f "$installer_copy"
    exit "$installer_status"
fi

function tcp_port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .
    else
        netstat -ltn 2>/dev/null | awk -v port=":$port" '$4 ~ port "$" { found=1 } END { exit !found }'
    fi
}

function find_available_tcp_port() {
    local port
    for _ in {1..100}; do
        port=$((10000 + RANDOM))
        if ! tcp_port_in_use "$port"; then
            printf '%s\n' "$port"
            return 0
        fi
    done
    return 1
}

function install_p3terx_aria2() {
    local architecture release archive_url workdir binary

    if [ -x "/usr/local/bin/aria2c" ]; then
        echo -e "${GREEN}✅ 检测到自定义 Aria2 二进制: /usr/local/bin/aria2c${NC}"
        return 0
    fi

    case "$(uname -m)" in
        x86_64) architecture="amd64" ;;
        aarch64) architecture="arm64" ;;
        armv7l|armv6l) architecture="armhf" ;;
        i?86) architecture="i386" ;;
        *)
            echo -e "${RED}❌ 不支持的 Aria2 CPU 架构: $(uname -m)${NC}"
            return 1
            ;;
    esac

    release=$(
        (curl -fsSL "https://api.github.com/repos/P3TERX/Aria2-Pro-Core/releases/latest" \
            || curl -fsSL "https://gh-api.p3terx.com/repos/P3TERX/Aria2-Pro-Core/releases/latest") 2>/dev/null \
            | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p'
    )
    if [ -z "$release" ]; then
        echo -e "${RED}❌ 无法获取 P3TERX Aria2 发布版本。${NC}"
        return 1
    fi

    archive_url="https://github.com/P3TERX/Aria2-Pro-Core/releases/download/$release/aria2-${release%_*}-static-linux-$architecture.tar.gz"
    workdir=$(mktemp -d)
    if ! curl -fL --retry 3 -o "$workdir/aria2.tar.gz" "$archive_url" \
        || ! tar -xzf "$workdir/aria2.tar.gz" -C "$workdir"; then
        rm -rf "$workdir"
        echo -e "${RED}❌ P3TERX Aria2 下载或解压失败。${NC}"
        return 1
    fi

    binary=$(find "$workdir" -type f -name aria2c -print -quit)
    if [ -z "$binary" ]; then
        rm -rf "$workdir"
        echo -e "${RED}❌ P3TERX Aria2 压缩包中未找到 aria2c。${NC}"
        return 1
    fi

    install -m 755 "$binary" "/usr/local/bin/aria2c"
    rm -rf "$workdir"
    echo -e "${GREEN}✅ P3TERX Aria2 已安装: /usr/local/bin/aria2c ($release)${NC}"
}

function install_rclone() {
    local installer installer_status

    echo -e "${CYAN}>>> 使用 rclone 官方安装脚本安装/更新 rclone...${NC}"
    installer=$(mktemp)
    if ! curl -fL --retry 3 -o "$installer" "https://rclone.org/install.sh"; then
        rm -f "$installer"
        echo -e "${RED}❌ rclone 官方安装脚本下载失败。${NC}"
        return 1
    fi
    installer_status=0
    bash "$installer" || installer_status=$?
    if [ "$installer_status" -ne 0 ] && [ "$installer_status" -ne 3 ]; then
        echo -e "${YELLOW}⚠️ rclone 官方安装脚本返回非零，继续检查实际安装结果。${NC}"
    fi
    rm -f "$installer"
    hash -r
    if ! command -v rclone >/dev/null 2>&1; then
        echo -e "${RED}❌ 未找到 rclone，请检查官方安装脚本输出。${NC}"
        return 1
    fi
    echo -e "${GREEN}✅ rclone 已就绪: $(command -v rclone)${NC}"
}

function install_system_dependencies() {
    apt-get update -qq || return 1
    apt-get install -y \
        ffmpeg python3 python3-pip unzip curl ca-certificates libsndfile1 net-tools \
        git cmake build-essential tar gzip openssl > /dev/null || return 1

    for REQUIRED_CMD in ffmpeg ffprobe python3 pip3 git cmake; do
        if ! command -v "$REQUIRED_CMD" >/dev/null 2>&1; then
            echo -e "${YELLOW}⚠️  未检测到 $REQUIRED_CMD，请确认系统依赖安装是否成功。${NC}"
        fi
    done
    install_p3terx_aria2 || return 1
    install_rclone
}

function deploy_project_archive() {
    local install_base="$1"
    local project_tmp project_archive detected_app detected_dir scanner_env_backup

    if [ -d "$install_base" ]; then
        echo -e "${YELLOW}⚠️  目录 $install_base 已存在，正在覆盖代码文件并保留运行数据...${NC}"
    fi
    scanner_env_backup=""
    if [ -f "$install_base/scanner.env" ]; then
        scanner_env_backup=$(mktemp)
        cp -a "$install_base/scanner.env" "$scanner_env_backup" || return 1
    fi

    project_tmp=$(mktemp -d)
    project_archive="$project_tmp/scanner.zip"
    if ! curl -fL --retry 3 -o "$project_archive" "$PROJECT_ARCHIVE_URL"; then
        [ -n "$scanner_env_backup" ] && rm -f "$scanner_env_backup"
        rm -rf "$project_tmp"
        echo -e "${RED}❌ 项目下载失败: $PROJECT_ARCHIVE_URL${NC}"
        return 1
    fi
    if ! unzip -q "$project_archive" -d "$project_tmp/source"; then
        [ -n "$scanner_env_backup" ] && rm -f "$scanner_env_backup"
        rm -rf "$project_tmp"
        echo -e "${RED}❌ 项目压缩包解压失败。${NC}"
        return 1
    fi
    detected_app=$(find "$project_tmp/source" -type f -name "app.py" -print -quit)
    if [ -z "$detected_app" ]; then
        [ -n "$scanner_env_backup" ] && rm -f "$scanner_env_backup"
        rm -rf "$project_tmp"
        echo -e "${RED}❌ 项目压缩包中未找到 app.py。${NC}"
        return 1
    fi
    detected_dir=$(dirname "$detected_app")
    mkdir -p "$install_base"
    if ! cp -a "$detected_dir"/. "$install_base/"; then
        [ -n "$scanner_env_backup" ] && rm -f "$scanner_env_backup"
        rm -rf "$project_tmp"
        echo -e "${RED}❌ 无法部署项目文件到: $install_base${NC}"
        return 1
    fi
    if [ -n "$scanner_env_backup" ]; then
        cp -a "$scanner_env_backup" "$install_base/scanner.env" || {
            rm -f "$scanner_env_backup"
            rm -rf "$project_tmp"
            echo -e "${RED}❌ 无法恢复现有 scanner.env。${NC}"
            return 1
        }
        rm -f "$scanner_env_backup"
    fi
    rm -rf "$project_tmp"
    PROJECT_ROOT="$install_base"
    echo -e "${YELLOW}>>> 项目根目录确认: $PROJECT_ROOT${NC}"
}

function ensure_ariang_assets() {
    local project_root="$1"
    local ariang_dir marker ariang_tmp ariang_index ariang_views

    ariang_dir="$project_root/ariang"
    marker="$ariang_dir/.scanner_ariang_allinone_1.3.14"
    if [ -f "$ariang_dir/index.html" ] && [ -f "$ariang_dir/views/settings-ariang.html" ] && [ -f "$marker" ]; then
        return
    fi

    echo -e "${GREEN}>>> 下载/修复 AriaNg 下载器资源...${NC}"
    ariang_tmp=$(mktemp -d)
    if curl -fL --retry 3 -o "$ariang_tmp/ariang.zip" "https://github.com/mayswind/AriaNg/releases/download/1.3.14/AriaNg-1.3.14-AllInOne.zip" \
        && unzip -q "$ariang_tmp/ariang.zip" -d "$ariang_tmp/extract" \
        && curl -fL --retry 3 -o "$ariang_tmp/source.zip" "https://github.com/mayswind/AriaNg/archive/refs/tags/1.3.14.zip" \
        && unzip -q "$ariang_tmp/source.zip" -d "$ariang_tmp/source"; then
        ariang_index=$(find "$ariang_tmp/extract" -type f -name index.html -print -quit)
        ariang_views=$(find "$ariang_tmp/source" -type d -path '*/src/views' -print -quit)
        if [ -n "$ariang_index" ] && [ -n "$ariang_views" ]; then
            mkdir -p "$ariang_dir"
            cp -a "$(dirname "$ariang_index")"/. "$ariang_dir/"
            cp -a "$ariang_views" "$ariang_dir/"
            touch "$marker"
            echo -e "${GREEN}✅ AriaNg 资源已就绪。${NC}"
        else
            echo -e "${YELLOW}⚠️ AriaNg 压缩包中未找到网页资源或设置模板，跳过。${NC}"
        fi
    else
        echo -e "${YELLOW}⚠️ AriaNg 下载失败，安装后可重新运行脚本补齐。${NC}"
    fi
    rm -rf "$ariang_tmp"
}

function install_python_dependencies() {
    local project_root="$1"
    local pip_xargs=""

    if [ ! -f "$project_root/requirements.txt" ]; then
        echo -e "${YELLOW}⚠️ 未找到 requirements.txt，跳过。${NC}"
        return
    fi
    if pip3 install --help | grep -q "break-system-packages"; then
        echo -e "${YELLOW}>>> 启用 PEP 668 系统保护绕过模式...${NC}"
        pip_xargs="--break-system-packages"
    fi
    echo -e "${CYAN}正在修复系统包冲突 (blinker)...${NC}"
    pip3 install blinker --ignore-installed $pip_xargs > /dev/null 2>&1
    echo -e "${CYAN}正在安装/检查其余依赖...${NC}"
    pip3 install -r "$project_root/requirements.txt" $pip_xargs
}

function get_installed_project_root() {
    local project_root=""

    if [ -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
        project_root=$(grep "WorkingDirectory=" "/etc/systemd/system/$SERVICE_NAME.service" | cut -d= -f2)
    fi
    if [ -z "$project_root" ] || [ ! -f "$project_root/app.py" ]; then
        echo -e "${RED}❌ 未找到已安装的 Scanner 服务或项目目录。${NC}" >&2
        return 1
    fi
    printf '%s\n' "$project_root"
}

function set_aria2_rpc_secret() {
    local config="$1"
    local rpc_secret

    rpc_secret=$(openssl rand -hex 32) || {
        echo -e "${RED}❌ 无法生成 Aria2 rpc-secret。${NC}"
        return 1
    }
    if grep -Eq '^[[:space:]]*rpc-secret[[:space:]]*=' "$config"; then
        sed -i -E "s|^[[:space:]]*rpc-secret[[:space:]]*=.*$|rpc-secret=$rpc_secret|" "$config"
    else
        printf '\nrpc-secret=%s\n' "$rpc_secret" >> "$config"
    fi
}

function choose_aria2_config_path() {
    local selected

    ARIA2_CONFIG_DIR="$DEFAULT_ARIA2_CONFIG_DIR"
    while [ -e "$ARIA2_CONFIG_DIR" ] && [ ! -f "$ARIA2_CONFIG_DIR/$ARIA2_CONFIG_MANAGED_MARKER" ]; do
        echo -e "${YELLOW}⚠️ $ARIA2_CONFIG_DIR 已被非 Scanner 的 Aria2 配置占用。${NC}"
        read -r -p "请输入新的 Aria2 配置目录（留空取消安装）: " selected
        selected=${selected%/}
        if [ -z "$selected" ]; then
            echo -e "${YELLOW}已取消安装，未修改现有 Aria2 配置。${NC}"
            return 1
        fi
        if [[ ! "$selected" =~ ^/[A-Za-z0-9._/-]+$ ]] || [ "$selected" = "/" ]; then
            echo -e "${YELLOW}⚠️ 请输入仅含字母、数字、点、下划线和连字符的非根目录绝对路径。${NC}"
            continue
        fi
        ARIA2_CONFIG_DIR="$selected"
    done
    ARIA2_CONF="$ARIA2_CONFIG_DIR/aria2.conf"
}

function update_aria2_config_paths() {
    local config_dir="$1"
    local file

    [ "$config_dir" = "$DEFAULT_ARIA2_CONFIG_DIR" ] && return
    for file in "$config_dir/aria2.conf" "$config_dir/script.conf" "$config_dir"/*.sh; do
        [ -f "$file" ] || continue
        sed -i "s|$DEFAULT_ARIA2_CONFIG_DIR|$config_dir|g" "$file"
    done
}

function ensure_aria2_session_file() {
    local config="$1"
    local session_path

    session_path=$(awk -F= '/^[[:space:]]*input-file[[:space:]]*=/ { value=$2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", value) } END { print value }' "$config")
    if [ -n "$session_path" ] && [ ! -e "$session_path" ]; then
        install -m 600 /dev/null "$session_path" || {
            echo -e "${RED}❌ 无法创建 Aria2 会话文件: $session_path${NC}"
            return 1
        }
    fi
}

function initialize_scanner_aria2_config() {
    local archive="$1"
    local config="$2"
    local config_dir marker staging_dir

    config_dir=$(dirname "$config")
    marker="$config_dir/$ARIA2_CONFIG_MANAGED_MARKER"
    if [ -e "$config_dir" ]; then
        if [ ! -f "$marker" ]; then
            echo -e "${RED}❌ $config_dir 已存在且不由 Scanner 管理，未覆盖其中的 Aria2 配置。${NC}"
            return 1
        fi
        if [ ! -f "$config" ]; then
            echo -e "${RED}❌ Scanner 管理的 Aria2 配置缺失: $config${NC}"
            return 1
        fi
    else
        if [ ! -f "$archive" ]; then
            echo -e "${RED}❌ 未找到内置 Aria2 配置模板: $archive${NC}"
            return 1
        fi
        if ! tar -tf "$archive" | grep -qx '.aria2c/aria2.conf'; then
            echo -e "${RED}❌ 内置 Aria2 配置模板格式不正确。${NC}"
            return 1
        fi
        echo -e "${GREEN}>>> 初始化 Aria2 配置到 $config_dir...${NC}"
        staging_dir=$(mktemp -d)
        if ! tar -xf "$archive" -C "$staging_dir" || [ ! -f "$staging_dir/.aria2c/aria2.conf" ]; then
            rm -rf "$staging_dir"
            echo -e "${RED}❌ Aria2 配置模板解压失败。${NC}"
            return 1
        fi
        if [ -e "$config_dir" ] || ! mkdir -p "$(dirname "$config_dir")" || ! mv "$staging_dir/.aria2c" "$config_dir"; then
            rm -rf "$staging_dir"
            echo -e "${RED}❌ 无法创建 Aria2 配置目录: $config_dir${NC}"
            return 1
        fi
        rm -rf "$staging_dir"
        if [ ! -f "$config" ]; then
            rm -rf "$config_dir"
            echo -e "${RED}❌ Aria2 配置模板解压失败。${NC}"
            return 1
        fi
        touch "$marker"
    fi

    update_aria2_config_paths "$config_dir" || return 1
    ensure_aria2_session_file "$config" || return 1
    set_aria2_rpc_secret "$config" || return 1
    echo -e "${GREEN}✅ Aria2 配置已就绪，已生成新的 rpc-secret。${NC}"
}

function systemd_service_execstart_missing() {
    local service="$1"
    local exec_path

    exec_path=$(systemctl cat "$service.service" 2>/dev/null | awk -F= '/^ExecStart=/ { split($2, args, " "); print args[1]; exit }')
    [ -n "$exec_path" ] && [ ! -x "$exec_path" ]
}

function remove_scanner_aria2_config() {
    local config_dir marker service unit

    config_dir="${1:-$DEFAULT_ARIA2_CONFIG_DIR}"
    marker="$config_dir/$ARIA2_CONFIG_MANAGED_MARKER"
    if [ ! -f "$marker" ]; then
        echo -e "${YELLOW}⚠️ $config_dir 不是 Scanner 管理的配置，保留不动。${NC}"
        return
    fi

    for service in aria2 aria2c; do
        unit="/etc/systemd/system/$service.service"
        if [ -f "$unit" ] && grep -Fqx '# Managed by Scanner' "$unit"; then
            systemctl disable --now "$service" 2>/dev/null || true
            rm -f "$unit"
            systemctl daemon-reload
            echo -e "${YELLOW}已移除 Scanner 注册的 $service 服务。${NC}"
            break
        fi
    done
    rm -rf "$config_dir"
    echo -e "${GREEN}✅ 已清理 Scanner 管理的 Aria2 配置: $config_dir${NC}"
}

function read_pem_to_file() {
    local destination="$1"
    local label="$2"
    local line

    echo -e "${CYAN}请粘贴$label，完成后单独输入一行 EOF：${NC}"
    : > "$destination"
    chmod 600 "$destination"
    while IFS= read -r line; do
        [ "$line" = "EOF" ] && break
        printf '%s\n' "$line" >> "$destination"
    done

    if [ ! -s "$destination" ]; then
        rm -f "$destination"
        echo -e "${RED}❌ 未收到$label。${NC}"
        return 1
    fi
}

function get_aria2_rpc_port() {
    local config="$1"
    local port
    port=$(awk -F= '/^[[:space:]]*rpc-listen-port[[:space:]]*=/ { value=$2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", value) } END { print value }' "$config")
    if [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ]; then
        printf '%s\n' "$port"
    else
        printf '%s\n' "$DEFAULT_ARIA2_RPC_PORT"
    fi
}

function tcp_port_owned_by_nginx() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -H -ltnp "sport = :$port" 2>/dev/null | grep -q '[n]ginx'
    else
        pgrep -x nginx >/dev/null 2>&1
    fi
}

function aria2_wss_ready() {
    local config="$1"
    local rpc_enabled rpc_secret
    [ -f "$config" ] || return 1

    rpc_enabled=$(awk -F= '/^[[:space:]]*enable-rpc[[:space:]]*=/ { value=$2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", value) } END { print value }' "$config")
    rpc_secret=$(awk -F= '/^[[:space:]]*rpc-secret[[:space:]]*=/ { value=$2; sub(/^[[:space:]]+/, "", value); sub(/[[:space:]]+$/, "", value) } END { print value }' "$config")
    [[ "$rpc_enabled" =~ ^(true|1)$ ]] && [ -n "$rpc_secret" ]
}

function get_aria2_rpc_secret() {
    local config="$1"
    awk -F= '/^[[:space:]]*rpc-secret[[:space:]]*=/ { value=$2; sub(/^[[:space:]]+/, "", value); sub(/[[:space:]]+$/, "", value) } END { print value }' "$config"
}

function nginx_has_other_sites() {
    local site
    for site in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*; do
        [ -e "$site" ] || [ -L "$site" ] || continue
        [ "$site" = "/etc/nginx/sites-enabled/default" ] && continue
        [ "$site" = "/etc/nginx/sites-enabled/scanner" ] && continue
        return 0
    done
    return 1
}

function configure_nginx_https() {
    local nginx_choice cert_source cert_file key_file nginx_site nginx_link site_backup link_target redirect_port

    read -p "是否配置 Nginx HTTPS 反向代理? 输入 y 确认，回车跳过: " nginx_choice
    if [[ ! "$nginx_choice" =~ ^[Yy]$ ]]; then
        return 0
    fi

    read -p "请输入 HTTPS 域名或 IP: " NGINX_SERVER_NAME
    if [[ ! "$NGINX_SERVER_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
        echo -e "${RED}❌ 域名或 IP 只能包含字母、数字、点、连字符和下划线。${NC}"
        return 1
    fi

    NGINX_HTTPS_PORT="$DEFAULT_NGINX_HTTPS_PORT"
    if tcp_port_in_use "$NGINX_HTTPS_PORT"; then
        NGINX_HTTPS_PORT=$(find_available_tcp_port) || {
            echo -e "${RED}❌ 无法找到空闲 HTTPS TCP 端口。${NC}"
            return 1
        }
        echo -e "${YELLOW}⚠️ HTTPS 端口 $DEFAULT_NGINX_HTTPS_PORT 已被占用，自动改用端口 $NGINX_HTTPS_PORT。${NC}"
    fi
    redirect_port=":$NGINX_HTTPS_PORT"

    if ! command -v nginx >/dev/null 2>&1; then
        NGINX_INSTALLED_BY_SCANNER=1
    fi
    if ! command -v nginx >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
        echo -e "${GREEN}>>> 安装 Nginx 和 OpenSSL...${NC}"
        apt-get install -y nginx openssl || return 1
    fi

    while true; do
        read -p "证书来源 [1=已有文件路径, 2=粘贴 PEM 内容] (默认 1): " cert_source
        cert_source=${cert_source:-1}
        case "$cert_source" in
            1)
                read -r -p "证书 fullchain 文件路径: " cert_file
                read -r -p "私钥文件路径: " key_file
                if [ ! -r "$cert_file" ] || [ ! -r "$key_file" ]; then
                    echo -e "${RED}❌ 证书或私钥文件不存在或不可读。${NC}"
                    continue
                fi
                ;;
            2)
                install -d -m 700 /etc/nginx/ssl
                cert_file="/etc/nginx/ssl/scanner.crt"
                key_file="/etc/nginx/ssl/scanner.key"
                read_pem_to_file "$cert_file" "证书" || return 1
                read_pem_to_file "$key_file" "私钥" || return 1
                ;;
            *)
                echo -e "${YELLOW}⚠️ 请输入 1 或 2。${NC}"
                continue
                ;;
        esac

        if ! openssl x509 -in "$cert_file" -noout >/dev/null 2>&1; then
            echo -e "${RED}❌ 证书不是有效的 PEM X.509 文件。${NC}"
            [ "$cert_source" = "2" ] && rm -f "$cert_file"
            continue
        fi
        if ! openssl pkey -in "$key_file" -passin pass: -noout >/dev/null 2>&1; then
            echo -e "${RED}❌ 私钥无效或受密码保护。${NC}"
            [ "$cert_source" = "2" ] && rm -f "$key_file"
            continue
        fi
        break
    done

    nginx_site="/etc/nginx/sites-available/scanner"
    nginx_link="/etc/nginx/sites-enabled/scanner"
    if [ -e "$nginx_link" ] && [ ! -L "$nginx_link" ]; then
        echo -e "${RED}❌ $nginx_link 已存在且不是符号链接，未覆盖。${NC}"
        return 1
    fi

    site_backup=$(mktemp)
    if [ -e "$nginx_site" ]; then
        cp -a "$nginx_site" "$site_backup"
    else
        rm -f "$site_backup"
        site_backup=""
    fi
    if [ -L "$nginx_link" ]; then
        link_target=$(readlink "$nginx_link")
    else
        link_target=""
    fi

    NGINX_ARIA2_WSS_ENABLED=0
    if aria2_wss_ready "$ARIA2_CONF"; then
        ARIA2_RPC_PORT=$(get_aria2_rpc_port "$ARIA2_CONF")
        NGINX_ARIA2_WSS_ENABLED=1
        if tcp_port_in_use 443 && ! tcp_port_owned_by_nginx 443; then
            [ -n "$site_backup" ] && rm -f "$site_backup"
            echo -e "${RED}❌ TCP 443 已被非 Nginx 服务占用，无法创建 Aria2 HTTPS/WSS 入口。${NC}"
            return 1
        fi
    else
        echo -e "${YELLOW}⚠️ Aria2 未启用带 rpc-secret 的 RPC，跳过 WSS 下载器入口。${NC}"
    fi

    cat > "$nginx_site" <<EOF
server {
    listen 80;
    server_name $NGINX_SERVER_NAME;
    return 301 https://\$host$redirect_port\$request_uri;
}

server {
    listen $NGINX_HTTPS_PORT ssl;
    server_name $NGINX_SERVER_NAME;

    ssl_certificate $cert_file;
    ssl_certificate_key $key_file;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:$SCANNER_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
EOF
    if [ "$NGINX_ARIA2_WSS_ENABLED" -eq 1 ]; then
        cat >> "$nginx_site" <<EOF
}

server {
    listen 443 ssl;
    server_name $NGINX_SERVER_NAME;

    ssl_certificate $cert_file;
    ssl_certificate_key $key_file;
    ssl_protocols TLSv1.2 TLSv1.3;

    location = /jsonrpc {
        proxy_pass http://127.0.0.1:$ARIA2_RPC_PORT/jsonrpc;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }

    location = /aria2/jsonrpc {
        proxy_pass http://127.0.0.1:$ARIA2_RPC_PORT/jsonrpc;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
EOF
    fi
    cat >> "$nginx_site" <<EOF
}
EOF
    mkdir -p /etc/nginx/sites-enabled
    ln -sfn "$nginx_site" "$nginx_link"

    if ! nginx -t; then
        if [ -n "$site_backup" ]; then
            cp -a "$site_backup" "$nginx_site"
            rm -f "$site_backup"
        else
            rm -f "$nginx_site"
        fi
        if [ -n "$link_target" ]; then
            ln -sfn "$link_target" "$nginx_link"
        else
            rm -f "$nginx_link"
        fi
        echo -e "${RED}❌ Nginx 配置校验失败，已恢复原配置。${NC}"
        return 1
    fi

    rm -f "$site_backup"
    systemctl enable nginx
    if ! systemctl reload nginx; then
        systemctl restart nginx || {
            echo -e "${RED}❌ Nginx 无法启动，请检查: systemctl status nginx${NC}"
            return 1
        }
    fi
    NGINX_HTTPS_ENABLED=1
    echo -e "${GREEN}✅ Nginx HTTPS 反向代理已启用。${NC}"
}

# 检查 Root 权限
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}❌ 请使用 root 权限运行此脚本。${NC}"
  exit 1
fi

# ================= 功能函数: 安装 =================
function install_app() {
    echo -e "${CYAN}>>> 进入安装流程...${NC}"

    # 1. 交互配置 (路径)
    read -p "请输入安装目录 (回车默认 $DEFAULT_INSTALL_DIR): " USER_DIR
    INSTALL_BASE=${USER_DIR:-$DEFAULT_INSTALL_DIR}
    INSTALL_BASE=${INSTALL_BASE%/} # 去除末尾斜杠

    choose_aria2_config_path || return

    # 2. 系统依赖
    echo -e "${GREEN}>>> [1/5] 安装/检查系统依赖...${NC}"
    install_system_dependencies || return

    # 3. 下载、解压并部署项目
    echo -e "${GREEN}>>> [2/5] 下载并部署项目...${NC}"
    deploy_project_archive "$INSTALL_BASE" || return

    ARIA2_CONFIG_ARCHIVE="$PROJECT_ROOT/$ARIA2_CONFIG_ARCHIVE_RELATIVE"
    initialize_scanner_aria2_config "$ARIA2_CONFIG_ARCHIVE" "$ARIA2_CONF" || return

    ensure_ariang_assets "$PROJECT_ROOT"

    # 5. 配置监听地址
    echo -e "${GREEN}>>> [Extra] 配置网络监听...${NC}"
    read -p "是否仅允许本机访问 (127.0.0.1)? 输入 y 确认，回车默认开放外网 (0.0.0.0): " NET_CHOICE
    APP_PY_PATH="$PROJECT_ROOT/app.py"
    SCANNER_PORT="$DEFAULT_SCANNER_PORT"

    if [[ "$NET_CHOICE" =~ ^[Yy]$ ]]; then
        sed -i "s/0.0.0.0/127.0.0.1/g" "$APP_PY_PATH"
        echo -e "${YELLOW}🔒 已设置为仅本机访问。${NC}"
    else
        sed -i "s/127.0.0.1/0.0.0.0/g" "$APP_PY_PATH"
        if tcp_port_in_use "$SCANNER_PORT"; then
            SCANNER_PORT=$(find_available_tcp_port) || {
                echo -e "${RED}❌ 无法找到空闲公网 TCP 端口。${NC}"
                return
            }
            echo -e "${YELLOW}⚠️ 端口 $DEFAULT_SCANNER_PORT 已被占用，自动改用端口 $SCANNER_PORT。${NC}"
        fi
        echo -e "${YELLOW}🌍 已设置为开放外网访问，端口: $SCANNER_PORT。${NC}"
    fi
    printf 'SCANNER_PORT=%s\nSCANNER_ARIA2_CONFIG_DIR=%s\n' "$SCANNER_PORT" "$ARIA2_CONFIG_DIR" > "$PROJECT_ROOT/scanner.env"

    # 6. Python 依赖 (当前版本使用 GGUF/llama.cpp，本地 ASR 不再依赖 Torch/FunASR)
    echo -e "${GREEN}>>> [3/5] 安装 Python 依赖...${NC}"
    install_python_dependencies "$PROJECT_ROOT"

    # 7. Aria2 运行集成
    echo -e "${GREEN}>>> [4/5] 检查 Aria2 服务...${NC}"
    TRIGGER_SCRIPT="$PROJECT_ROOT/trigger.sh"
    chmod +x "$TRIGGER_SCRIPT"

    if [ -f "$ARIA2_CONF" ]; then
        echo -e "${YELLOW}>>> 保留模板中的 Aria2 设置。如需下载完成后自动扫描，请手动添加: on-download-complete=$TRIGGER_SCRIPT${NC}"

        ARIA2_SERVICE=""
        ARIA2_SERVICE_CREATED=0
        if systemctl cat aria2.service >/dev/null 2>&1; then
            if systemd_service_execstart_missing aria2; then
                echo -e "${YELLOW}⚠️ 已检测到失效的 aria2.service，正在改用当前 Aria2 二进制修复。${NC}"
            else
                ARIA2_SERVICE="aria2"
            fi
        fi
        if [ -z "$ARIA2_SERVICE" ] && systemctl cat aria2c.service >/dev/null 2>&1; then
            if systemd_service_execstart_missing aria2c; then
                echo -e "${YELLOW}⚠️ 已检测到失效的 aria2c.service，正在改用当前 Aria2 二进制修复。${NC}"
            else
                ARIA2_SERVICE="aria2c"
            fi
        fi
        if [ -z "$ARIA2_SERVICE" ]; then
            ARIA2_BIN=$(command -v aria2c)
            if [ -n "$ARIA2_BIN" ]; then
                echo -e "${YELLOW}>>> 未检测到 Aria2 systemd 服务，正在注册...${NC}"
                cat > "/etc/systemd/system/aria2.service" <<EOF
[Unit]
# Managed by Scanner
Description=Aria2 Download Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=$ARIA2_BIN --conf-path=$ARIA2_CONF --daemon=false
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
                systemctl daemon-reload
                if systemctl enable aria2; then
                    ARIA2_SERVICE="aria2"
                    ARIA2_SERVICE_CREATED=1
                    echo -e "${GREEN}✅ Aria2 systemd 服务已注册。${NC}"
                else
                    echo -e "${YELLOW}⚠️ Aria2 服务注册失败，请检查 systemctl 状态。${NC}"
                fi
            else
                echo -e "${YELLOW}⚠️ 未找到 aria2c，无法注册 Aria2 服务。${NC}"
            fi
        fi

        if [ -n "$ARIA2_SERVICE" ]; then
            if [ "$ARIA2_SERVICE_CREATED" -eq 1 ] && pgrep -x aria2c >/dev/null 2>&1; then
                echo -e "${YELLOW}⚠️ 检测到手工运行的 Aria2。服务已注册但未强制停止现有下载；请在空闲时执行 systemctl restart $ARIA2_SERVICE 使触发器生效。${NC}"
            elif systemctl restart "$ARIA2_SERVICE"; then
                echo -e "${GREEN}✅ Aria2 服务已重启。${NC}"
            else
                echo -e "${YELLOW}⚠️ 无法自动重启 Aria2，请检查: systemctl status $ARIA2_SERVICE${NC}"
            fi
        fi
    else
        echo -e "${YELLOW}⚠️ 未找到 Aria2 配置文件: $ARIA2_CONF${NC}"
        echo -e "   已安装 Aria2 二进制，但未创建或修改配置。"
    fi

    # 8. Systemd 服务
    echo -e "${GREEN}>>> [5/5] 注册服务...${NC}"
    cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Scanner Pro Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_ROOT
ExecStart=/usr/bin/python3 app.py
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$PROJECT_ROOT/scanner.env
StandardOutput=append:$PROJECT_ROOT/scanner.log
StandardError=append:$PROJECT_ROOT/scanner_error.log
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable $SERVICE_NAME
    systemctl restart $SERVICE_NAME

    INITIAL_CREDENTIALS_FILE="$PROJECT_ROOT/.initial_admin_credentials"
    for _ in {1..5}; do
        [ -s "$INITIAL_CREDENTIALS_FILE" ] && break
        sleep 1
    done
    if [ -s "$INITIAL_CREDENTIALS_FILE" ]; then
        INITIAL_ADMIN_USERNAME=$(sed -n 's/^username=//p' "$INITIAL_CREDENTIALS_FILE")
        INITIAL_ADMIN_PASSWORD=$(sed -n 's/^password=//p' "$INITIAL_CREDENTIALS_FILE")
        rm -f "$INITIAL_CREDENTIALS_FILE"
    else
        INITIAL_ADMIN_USERNAME=""
        INITIAL_ADMIN_PASSWORD=""
    fi

    NGINX_HTTPS_ENABLED=0
    NGINX_INSTALLED_BY_SCANNER=0
    NGINX_ARIA2_WSS_ENABLED=0
    if ! configure_nginx_https; then
        echo -e "${YELLOW}⚠️ Nginx HTTPS 未启用，Scanner 仍可使用直连地址访问。${NC}"
    fi
    printf 'SCANNER_NGINX_INSTALLED=%s\n' "$NGINX_INSTALLED_BY_SCANNER" >> "$PROJECT_ROOT/scanner.env"

    echo -e "------------------------------------------------"
    echo -e "${GREEN}🎉 安装成功！${NC}"
    if [ -n "$INITIAL_ADMIN_USERNAME" ] && [ -n "$INITIAL_ADMIN_PASSWORD" ]; then
        echo -e "初始账号: $INITIAL_ADMIN_USERNAME / $INITIAL_ADMIN_PASSWORD"
    else
        echo -e "登录账号: 已保留现有账户"
    fi
    echo -e "📂 安装路径: $PROJECT_ROOT"
    echo -e "🤖 本地 GGUF 模型: 登录设置页后在「模型」中一键下载/更新"
    echo -e "☁️ Rclone Remote: 请确保 rclone 已配置，并在设置页填写远程名称"
    if [ "$NGINX_HTTPS_ENABLED" -eq 1 ]; then
        NGINX_PUBLIC_URL="https://$NGINX_SERVER_NAME"
        NGINX_WSS_URL="wss://$NGINX_SERVER_NAME"
        NGINX_PUBLIC_URL+=":$NGINX_HTTPS_PORT"
        echo -e "📡 访问地址: $NGINX_PUBLIC_URL"
        if [ "$NGINX_ARIA2_WSS_ENABLED" -eq 1 ]; then
            ARIA2_RPC_SECRET=$(get_aria2_rpc_secret "$ARIA2_CONF")
            echo -e "📡 Aria2 HTTPS RPC: https://$NGINX_SERVER_NAME/jsonrpc (443)"
            echo -e "📡 Aria2 WSS RPC: $NGINX_WSS_URL/jsonrpc (443)"
            echo -e "🔑 Aria2 rpc-secret: $ARIA2_RPC_SECRET"
        fi
    elif [[ "$NET_CHOICE" =~ ^[Yy]$ ]]; then
        echo -e "📡 访问地址: http://127.0.0.1:$SCANNER_PORT (仅限本机)"
    else
        echo -e "📡 访问地址: http://<VPS_IP>:$SCANNER_PORT"
    fi
    echo -e "📡 服务状态: systemctl status $SERVICE_NAME"
    echo -e "------------------------------------------------"
}

function update_app() {
    local project_root

    echo -e "${CYAN}>>> 更新 Scanner Pro...${NC}"
    project_root=$(get_installed_project_root) || return
    echo -e "${YELLOW}>>> 更新目录: $project_root${NC}"
    echo -e "${YELLOW}>>> 保留数据库、scanner.env、Aria2 配置/rpc-secret、Nginx、模型和 AriaNg 数据。${NC}"

    echo -e "${GREEN}>>> [1/4] 检查系统依赖...${NC}"
    install_system_dependencies || return

    echo -e "${GREEN}>>> [2/4] 下载并更新代码...${NC}"
    deploy_project_archive "$project_root" || return
    chmod +x "$PROJECT_ROOT/trigger.sh"
    ensure_ariang_assets "$PROJECT_ROOT"

    echo -e "${GREEN}>>> [3/4] 更新 rclone...${NC}"
    if ! rclone selfupdate; then
        echo -e "${RED}❌ rclone selfupdate 失败。${NC}"
        return 1
    fi

    echo -e "${GREEN}>>> [4/4] 更新 Python 依赖并重启服务...${NC}"
    install_python_dependencies "$PROJECT_ROOT" || return
    if ! systemctl restart "$SERVICE_NAME"; then
        echo -e "${RED}❌ Scanner 重启失败，请检查: systemctl status $SERVICE_NAME${NC}"
        return 1
    fi
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo -e "${GREEN}✅ 更新完成，Scanner 正在运行。${NC}"
    else
        echo -e "${RED}❌ Scanner 更新后未处于运行状态。${NC}"
        return 1
    fi
}

function reset_dashboard_password() {
    local project_root reset_username

    project_root=""
    if [ -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
        project_root=$(grep "WorkingDirectory=" "/etc/systemd/system/$SERVICE_NAME.service" | cut -d= -f2)
    fi
    if [ -z "$project_root" ]; then project_root=$DEFAULT_INSTALL_DIR; fi
    if [ ! -f "$project_root/reset_password.py" ]; then
        echo -e "${RED}❌ 未找到密码重置工具: $project_root/reset_password.py${NC}"
        return
    fi

    read -r -p "要重置的 Dashboard 用户名（回车重置第一个账户）: " reset_username
    if [ -n "$reset_username" ]; then
        python3 "$project_root/reset_password.py" --username "$reset_username"
    else
        python3 "$project_root/reset_password.py"
    fi
}

# ================= 功能函数: 卸载 =================
function uninstall_app() {
    echo -e "${RED}>>> ⚠️  警告: 即将卸载 Scanner Pro Dashboard${NC}"

    INSTALLED_DIR=""
    if [ -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
        INSTALLED_DIR=$(grep "WorkingDirectory=" /etc/systemd/system/$SERVICE_NAME.service | cut -d= -f2)
    fi
    if [ -z "$INSTALLED_DIR" ]; then INSTALLED_DIR=$DEFAULT_INSTALL_DIR; fi

    TARGET_DIR="$INSTALLED_DIR"

    if [ -z "$TARGET_DIR" ] || [ ! -d "$TARGET_DIR" ]; then
        echo -e "${RED}❌ 已记录的 Scanner 安装目录不存在: $TARGET_DIR${NC}"
        return
    fi
    echo -e "${YELLOW}>>> 使用已记录的 Scanner 安装目录: $TARGET_DIR${NC}"

    echo -e "${YELLOW}正在停止服务...${NC}"
    systemctl stop $SERVICE_NAME 2>/dev/null
    systemctl disable $SERVICE_NAME 2>/dev/null
    rm -f "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload

    ARIA2_CONFIG_DIR="$DEFAULT_ARIA2_CONFIG_DIR"
    if [ -f "$TARGET_DIR/scanner.env" ]; then
        SAVED_ARIA2_CONFIG_DIR=$(awk -F= '/^SCANNER_ARIA2_CONFIG_DIR=/ { print $2 }' "$TARGET_DIR/scanner.env")
        if [[ "$SAVED_ARIA2_CONFIG_DIR" == /* ]] && [ "$SAVED_ARIA2_CONFIG_DIR" != "/" ]; then
            ARIA2_CONFIG_DIR="$SAVED_ARIA2_CONFIG_DIR"
        fi
    fi
    remove_scanner_aria2_config "$ARIA2_CONFIG_DIR"

    NGINX_SITE_EXISTS=0
    if [ -e "/etc/nginx/sites-available/scanner" ] || [ -L "/etc/nginx/sites-enabled/scanner" ]; then
        NGINX_SITE_EXISTS=1
    fi
    if command -v nginx >/dev/null 2>&1 && [ "$NGINX_SITE_EXISTS" -eq 1 ]; then
        read -p "是否移除 Scanner 的 Nginx HTTPS 站点与粘贴证书? 输入 y 确认: " REMOVE_NGINX_SITE
        if [[ "$REMOVE_NGINX_SITE" =~ ^[Yy]$ ]]; then
            rm -f "/etc/nginx/sites-enabled/scanner" "/etc/nginx/sites-available/scanner"
            rm -f "/etc/nginx/ssl/scanner.crt" "/etc/nginx/ssl/scanner.key"
            if nginx -t; then
                systemctl reload nginx 2>/dev/null || true
                echo "✅ Scanner Nginx HTTPS 站点已移除。"
                NGINX_SITE_EXISTS=0
            else
                echo -e "${YELLOW}⚠️ Nginx 配置校验失败，未重载服务；请检查其他 Nginx 站点。${NC}"
            fi
        fi
    fi

    NGINX_MANAGED_BY_SCANNER=""
    if [ -f "$TARGET_DIR/scanner.env" ]; then
        NGINX_MANAGED_BY_SCANNER=$(awk -F= '/^SCANNER_NGINX_INSTALLED=/ { print $2 }' "$TARGET_DIR/scanner.env")
    fi
    if [ "$NGINX_SITE_EXISTS" -eq 0 ] && [ "$NGINX_MANAGED_BY_SCANNER" = "1" ]; then
        read -p "Nginx 由 Scanner 安装，是否同时卸载 Nginx 环境? 输入 y 确认: " REMOVE_NGINX_PACKAGE
        if [[ "$REMOVE_NGINX_PACKAGE" =~ ^[Yy]$ ]]; then
            if nginx_has_other_sites; then
                echo -e "${YELLOW}⚠️ 检测到其他 Nginx 站点，保留 Nginx 环境。${NC}"
            else
                systemctl disable --now nginx 2>/dev/null || true
                apt-get purge --auto-remove -y nginx || echo -e "${YELLOW}⚠️ Nginx 卸载未完成，请手动检查。${NC}"
            fi
        fi
    fi

    echo -e "${YELLOW}删除文件...${NC}"
    rm -rf "$TARGET_DIR"

    echo -e "${GREEN}✅ 卸载完成。${NC}"
}

# ================= 主菜单 =================
while true; do
    echo -e "\n${CYAN}=== Scanner Pro 管理脚本 ===${NC}"
    echo "1. 安装 (Install)"
    echo "2. 更新 (Update)"
    echo "3. 卸载 (Uninstall)"
    echo "4. 重置 Dashboard 密码 (Reset Password)"
    echo "5. 退出 (Exit)"
    read -p "请输入选项 [1-5]: " choice

    case $choice in
        1) install_app; break ;;
        2) update_app; break ;;
        3) uninstall_app; break ;;
        4) reset_dashboard_password; break ;;
        5) exit 0 ;;
        *) echo -e "${RED}无效选项。${NC}" ;;
    esac
done
