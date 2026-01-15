#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 广告秒杀工具 (全能完全体 - 智能音轨版 - 多模式切换 - 全文日志)
功能：
1. 🧹 元数据清洗：擦除标题/注释/轨道名中的广告。
2. 📝 字幕清洗：检查 SRT/ASS 字幕内容，有广告则移除字幕轨。
3. ☁️/🤖 音频扫描：支持 云端优先/纯本地/纯云端 三种模式。
4. 📋 全量日志：显示完整识别文字，不再截断。
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
from contextlib import contextmanager

try:
    import requests
    from pypinyin import lazy_pinyin
    from thefuzz import fuzz
    from tqdm import tqdm
except ImportError:
    print("❌ 缺少基础依赖库，请运行: pip install requests pypinyin thefuzz tqdm")
    time.sleep(5)
    sys.exit(1)

# ================= ⚙️ 配置区域 =================
API_KEY = "sk-abc"
API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
MODEL_NAME = "FunAudioLLM/SenseVoiceSmall"

SLICE_DURATION = 600
TEMP_DIR = os.path.join(os.getcwd(), "temp_scan")
SANITIZE_METADATA = True
CHECK_SUBTITLES = True

# --- 扫描模式选择 ---
# "auto"  : 智能模式 (默认) -> 优先使用 API，如果失败则自动切换到本地模型
# "local" : 纯本地模式 -> 直接使用本地模型，完全不联网
# "api"   : 纯云端模式 -> 只使用 API，失败则跳过
SCAN_MODE = "local"

# --- 本地模型配置 ---
LOCAL_MODELS_ROOT = os.path.join(os.getcwd(), "models")

# --- 黑名单配置 ---
BLACKLIST_KEYWORDS = [
    "加群", "交流群", "TG群", "Telegram", "QQ群", "Q群",
    "资源群", "福利群", "粉丝群", "看片",
    "微信号", "加微信", "微信群", "微信公众号", "关注公众号",
    "QQ号", "加Q", "加我V", "加V", "澳门", "威信", "VX", "http", "www"
]

META_BLACKLIST = [
    "http", "www", "weixin", "Telegram", "TG@", "TG频道@",
    "群：", "群:", "资源群", "加群", "微信号", "微信群",
    "QQ", "qq", "q群", "公众号", "微博", "b站", "Tacit0924",
    "整理", "无人在意做自己", "资源站", "资源网",
    "发布页", "压制", "荣誉出品","我堡牛皮",
    "link3.cc", "ysepan.com", "GyWEB", "Qqun", "hehehe", ".com",
    "PTerWEB", "panclub", "BT之家", "CMCT", "Byakuya", "ed3000",
    "yunpantv", "KKYY", "盘酱酱", "TREX", "£yhq@tv", "1000fr",
    "HDCTV", "HHWEB", "ADWeb", "PanWEB", "BestWEB"
]

GLOBAL_TAGS_TO_CHECK = ["genre", "comment", "description", "synopsis", "title", "artist", "album", "copyright"]
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.ts', '.m4v', '.webm'}


# ================= 🛠️ 基础工具 =================

def log(msg, level="INFO"):
    timestamp = time.strftime("%H:%M:%S")
    prefix = "🔵" if level == "INFO" else ("⚠️" if level == "WARN" else ("❌" if level == "ERR" else "✅"))
    if level == "HIT": prefix = "🚨"
    if level == "TEXT": prefix = "📝"
    print(f"[{timestamp}] [{level}] {prefix} {msg}")


def run_cmd(cmd_list, capture=True, timeout=None):
    try:
        if capture:
            return subprocess.run(cmd_list, capture_output=True, text=True, encoding='utf-8', errors='ignore',
                                  timeout=timeout)
        else:
            return subprocess.run(cmd_list, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=timeout)
    except:
        return None


def verify_file_integrity(file_path):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024: return False
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'format=duration', '-of',
               'default=noprint_wrappers=1:nokey=1', file_path]
        res = run_cmd(cmd, capture=True, timeout=10)
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


