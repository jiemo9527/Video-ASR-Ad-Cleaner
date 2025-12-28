#!/usr/bin/env bash
#
# Version: 15.3 (Fix: Default to s25 for root folder files)
# ================= 🔧 核心配置 =================
PYTHON_ENV_PATH="/usr/bin/python3"
PYTHON_SCRIPT_PATH="/root/.aria2c/scan_audio.py"
PYTHON_LOCAL_SCRIPT_PATH="/root/.aria2c/scan_audio_local.py"

# ⚠️ 填入你的 Token
export TG_BOT_TOKEN="TG_BOT_TOKEN"
export TG_CHAT_ID="TG_CHAT_ID"
# ===============================================

TASK_GID=$1
TASK_FILE_COUNT=$2
TASK_PATH=$3
CURRENT_FILE_NAME=""
LOCAL_PATH="$TASK_PATH"
export SCAN_REASON_FILE="/tmp/scan_reason_$$.txt"
export RCLONE_LOG_FILE="/tmp/rclone_error_$$.log"

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
            -d chat_id="${TG_CHAT_ID}" -d text="$msg" >/dev/null
    fi
}

has_sensitive_subtitle() {
    local result=$(ffprobe -v error -select_streams s -show_entries stream_tags=title,handler_name -of default=noprint_wrappers=1:nokey=1 "$1")
    if echo "$result" | grep -qE "GyWEB|www\.|.com|微信|加群|招募|公众号"; then
        log_message "WARN" "🚨 发现敏感字幕轨道"
        return 0
    fi
    return 1
}

remove_subtitle_track() {
    local input="$1"
    local ext="${input##*.}"
    local output="${input%.*}_clean.${ext}"
    ffmpeg -y -i "$input" -map 0 -map -0:s -c copy "$output" >/dev/null 2>&1
    if [ $? -eq 0 ] && [ -s "$output" ]; then echo "$output"; return 0; else return 1; fi
}

audio_ad_check_and_act() {
    local target_file="$1"
    echo "" > "$SCAN_REASON_FILE"

    # Step 1: Cloud Scan
    $PYTHON_ENV_PATH -u "$PYTHON_SCRIPT_PATH" "$target_file" 2>&1 | \
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if echo "$line" | grep -qE "FATAL.*🚫"; then continue; fi
        log_message "INFO" "[PY] $line"
    done
    local code_cloud=${PIPESTATUS[0]}

    if [ $code_cloud -eq 1 ]; then
        local reason=$(cat "$SCAN_REASON_FILE")
        log_message "WARN" "⛔ [Cloud] 拦截到脏文件: $reason"
        SEND_TG_MSG "🚫 [Cloud] 发现违规音频: ${CURRENT_FILE_NAME}%0A原因是: ${reason}"
        return 1
    elif [ $code_cloud -eq 0 ]; then
        return 0
    else
        log_message "WARN" "⚠️ [Cloud] 异常 (Code: $code_cloud)，切换本地..."
    fi

    # Step 2: Local Fallback
    if [ ! -f "$PYTHON_LOCAL_SCRIPT_PATH" ]; then return 2; fi
    log_message "INFO" "🔄 启动本地模型扫描"

    # 缓冲：给系统 3 秒钟回收内存
    sync && echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
    sleep 3

    $PYTHON_ENV_PATH -u "$PYTHON_LOCAL_SCRIPT_PATH" "$target_file" 2>&1 | \
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if echo "$line" | grep -qE "FATAL.*🚫"; then continue; fi
        log_message "INFO" "[Local] $line"
    done
    local code_local=${PIPESTATUS[0]}

    if [ $code_local -eq 1 ]; then
        local reason=$(cat "$SCAN_REASON_FILE")
        log_message "WARN" "⛔ [Local] 拦截到脏文件: $reason"
        SEND_TG_MSG "🚫 [Local] 发现违规音频: ${CURRENT_FILE_NAME}%0A原因是: ${reason}"
        return 1
    elif [ $code_local -eq 0 ]; then
        return 0
    else
        log_message "ERROR" "❌ [Local] 扫描脚本出错 (Code: $code_local)"
        SEND_TG_MSG "⚠️ [扫描异常] 跳过文件: ${CURRENT_FILE_NAME}"
        return 2
    fi
}

