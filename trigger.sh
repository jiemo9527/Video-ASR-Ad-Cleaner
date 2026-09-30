#!/bin/bash

# 获取脚本所在的目录
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 安装器写入此文件，使 Aria2 回调与 Scanner 服务使用同一监听端口。
RUNTIME_ENV_FILE="$DIR/scanner.env"
if [ -r "$RUNTIME_ENV_FILE" ]; then
    . "$RUNTIME_ENV_FILE"
fi
SCANNER_PORT="${SCANNER_PORT:-5000}"

# 定义密钥文件路径 (与 app.py 中定义的路径一致)
TOKEN_FILE="$DIR/.token_secret"

# 1. 尝试读取文件中的 Token
if [ -f "$TOKEN_FILE" ]; then
    # 读取内容并去除可能存在的空格/换行符
    TOKEN=$(cat "$TOKEN_FILE" | tr -d '[:space:]')
else
    # 如果文件不存在 (比如系统刚初始化)，使用一个默认值或旧值
    TOKEN="8pUoqOTHhEAhRnacl3c19"
fi

# 2. 执行回调
TASK_GID="$1"
TASK_FILE_COUNT="$2"
TASK_PATH="$3"

if [ -e "$TASK_PATH" ]; then
    PAYLOAD=$(python3 - "$TASK_GID" "$TASK_FILE_COUNT" "$TASK_PATH" <<'PY'
import json
import sys

print(json.dumps({"gid": sys.argv[1], "file_count": sys.argv[2], "path": sys.argv[3]}))
PY
)
    if ! curl --silent --show-error --fail --retry 5 --retry-delay 2 --retry-all-errors \
        --connect-timeout 5 --max-time 30 -X POST "http://127.0.0.1:$SCANNER_PORT/api/trigger" \
        -H "Content-Type: application/json" -H "X-API-Token: $TOKEN" -d "$PAYLOAD"; then
        logger -t scanner-trigger "Aria2 callback failed after retries: gid=$TASK_GID path=$TASK_PATH"
    fi
else
    logger -t scanner-trigger "Aria2 callback skipped missing path: gid=$TASK_GID path=$TASK_PATH"
fi