def print_ffmpeg_raw_info(file_path):
    print("\n" + "=" * 20 + " [FFmpeg Info] " + "=" * 20)
    try:
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


# ================= 🧠 智能音轨选择 =================

def get_smart_audio_map(file_path):
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a',
               '-show_entries', 'stream=index,codec_name', '-of', 'csv=p=0', file_path]
        res = run_cmd(cmd, capture=True)

        streams = []
        if res and res.stdout:
            for line in res.stdout.strip().splitlines():
                parts = line.split(',')
                if len(parts) >= 2:
                    streams.append({'index': parts[0], 'codec': parts[1].strip().lower()})

        if streams:
            first = streams[0]
            if 'flac' in first['codec'] and len(streams) > 1:
                second = streams[1]
                log(f"⚠️ 首选音轨为 FLAC，自动切换至次选音轨: Stream #{second['index']} ({second['codec']})", "WARN")
                return f"0:{second['index']}"
            else:
                log(f"🎵 使用默认音轨: Stream #{first['index']} ({first['codec']})", "INFO")
                return "0:a:0"

    except Exception as e:
        log(f"音轨选择出错: {e}", "ERR")

    return "0:a:0"


def sanitize_subtitles(source):
    if not CHECK_SUBTITLES: return False

    try:
        cmd_scan = ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries', 'stream=index', '-of', 'csv=p=0',
                    source]
        res = run_cmd(cmd_scan, capture=True)
        if not res or not res.stdout.strip(): return False

        all_sub_indices = [x.strip() for x in res.stdout.splitlines() if x.strip()]
    except Exception as e:
        log(f"扫描字幕轨失败: {e}", "ERR")
        return False

    dirty_indices = []
    all_blacklist = list(set(BLACKLIST_KEYWORDS + META_BLACKLIST))

    if "by" in all_blacklist: all_blacklist.remove("by")

    for idx in all_sub_indices:
        try:
            cmd_extract = ['ffmpeg', '-v', 'error', '-i', source, '-map', f'0:{idx}', '-f', 'srt', '-']
            res = run_cmd(cmd_extract, capture=True)

            if res and res.stdout:
                content = res.stdout.lower()
                for kw in all_blacklist:
                    if kw.lower() in content:
                        log(f"🚨 字幕轨 [Stream #{idx}] 命中黑名单: '{kw}'", "WARN")
                        dirty_indices.append(idx)
                        break
        except:
            continue

    if not dirty_indices: return False

    log(f"🧹 正在移除 {len(dirty_indices)} 个违规字幕轨...", "CLEAN")

    dir_name = os.path.dirname(source)
    name, ext = os.path.splitext(os.path.basename(source))
    output_path = os.path.join(dir_name, f"{name}_clean_sub{ext}")

    try:
        cmd_rebuild = ['ffmpeg', '-v', 'error', '-i', source, '-map', '0:v', '-map', '0:a?']
        for idx in all_sub_indices:
            if idx not in dirty_indices:
                cmd_rebuild.extend(['-map', f'0:{idx}'])

        cmd_rebuild.extend(['-c', 'copy', '-dn', '-ignore_unknown', '-y', output_path])
        run_cmd(cmd_rebuild, capture=False)

        if verify_file_integrity(output_path):
            if safe_replace(output_path, source):
                log(f"✨ 清洗完成，保留了 {len(all_sub_indices) - len(dirty_indices)} 条干净字幕", "SUCCESS")
                return True
        else:
            log("❌ 重构文件失败，保留原文件", "ERR")
            if os.path.exists(output_path): os.remove(output_path)

    except Exception as e:
        log(f"移除字幕出错: {e}", "ERR")
        if os.path.exists(output_path): os.remove(output_path)

    return False


