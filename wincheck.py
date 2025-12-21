#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 广告秒杀工具 (全能完全体)
功能：
1. 🧹 元数据清洗：擦除标题/注释/轨道名中的广告。
2. 📝 字幕清洗：检查 SRT/ASS 字幕内容，有广告则移除字幕轨。
3. ☁️ 音频扫描：云端识别语音广告。
4. 📋 全量日志：显示识别文字，方便核查。
"""

import os
import sys
import subprocess
import re
import time
import hashlib
import shutil
import json
from urllib.parse import unquote

try:
    import requests
    from pypinyin import lazy_pinyin
    from thefuzz import fuzz
    from tqdm import tqdm
except ImportError:
    print("❌ 缺少依赖库，请运行: pip install requests pypinyin thefuzz tqdm")
    time.sleep(5)
    sys.exit(1)

# ================= ⚙️ 配置区域 =================
API_KEY = "sk-xxx"
API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
MODEL_NAME = "FunAudioLLM/SenseVoiceSmall"

SLICE_DURATION = 600
TEMP_DIR = os.path.join(os.getcwd(), "temp_scan")
SANITIZE_METADATA = True
CHECK_SUBTITLES = True  # 新增开关：是否检查字幕

# --- 黑名单配置 ---
BLACKLIST_KEYWORDS = [
    "加群", "交流群", "TG群", "Telegram", "QQ群", "Q群",
    "资源群", "福利群", "粉丝群", "看片",
    "微信号", "加微信", "微信群", "微信公众号", "关注公众号",
    "QQ号", "加Q", "加我V", "加V", "澳门", "威信", "VX", "http", "www"
]

META_BLACKLIST = [
    "微博", "Tacit0924", "tg", "qq", "q群", "微信", "公众号", "link3.cc", "ysepan.com", "GyWEB",
    "Qqun", "hehehe", ".com", "PTerWEB", "b站", "字幕组", "panclub", "by", "BT之家", "荣誉出品",
    "资源站", "资源网", "我堡牛皮", "发布页", "压制", "CMCT", "Byakuya", "ed3000", "整理", "yunpantv",
    "TG频道@", "KKYY", "盘酱酱", "TREX", "无人在意做自己", "£yhq@tv", "1000fr", "HDCTV", "HHWEB", "ADWeb", "PanWEB",
    "BestWEB"
]

GLOBAL_TAGS_TO_CHECK = ["genre", "comment", "description", "synopsis", "title", "artist", "album", "copyright"]
PINYIN_TARGETS = ["ziyuanqun", "tgqun", "jiaqun", "qqqun", "dianbaoqun", "fuliqun", "weixinqun"]
HOMOPHONE_MAP = {"踢踢": "TG", "听听": "TG", "提提": "TG", "扣扣": "QQ", "夫妻": "QQ", "几": "加", "薇": "微",
                 "V": "微"}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.ts', '.m4v', '.webm'}


# ================= 🛠️ 基础工具 =================

def log(msg, level="INFO"):
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")


def run_cmd(cmd_list, capture=True):
    try:
        if capture:
            return subprocess.run(cmd_list, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        else:
            return subprocess.run(cmd_list, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        return None


def verify_file_integrity(file_path):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024: return False
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'format=duration', '-of',
               'default=noprint_wrappers=1:nokey=1', file_path]
        res = run_cmd(cmd, capture=True)
        return res and res.stdout.strip() and float(res.stdout.strip()) > 0
    except:
        return False


def safe_replace(src, dst):
    try:
        if os.path.exists(dst): os.remove(dst)
        os.rename(src, dst)
        return True
    except:
        return False


# 🔥🔥🔥 新增函数：打印 ffmpeg -i 的原始输出 🔥🔥🔥
def print_ffmpeg_raw_info(file_path):
    print("\n" + "=" * 20 + " [FFmpeg Info] " + "=" * 20)
    try:
        # ffmpeg -i 不带输出文件通常会报错退出，这是正常的，信息在 stderr 中
        # 我们将 stderr 重定向到 stdout 以便显示
        subprocess.run(['ffmpeg', '-hide_banner', '-i', file_path],
                       stdout=subprocess.DEVNULL,
                       stderr=sys.stdout)
    except Exception as e:
        print(f"获取 FFmpeg 信息失败: {e}")
    print("=" * 20 + " [End Info] " + "=" * 20 + "\n")


def log_metadata_and_tracks(file_path):
    log("📋 读取轨道摘要...", "INFO")
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries',
               'stream=index,codec_type,codec_name:stream_tags=language,title', '-of', 'csv=p=0', file_path]
        res = run_cmd(cmd, capture=True)
        if res and res.stdout.strip():
            for line in res.stdout.strip().splitlines():
                print(f"   {line}")
    except:
        pass


# ================= 📝 字幕清洗模块 (新增) =================

def sanitize_subtitles(source):
    if not CHECK_SUBTITLES: return False

    dir_name = os.path.dirname(source)
    name, ext = os.path.splitext(os.path.basename(source))
    output_path = os.path.join(dir_name, f"{name}_clean_sub{ext}")

    hit_keyword = None

    try:
        # 1. 提取所有字幕内容 (text格式)
        # -map 0:s 只选择字幕流，-f srt 输出为 SRT 格式
        cmd_extract = ['ffmpeg', '-v', 'error', '-i', source, '-map', '0:s', '-f', 'srt', '-']
        res = run_cmd(cmd_extract, capture=True)

        if res and res.stdout:
            content = res.stdout.lower()
            for kw in BLACKLIST_KEYWORDS:  # 复用语音黑名单
                if kw.lower() in content:
                    hit_keyword = kw
                    break

        # 2. 如果发现敏感词，移除字幕轨道
        if hit_keyword:
            log(f"🚫 字幕中发现敏感词: '{hit_keyword}'", "WARN")
            log("🧹 正在移除敏感字幕轨...", "CLEAN")

            # -sn: 禁用字幕流 (-c copy 复制音视频)
            cmd_remove = [
                'ffmpeg', '-v', 'error', '-i', source,
                '-c', 'copy', '-sn',
                '-y', output_path
            ]
            run_cmd(cmd_remove, capture=False)

            if verify_file_integrity(output_path):
                if safe_replace(output_path, source):
                    log("✨ 字幕已移除，原文件已替换", "SUCCESS")
                    return True
            else:
                log("❌ 字幕移除失败，保留原文件", "ERR")
                if os.path.exists(output_path): os.remove(output_path)

    except Exception as e:
        log(f"字幕检查出错: {e}", "ERR")
        if os.path.exists(output_path): os.remove(output_path)

    return False


# ================= 🧹 元数据清洗模块 =================

def sanitize_metadata(source):
    if not SANITIZE_METADATA: return False
    clean_needed = False
    output_path = os.path.join(os.path.dirname(source), "temp_meta_clean.mp4")

    try:
        # 检查逻辑保持不变 (略微精简代码以节省篇幅)
        for tag in GLOBAL_TAGS_TO_CHECK:
            res = run_cmd(['ffprobe', '-v', 'error', '-show_entries', f'format_tags={tag}', '-of', 'csv=p=0', source])
            if res.stdout and any(k.lower() in res.stdout.lower() for k in META_BLACKLIST): clean_needed = True; break

        if not clean_needed:
            res = run_cmd(
                ['ffprobe', '-v', 'error', '-show_entries', 'stream=index:stream_tags=language,title', '-of', 'csv=p=0',
                 source])
            if res.stdout and any(k.lower() in res.stdout.lower() for k in META_BLACKLIST): clean_needed = True

        if clean_needed:
            log("🚫 发现脏元数据，正在清洗...", "CLEAN")
            cmd_nuclear = [
                'ffmpeg', '-err_detect', 'ignore_err', '-i', source,
                '-map', '0:v:0', '-map', '0:a?', '-map', '0:s?',
                '-c', 'copy', '-dn', '-ignore_unknown',
                '-map_metadata', '-1', '-metadata', 'title=', '-metadata', 'comment=', '-metadata:s', 'title=',
                '-y', output_path
            ]
            run_cmd(cmd_nuclear, capture=False)
            if verify_file_integrity(output_path):
                if safe_replace(output_path, source):
                    log("✨ 元数据已净化", "SUCCESS")
                    return True
            else:
                if os.path.exists(output_path): os.remove(output_path)
    except:
        pass
    return False


# ================= 🎙️ 音频处理与AI =================

def normalize_text(text):
    text = re.sub(r'<\|.*?\|>', '', text)
    trans = str.maketrans("零一二三四五六七八九", "0123456789")
    text = text.translate(trans)
    return re.sub(r'[^\w\s,.，。？！:：0-9a-zA-Z\u4e00-\u9fa5/\-_.\[\]\(\)]', '', text)


def check_spam_final(text):
    match = re.search(r'(资源|加群|入群|群号|QQ|TG|VX|微信).{0,12}\d{5,}', text, re.IGNORECASE)
    if match: return True, f"Regex_Match: [{match.group(0)}] (...{text[max(0, match.start() - 10):min(len(text), match.end() + 10)]}...)"
    for kw in BLACKLIST_KEYWORDS:
        if kw in text: return True, f"Keyword_{kw}"
    return False, None


def get_duration(source):
    res = run_cmd(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
         source])
    return float(res.stdout.strip()) if res and res.stdout.strip() else 0.0


def extract_audio_segment(input_source, start_time, duration, output_path):
    cmd = ['ffmpeg', '-v', 'error', '-ss', str(start_time), '-i', input_source, '-t', str(duration), '-vn', '-sn',
           '-map', '0:a:0', '-ac', '1', '-ar', '16000', '-af', 'highpass=f=200,lowpass=f=3000,loudnorm', '-b:a', '64k',
           '-f', 'mp3', '-y', output_path]
    run_cmd(cmd, capture=False)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 1024


def scan_audio_cloud(audio_path, time_offset):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        with open(audio_path, "rb") as f:
            response = requests.post(API_URL, headers=headers,
                                     files={"file": ("a.mp3", f, "audio/mpeg"), "model": (None, MODEL_NAME),
                                            "response_format": (None, "json"),
                                            "prompt": (None, "资源分享 QQ群 微信号 加群 70377")}, timeout=120)
            if response.status_code == 200:
                text = normalize_text(response.json().get("text", ""))
                log(f"💬 [{time.strftime('%H:%M:%S', time.gmtime(time_offset))}] {text}", "TEXT")
                is_spam, reason = check_spam_final(text)
                return is_spam, reason
            return False, f"HTTP {response.status_code}"
    except Exception as e:
        return False, str(e)


def download_url(url):
    if not os.path.exists(TEMP_DIR): os.makedirs(TEMP_DIR)
    filename = unquote(url.split("/")[-1].split("?")[0]) or f"dl_{int(time.time())}.mp4"
    local_path = os.path.join(TEMP_DIR, filename)
    log(f"正在下载: {url}", "NET")
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f, tqdm(total=int(r.headers.get('content-length', 0)), unit='iB',
                                                   unit_scale=True) as bar:
                for chunk in r.iter_content(chunk_size=8192): f.write(chunk); bar.update(len(chunk))
        return local_path
    except:
        return None


# ================= 🚀 主流程 =================

def process_file(file_path, is_temp=False):
    filename = os.path.basename(file_path)
    log(f"开始分析: {filename}", "START")

    # 🔥🔥 新增调用：打印 FFmpeg 原始信息 🔥🔥
    print_ffmpeg_raw_info(file_path)

    log_metadata_and_tracks(file_path)

    # 1. 清洗 (元数据 + 字幕)
    if sanitize_metadata(file_path): log("元数据清洗完成", "INFO")
    if sanitize_subtitles(file_path): log("字幕清洗完成", "INFO")

    # 2. 扫描音频
    duration = get_duration(file_path)
    if duration == 0: return

    tasks = []
    cursor = duration
    while cursor > 0:
        start = max(0, cursor - SLICE_DURATION)
        tasks.append({"start": start, "duration": cursor - start})
        cursor = start

    temp_wav = os.path.join(TEMP_DIR, f"scan_{hashlib.md5(file_path.encode()).hexdigest()[:8]}.mp3")
    hit, hit_reason = False, ""

    for i, task in enumerate(tasks):
        log(f"🔍 扫描分段 ({i + 1}/{len(tasks)}): {int(task['start'])}s -> {int(task['start'] + task['duration'])}s",
            "SCAN")
        if extract_audio_segment(file_path, task['start'], task['duration'], temp_wav):
            is_spam, reason = scan_audio_cloud(temp_wav, task['start'])
            if is_spam: hit = True; hit_reason = reason; log(f"🚨 发现广告: {reason}", "HIT"); break

    if os.path.exists(temp_wav): os.remove(temp_wav)

    # 3. 处置
    if hit:
        if is_temp:
            log("🗑️ 删除临时脏文件", "DEL"); os.remove(file_path)
        else:
            try:
                os.rename(file_path, os.path.join(os.path.dirname(file_path), "脏-" + filename)); log("已重命名",
                                                                                                      "RENAMED")
            except:
                log("重命名失败", "ERR")
    else:
        log("✅ 文件干净", "SAFE")
        if is_temp: os.remove(file_path)


def main():
    if not os.path.exists(TEMP_DIR): os.makedirs(TEMP_DIR)
    target = input("请输入 视频路径 / 文件夹 / HTTP链接: ").strip().strip('"')
    if target.startswith("http"):
        f = download_url(target)
        if f: process_file(f, is_temp=True)
    elif os.path.isdir(target):
        for r, d, f in os.walk(target):
            for file in f:
                if os.path.splitext(file)[1].lower() in VIDEO_EXTENSIONS: process_file(os.path.join(r, file))
    elif os.path.isfile(target):
        process_file(target)

    try:
        shutil.rmtree(TEMP_DIR)
    except:
        pass
    input("\n按回车键退出...")


if __name__ == "__main__":
    main()