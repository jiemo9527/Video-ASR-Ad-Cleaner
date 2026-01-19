#!/bin/bash

# 获取脚本所在的目录
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

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

# 2. 执行回调 (这部分保持不变)
TASK_PATH="$3"

if [ -f "$TASK_PATH" ]; then
    curl -s -X POST "http://127.0.0.1:5000/api/trigger" \
         -H "Content-Type: application/json" \
         -H "X-API-Token: $TOKEN" \
         -d "{\"path\": \"$TASK_PATH\"}"
fi