def sanitize_metadata(source):
    if not SANITIZE_METADATA: return False
    clean_needed = False

    _, ext = os.path.splitext(source)
    output_path = os.path.join(os.path.dirname(source), f"temp_meta_{int(time.time())}{ext}")

    try:
        for tag in GLOBAL_TAGS_TO_CHECK:
            res = run_cmd(['ffprobe', '-v', 'error', '-show_entries', f'format_tags={tag}', '-of', 'csv=p=0', source],
                          capture=True)
            if res and res.stdout and any(k.lower() in res.stdout.lower() for k in META_BLACKLIST):
                clean_needed = True;
                log(f"🔍 发现脏全局标签 [{tag}]", "WARN");
                break

        if not clean_needed:
            res = run_cmd(
                ['ffprobe', '-v', 'error', '-show_entries', 'stream=index:stream_tags=language,title,handler_name',
                 '-of', 'csv=p=0', source], capture=True)
            if res and res.stdout and any(k.lower() in res.stdout.lower() for k in META_BLACKLIST):
                clean_needed = True;
                log(f"🔍 发现脏轨道标签", "WARN")

        if clean_needed:
            log("🚫 发现脏元数据，正在执行核弹级清洗...", "CLEAN")
            cmd_nuclear = [
                'ffmpeg', '-err_detect', 'ignore_err', '-i', source,
                '-map', '0:v:0', '-map', '0:a?', '-map', '0:s?',
                '-c', 'copy', '-dn', '-ignore_unknown',
                '-map_metadata', '-1',
                '-metadata', 'title=', '-metadata', 'comment=',
                '-metadata', 'description=', '-metadata', 'synopsis=',
                '-metadata', 'artist=', '-metadata', 'album=', '-metadata', 'copyright=',
                '-metadata:s', 'title=', '-metadata:s', 'language=und', '-metadata:s', 'handler_name=',
                '-y', output_path
            ]
            run_cmd(cmd_nuclear, capture=False, timeout=120)

            if verify_file_integrity(output_path):
                if safe_replace(output_path, source):
                    log("✨ 元数据已深度净化", "SUCCESS")
                    return True
            else:
                log("❌ 元数据清洗失败", "ERR")
                if os.path.exists(output_path): os.remove(output_path)
    except Exception as e:
        log(f"元数据检查出错: {e}", "ERR")
        if os.path.exists(output_path): os.remove(output_path)

    return False


# ================= 🤖 本地模型逻辑 =================
local_model_instance = None


@contextmanager
def suppress_output():
    # 屏蔽 FunASR 底层输出
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdout = devnull
            sys.stderr = devnull
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


def init_local_model():
    global local_model_instance
    if local_model_instance is not None: return True

    log(f"⏳ 正在加载本地模型 (Path: {LOCAL_MODELS_ROOT})...", "INFO")
    try:
        from funasr import AutoModel
        if not os.path.exists(LOCAL_MODELS_ROOT): os.makedirs(LOCAL_MODELS_ROOT)
        with suppress_output():
            local_model_instance = AutoModel(
                model="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                punc_model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                device="cpu", ncpu=4, disable_update=True, log_level="ERROR",
                cache_dir=LOCAL_MODELS_ROOT
            )
        log("✅ 本地模型加载完成", "SUCCESS")
        return True
    except ImportError:
        log("❌ 未安装 funasr，请运行 pip install funasr modelscope torch", "ERR")
        return False
    except Exception as e:
        log(f"❌ 本地模型加载失败: {e}", "ERR")
        return False


def scan_audio_local(audio_path):
    if not local_model_instance:
        if not init_local_model(): return False, "Model Load Failed"

    try:
        with suppress_output():
            res = local_model_instance.generate(input=audio_path, batch_size_s=300)

        if res and isinstance(res, list) and len(res) > 0:
            text = res[0].get("text", "")
            if text:
                norm_text = normalize_text(text)
                # 🔥🔥🔥 修改点：移除 [:50] 限制，输出全文 🔥🔥🔥
                log(f"📝 [Local] 识别结果: {norm_text}", "TEXT")
                return check_spam_final(norm_text)
        return False, None
    except Exception as e:
        log(f"本地识别出错: {e}", "ERR")
        return False, str(e)


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


