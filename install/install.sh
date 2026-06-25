#!/bin/bash

# ================= 默认配置 =================
ZIP_FILE="main.zip"
DEFAULT_INSTALL_DIR="/www/wwwroot/scanner_web"
DEFAULT_ARIA2_CONF="/root/.aria2c/aria2.conf"
SERVICE_NAME="scanner"
# ===========================================

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# 检查 Root 权限
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}❌ 请使用 root 权限运行此脚本。${NC}"
  exit 1
fi

# ================= 功能函数: 安装 =================
function install_app() {
    echo -e "${CYAN}>>> 进入安装流程...${NC}"

    # 1. 检查压缩包
    if [ ! -f "$ZIP_FILE" ]; then
        echo -e "${RED}❌ 错误: 当前目录下未找到 $ZIP_FILE${NC}"
        return
    fi

    # 2. 交互配置 (路径)
    read -p "请输入安装目录 (回车默认 $DEFAULT_INSTALL_DIR): " USER_DIR
    INSTALL_BASE=${USER_DIR:-$DEFAULT_INSTALL_DIR}
    INSTALL_BASE=${INSTALL_BASE%/} # 去除末尾斜杠

    read -p "请输入 Aria2 配置文件路径 (回车默认 $DEFAULT_ARIA2_CONF): " USER_CONF
    ARIA2_CONF=${USER_CONF:-$DEFAULT_ARIA2_CONF}

    # 3. 系统依赖
    echo -e "${GREEN}>>> [1/5] 安装/检查系统依赖...${NC}"
    apt-get update -qq
    apt-get install -y \
        ffmpeg python3 python3-pip unzip ca-certificates libsndfile1 net-tools \
        git cmake build-essential rclone aria2 > /dev/null

    for REQUIRED_CMD in ffmpeg ffprobe python3 pip3 git cmake rclone; do
        if ! command -v "$REQUIRED_CMD" >/dev/null 2>&1; then
            echo -e "${YELLOW}⚠️  未检测到 $REQUIRED_CMD，请确认系统依赖安装是否成功。${NC}"
        fi
    done

    # 4. 解压与铺平
    echo -e "${GREEN}>>> [2/5] 解压并部署文件...${NC}"
    if [ -d "$INSTALL_BASE" ]; then
        echo -e "${YELLOW}⚠️  目录 $INSTALL_BASE 已存在，正在覆盖...${NC}"
    fi
    mkdir -p "$INSTALL_BASE"
    unzip -q -o "$ZIP_FILE" -d "$INSTALL_BASE"

    # --- 自动铺平逻辑 ---
    DETECTED_APP=$(find "$INSTALL_BASE" -name "app.py" | head -n 1)
    if [ -z "$DETECTED_APP" ]; then
        echo -e "${RED}❌ 错误: 找不到 app.py，请检查压缩包。${NC}"
        return
    fi
    DETECTED_DIR=$(dirname "$DETECTED_APP")

    if [ "$DETECTED_DIR" != "$INSTALL_BASE" ]; then
        echo -e "${YELLOW}>>> 检测到多层目录，正在铺平...${NC}"
        mv "$DETECTED_DIR"/* "$INSTALL_BASE/" 2>/dev/null
        mv "$DETECTED_DIR"/.* "$INSTALL_BASE/" 2>/dev/null
        rmdir "$DETECTED_DIR" 2>/dev/null
        PROJECT_ROOT="$INSTALL_BASE"
    else
        PROJECT_ROOT="$DETECTED_DIR"
    fi
    echo -e "${YELLOW}>>> 项目根目录确认: $PROJECT_ROOT${NC}"

    # 5. 配置监听地址
    echo -e "${GREEN}>>> [Extra] 配置网络监听...${NC}"
    read -p "是否仅允许本机访问 (127.0.0.1)? 输入 y 确认，回车默认开放外网 (0.0.0.0): " NET_CHOICE
    APP_PY_PATH="$PROJECT_ROOT/app.py"

    if [[ "$NET_CHOICE" =~ ^[Yy]$ ]]; then
        sed -i "s/0.0.0.0/127.0.0.1/g" "$APP_PY_PATH"
        echo -e "${YELLOW}🔒 已设置为仅本机访问。${NC}"
    else
        sed -i "s/127.0.0.1/0.0.0.0/g" "$APP_PY_PATH"
        echo -e "${YELLOW}🌍 已设置为开放外网访问。${NC}"
    fi

    # 6. Python 依赖 (当前版本使用 GGUF/llama.cpp，本地 ASR 不再依赖 Torch/FunASR)
    echo -e "${GREEN}>>> [3/5] 安装 Python 依赖...${NC}"
    if [ -f "$PROJECT_ROOT/requirements.txt" ]; then

        # 准备参数: 检测是否需要 --break-system-packages
        PIP_XARGS=""
        if pip3 install --help | grep -q "break-system-packages"; then
            echo -e "${YELLOW}>>> 启用 PEP 668 系统保护绕过模式...${NC}"
            PIP_XARGS="--break-system-packages"
        fi

        # === 关键修复步骤 Start ===
        # 1. 单独强制重装 blinker (解决 RECORD file not found 错误)
        echo -e "${CYAN}正在修复系统包冲突 (blinker)...${NC}"
        pip3 install blinker --ignore-installed $PIP_XARGS > /dev/null 2>&1

        # 2. 正常安装轻量 Web/API 依赖
        echo -e "${CYAN}正在安装/检查其余依赖...${NC}"
        pip3 install -r "$PROJECT_ROOT/requirements.txt" $PIP_XARGS
        # === 关键修复步骤 End ===

    else
        echo -e "${YELLOW}⚠️ 未找到 requirements.txt，跳过。${NC}"
    fi

    # 7. Aria2 配置
    echo -e "${GREEN}>>> [4/5] 注入 Aria2 触发器...${NC}"
    TRIGGER_SCRIPT="$PROJECT_ROOT/trigger.sh"
    chmod +x "$TRIGGER_SCRIPT"

    if [ -f "$ARIA2_CONF" ]; then
        sed -i '/on-download-complete=/d' "$ARIA2_CONF"
        sed -i '/# \[Auto Inject\] Scanner Pro/d' "$ARIA2_CONF"

        echo "" >> "$ARIA2_CONF"
        echo "# [Auto Inject] Scanner Pro Trigger" >> "$ARIA2_CONF"
        echo "on-download-complete=$TRIGGER_SCRIPT" >> "$ARIA2_CONF"

        echo -e "${GREEN}✅ Aria2 配置已更新，尝试重启...${NC}"
        systemctl restart aria2 2>/dev/null || systemctl restart aria2c 2>/dev/null || echo -e "${YELLOW}⚠️ 无法自动重启 Aria2，请手动重启。${NC}"
    else
        echo -e "${RED}❌ 未找到文件: $ARIA2_CONF${NC}"
        echo -e "   请手动添加: on-download-complete=$TRIGGER_SCRIPT"
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

    echo -e "------------------------------------------------"
    echo -e "${GREEN}🎉 安装成功！${NC}"
    echo -e "默认账号: admin / admin123"
    echo -e "📂 安装路径: $PROJECT_ROOT"
    echo -e "🤖 本地 GGUF 模型: 登录设置页后在「模型」中一键下载/更新"
    echo -e "☁️ Rclone Remote: 请确保 rclone 已配置，并在设置页填写远程名称"
    if [[ "$NET_CHOICE" =~ ^[Yy]$ ]]; then
        echo -e "📡 访问地址: http://127.0.0.1:5000 (仅限本机)"
    else
        echo -e "📡 访问地址: http://<VPS_IP>:5000"
    fi
    echo -e "📡 服务状态: systemctl status $SERVICE_NAME"
    echo -e "------------------------------------------------"
}

# ================= 功能函数: 卸载 =================
function uninstall_app() {
    echo -e "${RED}>>> ⚠️  警告: 即将卸载 Scanner Pro Dashboard${NC}"

    INSTALLED_DIR=""
    if [ -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
        INSTALLED_DIR=$(grep "WorkingDirectory=" /etc/systemd/system/$SERVICE_NAME.service | cut -d= -f2)
    fi
    if [ -z "$INSTALLED_DIR" ]; then INSTALLED_DIR=$DEFAULT_INSTALL_DIR; fi

    read -p "确认要删除的目录 [默认为: $INSTALLED_DIR]: " USER_INPUT_DIR
    TARGET_DIR=${USER_INPUT_DIR:-$INSTALLED_DIR}

    if [ -z "$TARGET_DIR" ] || [ ! -d "$TARGET_DIR" ]; then
        echo -e "${RED}❌ 目录不存在，取消。${NC}"
        return
    fi

    read -p "Aria2 配置文件路径 [默认 $DEFAULT_ARIA2_CONF]: " USER_CONF
    CLEAN_CONF=${USER_CONF:-$DEFAULT_ARIA2_CONF}

    echo -e "${YELLOW}正在停止服务...${NC}"
    systemctl stop $SERVICE_NAME 2>/dev/null
    systemctl disable $SERVICE_NAME 2>/dev/null
    rm -f "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload

    echo -e "${YELLOW}正在清理 Aria2 配置...${NC}"
    if [ -f "$CLEAN_CONF" ]; then
        sed -i '/trigger.sh/d' "$CLEAN_CONF"
        sed -i '/Scanner Pro Trigger/d' "$CLEAN_CONF"
        echo "✅ Aria2 配置已清理。"
        systemctl restart aria2 2>/dev/null || systemctl restart aria2c 2>/dev/null
    fi

    echo -e "${YELLOW}删除文件...${NC}"
    rm -rf "$TARGET_DIR"

    echo -e "${GREEN}✅ 卸载完成。${NC}"
}

# ================= 主菜单 =================
while true; do
    echo -e "\n${CYAN}=== Scanner Pro 管理脚本 ===${NC}"
    echo "1. 安装 (Install)"
    echo "2. 卸载 (Uninstall)"
    echo "3. 退出 (Exit)"
    read -p "请输入选项 [1-3]: " choice

    case $choice in
        1) install_app; break ;;
        2) uninstall_app; break ;;
        3) exit 0 ;;
        *) echo -e "${RED}无效选项。${NC}" ;;
    esac
done
