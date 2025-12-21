#!/usr/bin/env bash
#
# Manual Scanner Tool (本地手动扫描工具)
# 用法: ./manual_scan.sh <文件或文件夹路径>
#

# ================= 🔧 配置 (保持与 uppp.sh 一致) =================
PYTHON_ENV_PATH="/usr/bin/python3"
PYTHON_SCRIPT_PATH="/root/.aria2c/scan_audio.py"
export TG_BOT_TOKEN="123:xxx"
export TG_CHAT_ID="1234"
# ===============================================================

# 获取输入参数
TARGET_PATH="$1"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

if [ -z "$TARGET_PATH" ]; then
    echo -e "${RED}❌ 用法错误: 请指定要扫描的文件或目录${NC}"
    echo "示例: ./manual_scan.sh /home/downloads/video.mp4"
    echo "示例: ./manual_scan.sh /home/downloads/movies/"
    exit 1
fi

# 日志函数 (输出到屏幕)
log() {
    local level="$1"
    local msg="$2"
    echo -e "$(date '+%H:%M:%S') [${level}] ${msg}"
}

# TG 通知函数
send_tg() {
    local msg="$1"
    if [[ -n "$TG_BOT_TOKEN" && -n "$TG_CHAT_ID" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TG_CHAT_ID}" \
            -d parse_mode="HTML" \
            --data-urlencode text="${msg}" >/dev/null
    fi
}

# 判断是否为视频
is_video() {
    local f="$1"
    local ext="${f##*.}"
    ext="${ext,,}"
    case "$ext" in
        mp4|mkv|avi|mov|flv|wmv|ts|m4v|webm) return 0 ;;
        *) return 1 ;;
    esac
}

# 核心扫描函数 (从 uppp.sh 移植并精简)
scan_single_file() {
    local file_path="$1"
    local file_name=$(basename "$file_path")

    # 使用 PID 作为随机后缀，防止日志冲突
    local run_log="/tmp/manual_scan_${$}_${RANDOM}.log"

    if ! is_video "$file_path"; then
        return
    fi

    log "INFO" "${BLUE}>>> 开始扫描: ${file_name}${NC}"

    # 🔥 1. 并发排队逻辑 (最大 2)
    while true; do
        current_jobs=$(pgrep -c -f "scan_audio.py")
        if [ "$current_jobs" -ge 2 ]; then
            echo -ne "\r${YELLOW}🚦 队列已满 ($current_jobs/2)，等待中...${NC}"
            sleep 5
        else
            echo -e "" # 换行
            break
        fi
    done

    # 🔥 2. 调用 Python
    "$PYTHON_ENV_PATH" -u "$PYTHON_SCRIPT_PATH" "$file_path" > "$run_log" 2>&1
    local exit_code=$?

    # 🔥 3. 结果处理
    if [ "$exit_code" -ne 0 ]; then

        # 🚨 发现广告 (Exit 1)
        if grep -q "RENAMED:" "$run_log"; then
            local dirty_file=$(grep "RENAMED:" "$run_log" | head -n 1 | awk -F "RENAMED: " '{print $2}' | tr -d '\r')
            local rule=$(grep "规则:" "$run_log" | head -n 1 | awk -F "规则: " '{print $2}' | tr -d '\r')

            log "WARN" "${RED}⛔ 拦截到广告: $rule${NC}"

            send_tg "🚨 <b>手动扫描拦截</b> 🚨
--------------------
📁 <b>文件:</b> ${file_name}
🔑 <b>规则:</b> ${rule}
🗑️ <b>动作:</b> 自行删除蛤！"

#            if [ -f "$dirty_file" ]; then
#                rm -f "$dirty_file"
#                log "INFO" "🗑️ 文件已删除"
#            fi

        # ⚠️ API 故障 (Exit 2)
        else
            local err=$(grep "❌" "$run_log" | tail -n 1 | sed 's/.*❌ //')
            log "ERROR" "${RED}🚫 分析失败: $err${NC}"

            send_tg "⚠️ <b>手动扫描失败</b> ⚠️
--------------------
📁 <b>文件:</b> ${file_name}
❌ <b>错误:</b> ${err}
🛑 <b>动作:</b> 跳过"
        fi
    else
        log "INFO" "${GREEN}✅ 扫描安全${NC}"
    fi

    rm -f "$run_log"
}

# ================= 主流程 =================

if [ -f "$TARGET_PATH" ]; then
    # 单文件模式
    scan_single_file "$TARGET_PATH"
elif [ -d "$TARGET_PATH" ]; then
    # 目录模式：遍历查找视频文件
    log "INFO" "正在遍历目录: $TARGET_PATH"
    # 使用 find 查找所有视频文件，并逐个处理
    find "$TARGET_PATH" -type f \( -iname "*.mp4" -o -iname "*.mkv" -o -iname "*.avi" -o -iname "*.mov" -o -iname "*.ts" \) | while read -r file; do
        scan_single_file "$file"
    done
else
    echo -e "${RED}❌ 路径不存在: $TARGET_PATH${NC}"
fi