def extract_audio_segment(input_source, start_time, duration, output_path, map_arg="0:a:0"):
    cmd = [
        'ffmpeg', '-v', 'error', '-ss', str(start_time), '-i', input_source,
        '-t', str(duration), '-vn', '-sn', '-map', map_arg,
        '-ac', '1', '-ar', '16000', '-acodec', 'pcm_s16le', '-f', 'wav', '-y', output_path
    ]
    run_cmd(cmd, capture=False)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 1024


def scan_audio_cloud(audio_path, time_offset):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        with open(audio_path, "rb") as f:
            response = requests.post(API_URL, headers=headers,
                                     files={"file": ("a.wav", f, "audio/wav"), "model": (None, MODEL_NAME),
                                            "response_format": (None, "json"),
                                            "prompt": (None, "资源分享 QQ群 微信号 加群 70377")}, timeout=60)
            if response.status_code == 200:
                text = normalize_text(response.json().get("text", ""))
                # 🔥🔥🔥 修改点：移除 [:50] 限制，输出全文 🔥🔥🔥
                log(f"💬 [Cloud] {text}", "TEXT")
                return check_spam_final(text)
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
    log(f"开始分析: {filename} (Mode: {SCAN_MODE})", "START")

    print_ffmpeg_raw_info(file_path)
    log_metadata_and_tracks(file_path)

    audio_map_arg = get_smart_audio_map(file_path)

    if sanitize_metadata(file_path): log("元数据清洗完成", "INFO")
    if sanitize_subtitles(file_path): log("字幕清洗完成", "INFO")

    duration = get_duration(file_path)
    if duration == 0: return

    tasks = []
    tail_dur = min(600 if duration >= 3600 else 300, duration)
    tasks.append({"start": max(0, duration - tail_dur), "duration": tail_dur, "name": "片尾优先"})
    if duration > 600:
        tasks.append({"start": (duration / 2) - 120, "duration": 240, "name": "中间抽查"})
        tasks.append({"start": 0, "duration": 240, "name": "片头抽查"})

    temp_wav = os.path.join(TEMP_DIR, f"scan_{hashlib.md5(file_path.encode()).hexdigest()[:8]}.wav")
    hit, hit_reason = False, ""

    for i, task in enumerate(tasks):
        log(f"🔍 任务 ({i + 1}/{len(tasks)}): [{task['name']}]", "SCAN")

        if extract_audio_segment(file_path, task['start'], task['duration'], temp_wav, map_arg=audio_map_arg):

            is_spam, reason = False, None

            # 模式判定
            if SCAN_MODE == "local":
                is_spam, reason = scan_audio_local(temp_wav)
            elif SCAN_MODE == "api":
                is_spam, reason = scan_audio_cloud(temp_wav, task['start'])
            else:  # auto
                is_spam, reason = scan_audio_cloud(temp_wav, task['start'])
                if not is_spam and reason and ("HTTP" in reason or "Error" in reason):
                    log(f"⚠️ 云端异常 ({reason})，切换本地...", "WARN")
                    is_spam, reason = scan_audio_local(temp_wav)

            if is_spam:
                hit = True;
                hit_reason = reason
                log(f"🚨 发现广告: {reason}", "HIT")
                break

        if os.path.exists(temp_wav): os.remove(temp_wav)

    if os.path.exists(temp_wav): os.remove(temp_wav)

    if hit:
        if is_temp:
            log("🗑️ 删除临时脏文件", "DEL");
            os.remove(file_path)
        else:
            try:
                dirty_dir = os.path.join(os.path.dirname(file_path), "脏文件")
                if not os.path.exists(dirty_dir): os.makedirs(dirty_dir)
                shutil.move(file_path, os.path.join(dirty_dir, filename))
                log(f"已移入脏文件目录: {dirty_dir}", "MOVED")
            except:
                log("移动失败，尝试重命名", "ERR")
                try: os.rename(file_path, os.path.join(os.path.dirname(file_path), "脏-" + filename));
                except: pass
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