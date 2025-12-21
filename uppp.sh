#!/usr/bin/env bash
#
# Version: 10.8 (Feature: Delete dirty files & Fix local log)
#

# ================= 🔧 核心配置 =================
PYTHON_ENV_PATH="/usr/bin/python3"
PYTHON_SCRIPT_PATH="/root/.aria2c/scan_audio.py"
PYTHON_LOCAL_SCRIPT_PATH="/root/.aria2c/scan_audio_local.py"

export TG_BOT_TOKEN="123:xxx"
export TG_CHAT_ID="1234"
# ===============================================

TASK_GID=$1
TASK_FILE_COUNT=$2
TASK_PATH=$3
CURRENT_FILE_NAME=""
LOCAL_PATH="$TASK_PATH"
CLEANED_FILE_FLAG=0

# 🔥 定义用于接收 Python 扫描结果的临时文件
export SCAN_REASON_FILE="/tmp/scan_reason_$$.txt"

log_message() {
    local level="$1"
    local message="$2"
    local clean_msg=$(echo -e "$message" | sed "s/\x1B\[[0-9;]*[a-zA-Z]//g")
    local prefix=""
    if [[ -n "$CURRENT_FILE_NAME" ]]; then prefix="[${CURRENT_FILE_NAME}] "; fi
    logger -t arup "$level: ${prefix}${clean_msg}"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$level] ${prefix}${clean_msg}"
}

SEND_TG_MSG() {
    local msg="$1"
    if [[ -n "$TG_BOT_TOKEN" && -n "$TG_CHAT_ID" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TG_CHAT_ID}" \
            -d text="$msg" >/dev/null
    fi
}

has_sensitive_subtitle() {
    local file_path="$1"
    local result
    result=$(ffprobe -v error -select_streams s -show_entries stream_tags=title,handler_name -of default=noprint_wrappers=1:nokey=1 "$file_path")
    if echo "$result" | grep -qE "GyWEB|www\.|.com|微信|加群|招募|公众号"; then
        log_message "WARN" "🚨 发现敏感字幕轨道"
        return 0
    fi
    return 1
}

remove_subtitle_track() {
    local input="$1"
    local dir_name=$(dirname "$input")
    local base_name=$(basename "$input")
    local ext="${base_name##*.}"
    local name="${base_name%.*}"
    local output="${dir_name}/${name}_clean.${ext}"

    ffmpeg -y -i "$input" -map 0 -map -0:s -c copy "$output" >/dev/null 2>&1
    if [ $? -eq 0 ] && [ -s "$output" ]; then
        echo "$output"
        return 0
    else
        return 1
    fi
}

audio_ad_check_and_act() {
    local target_file="$1"

    echo "" > "$SCAN_REASON_FILE"

    # ---------------- Step 1: Cloud Scan ----------------
    $PYTHON_ENV_PATH -u "$PYTHON_SCRIPT_PATH" "$target_file" 2>&1 | \
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if echo "$line" | grep -qE "FATAL.*🚫"; then continue; fi
        log_message "INFO" "[PY] $line"
    done

    local exit_code_cloud=${PIPESTATUS[0]}

    if [ $exit_code_cloud -eq 1 ]; then
        local reason="未知原因"
        if [ -s "$SCAN_REASON_FILE" ]; then reason=$(cat "$SCAN_REASON_FILE"); fi

        log_message "WARN" "⛔ [Cloud] 拦截到脏文件: $reason"
        SEND_TG_MSG "🚫 [Cloud] 发现违规音频: ${CURRENT_FILE_NAME}%0A--------------------------------%0A🔍 原因: ${reason}"
        return 1

    elif [ $exit_code_cloud -eq 0 ]; then
        return 0
    else
        log_message "WARN" "⚠️ [Cloud] 异常 (Code: $exit_code_cloud)，切换本地..."
    fi

    # ---------------- Step 2: Local Fallback ----------------
    if [ ! -f "$PYTHON_LOCAL_SCRIPT_PATH" ]; then
         log_message "ERROR" "❌ 本地脚本缺失"
         return 2
    fi

    log_message "INFO" "🔄 启动本地模型扫描"

    $PYTHON_ENV_PATH -u "$PYTHON_LOCAL_SCRIPT_PATH" "$target_file" 2>&1 | \
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if echo "$line" | grep -qE "FATAL.*🚫"; then continue; fi
        log_message "INFO" "[Local] $line"
    done

    local exit_code_local=${PIPESTATUS[0]}

    if [ $exit_code_local -eq 1 ]; then
        local reason="未知原因"
        if [ -s "$SCAN_REASON_FILE" ]; then reason=$(cat "$SCAN_REASON_FILE"); fi

        log_message "WARN" "⛔ [Local] 拦截到脏文件: $reason"
        SEND_TG_MSG "🚫 [Local] 发现违规音频: ${CURRENT_FILE_NAME}%0A--------------------------------%0A🔍 原因: ${reason}"
        return 1

    elif [ $exit_code_local -eq 0 ]; then
        return 0
    else
        log_message "ERROR" "❌ [Fatal] 双重扫描失败"
        SEND_TG_MSG "⚠️ [扫描异常] 跳过文件: ${CURRENT_FILE_NAME}"
        return 2
    fi
}

# ================= 主流程 =================
if [ "$TASK_FILE_COUNT" -eq 1 ]; then
    CURRENT_FILE_NAME=$(basename "$LOCAL_PATH")

    trap 'rm -f "$SCAN_REASON_FILE"' EXIT

    if echo "$CURRENT_FILE_NAME" | grep -qE "\.(mp4|mkv|avi|mov|flv|wmv|ts|m4v|webm)$"; then

        if has_sensitive_subtitle "$LOCAL_PATH"; then
            clean_file=$(remove_subtitle_track "$LOCAL_PATH")
            if [ $? -eq 0 ] && [ -n "$clean_file" ]; then
                rm -f "$LOCAL_PATH"
                LOCAL_PATH="$clean_file"
                CURRENT_FILE_NAME=$(basename "$LOCAL_PATH")
                CLEANED_FILE_FLAG=1
                log_message "INFO" "✅ 字幕已移除，新文件: ${CURRENT_FILE_NAME}"
            fi
        fi

        audio_ad_check_and_act "$LOCAL_PATH"
        # 🔥 修改点：检测失败后，执行删除操作
        if [ $? -ne 0 ]; then
            log_message "WARN" "⚠️ 扫描未通过，删除文件并停止上传"
            rm -f "$LOCAL_PATH"
            # 如果是清洗过的文件，原文件已经在清洗步骤被替换或删除了，这里再次确保清理
            exit 1
        fi
    fi

    # 上传
    REMOTE_PATH="s25:${CURRENT_FILE_NAME}"
    RETRY=0; RETRY_NUM=3
    while [ ${RETRY} -le ${RETRY_NUM} ]; do
        rclone moveto -v "$LOCAL_PATH" "$REMOTE_PATH" --ignore-size
        if [ $? -eq 0 ]; then
            log_message "INFO" "✅ 上传成功"
            break
        else
            RETRY=$((RETRY+1))
            log_message "ERROR" "上传重试 $RETRY..."
            sleep 3
        fi
    done

    rmdir "$TASK_PATH" 2>/dev/null
fi