if [ "$TASK_FILE_COUNT" -eq 1 ]; then
    CURRENT_FILE_NAME=$(basename "$LOCAL_PATH")
    trap 'rm -f "$SCAN_REASON_FILE" "$RCLONE_LOG_FILE"' EXIT

    if echo "$CURRENT_FILE_NAME" | grep -qE "\.(mp4|mkv|avi|mov|flv|wmv|ts|m4v|webm)$"; then
        if has_sensitive_subtitle "$LOCAL_PATH"; then
            clean_file=$(remove_subtitle_track "$LOCAL_PATH")
            if [ $? -eq 0 ] && [ -n "$clean_file" ]; then
                rm -f "$LOCAL_PATH"
                LOCAL_PATH="$clean_file"
                CURRENT_FILE_NAME=$(basename "$LOCAL_PATH")
                log_message "INFO" "✅ [Shell] 字幕已移除，更新路径: ${CURRENT_FILE_NAME}"
            fi
        fi

        audio_ad_check_and_act "$LOCAL_PATH"
        EXIT_CODE=$?

        if [ $EXIT_CODE -eq 1 ]; then
            log_message "WARN" "🚨 判定为违规文件，执行删除!"
            rm -f "$LOCAL_PATH"
            f_base="${LOCAL_PATH%.*}"
            f_base_clean=$(echo "$f_base" | sed 's/_clean$//')
            rm -f "${f_base_clean}_clean.${LOCAL_PATH##*.}"
            exit 1
        elif [ $EXIT_CODE -ne 0 ]; then
            log_message "ERROR" "⚠️ 检测脚本发生系统错误 (Code: $EXIT_CODE)，保留文件但不上传。"
            exit 1
        fi

        # Sync Logic
        DIR=$(dirname "$LOCAL_PATH")
        NAME=$(basename "$LOCAL_PATH" | sed 's/\.[^.]*$//')
        EXT="${LOCAL_PATH##*.}"
        CLEAN_PATH="${DIR}/${NAME}_clean.${EXT}"

        if [ ! -f "$LOCAL_PATH" ] && [ -f "$CLEAN_PATH" ]; then
            LOCAL_PATH="$CLEAN_PATH"
            CURRENT_FILE_NAME=$(basename "$LOCAL_PATH")

        elif [ -f "$CLEAN_PATH" ]; then
            LOCAL_PATH="$CLEAN_PATH"
            CURRENT_FILE_NAME=$(basename "$LOCAL_PATH")
            log_message "INFO" "🔄 [Sync] 发现净化文件，优先使用"
        fi
    fi

    # ================= 🚀 Upload 逻辑 (智能兜底版) =================

    ORIGIN_DIR=$(dirname "$TASK_PATH")
    PARENT_FOLDER_NAME=$(basename "$ORIGIN_DIR")

    # 🔥🔥🔥 判定逻辑修正 🔥🔥🔥
    # 如果父目录是 downloads (说明在根目录)，则默认去 s25
    if [[ "$PARENT_FOLDER_NAME" == "downloads" ]]; then
        RCLONE_REMOTE="s25"
        log_message "INFO" "📂 检测到文件位于根目录，默认目标: [s25]"
    else
        # 否则使用父目录名作为 Remote Name
        RCLONE_REMOTE="$PARENT_FOLDER_NAME"
        log_message "INFO" "📂 匹配父目录[${RCLONE_REMOTE}]"
    fi

    REMOTE_PATH="${RCLONE_REMOTE}:${CURRENT_FILE_NAME}"

    if [ -f "$LOCAL_PATH" ]; then
        FILE_SIZE=$(ls -lh "$LOCAL_PATH" | awk '{print $5}')
        log_message "INFO" "📊 准备上传(大小: $FILE_SIZE)"
    else
        log_message "ERROR" "❌ 致命错误: 要上传的文件不存在! ($LOCAL_PATH)"
        exit 1
    fi

    sync && echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
    sleep 2

    RETRY=0
    while [ ${RETRY} -le 3 ]; do
        # 保留 log-level ERROR, 移除 -v
        rclone moveto "$LOCAL_PATH" "$REMOTE_PATH" --ignore-size --log-file="$RCLONE_LOG_FILE" --log-level ERROR

        if [ $? -eq 0 ]; then
            log_message "INFO" "✅ 上传成功"
            break
        fi

        if [ ${RETRY} -ge 3 ]; then
            if [ -f "$RCLONE_LOG_FILE" ]; then
                RCLONE_ERR=$(tail -n 3 "$RCLONE_LOG_FILE")
                log_message "ERROR" "❌ 上传失败! Rclone 报错: ${RCLONE_ERR}"
            else
                log_message "ERROR" "❌ 上传失败 (无日志生成)"
            fi
            break
        fi

        RETRY=$((RETRY+1))
        log_message "WARN" "上传失败，等待 3s 后重试 ($RETRY / 3) ..."
        sleep 3
    done

    rmdir "$TASK_PATH" 2>/dev/null
    if [ "$LOCAL_PATH" != "$TASK_PATH" ]; then rmdir "$(dirname "$TASK_PATH")" 2>/dev/null; fi
fi