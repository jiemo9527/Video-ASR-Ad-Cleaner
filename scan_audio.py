#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import subprocess
import re
import json
import time
import signal
import hashlib
import random
import fcntl


# ================= 📦 依赖库自动检测与安装 =================
def install_package(package):
    print(f"正在安装缺失的库: {package}...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])


try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    install_package("requests")
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

try:
    from pypinyin import lazy_pinyin
    from thefuzz import fuzz
except ImportError:
    install_package("pypinyin")
    install_package("thefuzz")
    from pypinyin import lazy_pinyin
    from thefuzz import fuzz

# ================= ⚙️ 配置 =================
API_KEY = "sk-abc"
API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
MODEL_NAME = "FunAudioLLM/SenseVoiceSmall"

DEBUG_MODE = False
SANITIZE_METADATA = True
# 🔥 新增：字幕检测开关 (True: 开启, False: 关闭)
CHECK_SUBTITLES = True

CMD_TIMEOUT = 120
MAX_API_RETRIES = 4
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.ts', '.m4v', '.webm'}

# ================= 🚫 黑名单 =================
AUDIO_BLACKLIST = [
    "加群", "交流群", "TG群", "Telegram", "QQ群", "Q群",
    "资源群", "微信号", "微信群", "微信公众号", "关注公众号",
]

SUB_META_BLACKLIST = [
    # 基础社交与链接
    "http", "www", "weixin", "Telegram", "TG@", "TG频道@",
    "群：", "群:", "资源群", "加群", "微信号", "微信群",

    # 社交平台与工具
    "QQ", "qq", "q群", "公众号", "微博", "b站", "Tacit0924",

    # 关键词与短语 "压制","整理",
     "无人在意做自己", "资源站", "资源网",
    "发布页","荣誉出品", "字幕组", "我堡牛皮",

    # 特定站点与标识符
    "link3.cc", "ysepan.com", "GyWEB", "Qqun", "hehehe", ".com",
    "PTerWEB", "panclub", "BT之家", "CMCT", "Byakuya", "ed3000",
    "yunpantv", "KKYY", "盘酱酱", "TREX", "£yhq@tv", "1000fr",
    "HDCTV", "HHWEB", "ADWeb", "PanWEB", "BestWEB"
]

GLOBAL_TAGS_TO_CHECK = ["genre", "comment", "description", "synopsis", "title", "artist", "album", "copyright"]


# ================= 🛠️ 日志 =================
class PrettyLog:
    @staticmethod
    def info(msg): print(f"\033[94m[INFO]\033[0m {msg}")

    @staticmethod
    def success(msg): print(f"\033[92m[SUCCESS]\033[0m {msg}")

    @staticmethod
    def warn(msg): print(f"\033[93m[WARN]\033[0m {msg}")

    @staticmethod
    def error(msg): print(f"\033[91m[ERROR]\033[0m {msg}")

    @staticmethod
    def fatal(msg): print(f"\033[97;41m[FATAL]\033[0m {msg}")

    @staticmethod
    def step(msg): print(f"\n\033[96m🔵 {msg}\033[0m")

    @staticmethod
    def hit(msg): print(f"\033[91m🚨 [HIT] {msg}\033[0m")


# ================= 🛠️ 基础函数 =================
def write_reason_to_env(reason):
    reason_file = os.environ.get("SCAN_REASON_FILE")
    if reason_file:
        try:
            with open(reason_file, "w", encoding="utf-8") as f:
                f.write(reason)
        except:
            pass


def run_cmd(cmd, capture=True, timeout=CMD_TIMEOUT):
    try:
        if DEBUG_MODE: print(f"[CMD] {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='ignore', timeout=timeout
        )
        return result
    except subprocess.TimeoutExpired:
        PrettyLog.error(f"⚠️ 命令超时 ({timeout}s): {cmd[0]}")
        return None
    except Exception as e:
        PrettyLog.error(f"命令出错: {e}")
        return None


def verify_file_integrity(file_path):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024: return False
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'format=duration', '-of',
               'default=noprint_wrappers=1:nokey=1', file_path]
        res = run_cmd(cmd, capture=True, timeout=30)
        return float(res.stdout.strip()) > 0 if res and res.stdout.strip() else False
    except:
        return False


def safe_replace(src, dst):
    try:
        if os.path.exists(dst): os.remove(dst)
        os.rename(src, dst)
        return True
    except OSError as e:
        PrettyLog.error(f"替换失败: {e}")
        return False


# ================= 🧹 1. 元数据清洗 =================
def sanitize_metadata_tags(source):
    if not SANITIZE_METADATA: return False
    clean_needed = False
    log_details = []

    for tag in GLOBAL_TAGS_TO_CHECK:
        res = run_cmd(['ffprobe', '-v', 'error', '-show_entries', f'format_tags={tag}', '-of', 'csv=p=0', source],
                      timeout=30)
        if res and res.stdout:
            content = res.stdout.lower()
            for kw in SUB_META_BLACKLIST:
                if kw.lower() in content:
                    log_details.append(f"全局标签 [{tag}] 含 '{kw}'")
                    clean_needed = True
                    break
        if clean_needed: break

    if not clean_needed:
        res = run_cmd(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=index:stream_tags=language,title,handler_name', '-of',
             'csv=p=0', source], timeout=30)

        if res and res.stdout:
            content = res.stdout.lower()
            for kw in SUB_META_BLACKLIST:
                if kw.lower() in content:
                    log_details.append(f"轨道标签检测到 '{kw}'")
                    clean_needed = True
                    break

    if clean_needed:
        for d in log_details: PrettyLog.hit(d)
        PrettyLog.info("🧹 [Clean] 发现脏标签，正在深度清洗元数据...")

        dir_name = os.path.dirname(source)
        name, ext = os.path.splitext(os.path.basename(source))
        output_path = os.path.join(dir_name, f"{name}_clean_meta{ext}")

        cmd_nuclear = [
            'ffmpeg', '-err_detect', 'ignore_err', '-i', source,
            '-map', '0:v:0', '-map', '0:a?', '-map', '0:s?',
            '-c', 'copy',
            '-strict', '-2',
            '-dn',
            '-ignore_unknown',
            '-map_metadata', '-1',
            '-metadata', 'title=', '-metadata', 'comment=',
            '-metadata', 'description=', '-metadata', 'synopsis=',
            '-metadata', 'artist=', '-metadata', 'album=',
            '-metadata', 'copyright=',
            '-metadata:s', 'title=', '-metadata:s', 'language=und', '-metadata:s', 'handler_name=',
            '-y', output_path
        ]

        res = run_cmd(cmd_nuclear, capture=True, timeout=300)

        if res and res.returncode == 0 and verify_file_integrity(output_path):
            if safe_replace(output_path, source):
                PrettyLog.success("✨ [Clean] 元数据已深度净化 (Data流已剥离)")
                return True
        else:
            PrettyLog.error("❌ 元数据清洗失败")
            if res and res.stderr:
                err_log = res.stderr.splitlines()[-3:]
                for l in err_log: PrettyLog.warn(f"FFmpeg Error: {l}")

        if os.path.exists(output_path): os.remove(output_path)

    return False


# ================= 🧹 2. 字幕内容检测 =================
def sanitize_subtitle_content(source):
    # 🔥🔥🔥 检查开关 🔥🔥🔥
    if not CHECK_SUBTITLES:
        return None

    res = run_cmd(
        ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries', 'stream=index', '-of', 'csv=p=0', source],
        timeout=10)
    if not res or not res.stdout.strip(): return None

    subtitle_indices = [x.strip() for x in res.stdout.splitlines() if x.strip()]
    dirty_indices = []

    for idx in subtitle_indices:
        extract_cmd = ['ffmpeg', '-v', 'error', '-i', source, '-map', f'0:{idx}', '-f', 'webvtt', '-']
        proc = subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        sub_content = proc.stdout
        if not sub_content: continue

        hit_kw = None
        for kw in SUB_META_BLACKLIST:
            if kw in sub_content:
                hit_kw = kw
                break

        if hit_kw:
            PrettyLog.hit(f"字幕轨 [Stream #{idx}] 内容包含: '{hit_kw}' -> 计划移除")
            dirty_indices.append(idx)

    if not dirty_indices: return None

    PrettyLog.info(f"🧹 [Clean] 正在移除 {len(dirty_indices)} 个违规字幕轨...")
    dir_name = os.path.dirname(source)
    name, ext = os.path.splitext(os.path.basename(source))

    temp_output_path = os.path.join(dir_name, f"{name}_temp_clean{ext}")
    final_clean_path = os.path.join(dir_name, f"{name}_clean{ext}")

    cmd_clean = ['ffmpeg', '-err_detect', 'ignore_err', '-i', source, '-map', '0:v:0', '-map', '0:a?']

    for s_idx in subtitle_indices:
        if s_idx not in dirty_indices:
            cmd_clean.extend(['-map', f'0:{s_idx}'])

    cmd_clean.extend([
        '-c', 'copy',
        '-strict', '-2',
        '-dn',
        '-ignore_unknown',
        '-y', temp_output_path
    ])

    if run_cmd(cmd_clean, capture=False, timeout=120) and verify_file_integrity(temp_output_path):
        try:
            if os.path.exists(source): os.remove(source)
            if os.path.exists(final_clean_path): os.remove(final_clean_path)
            os.rename(temp_output_path, final_clean_path)

            PrettyLog.success(
                f"✨ [Clean] 违规字幕已移除 (保留其余轨道信息)，重命名为: {os.path.basename(final_clean_path)}")
            return final_clean_path
        except OSError as e:
            PrettyLog.error(f"重命名失败: {e}")
            if os.path.exists(temp_output_path): os.remove(temp_output_path)
            return None

    if os.path.exists(temp_output_path): os.remove(temp_output_path)
    return None


# ================= 🎙️ 3. 音频检测相关 =================
def remove_emojis(text):
    if not text: return ""
    return re.sub(r'[\U00010000-\U0010ffff]', '', text).strip()


def get_duration(file_path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
           file_path]
    res = run_cmd(cmd, timeout=10)
    if res and res.stdout.strip():
        try:
            return float(res.stdout.strip())
        except ValueError:
            pass
    return 0


def get_smart_audio_map(file_path):
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a',
               '-show_entries', 'stream=index,codec_name', '-of', 'csv=p=0', file_path]
        res = run_cmd(cmd, capture=True, timeout=10)

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
                PrettyLog.warn(f"⚠️ 首选音轨为 FLAC，自动切换至次选: Stream #{second['index']} ({second['codec']})")
                return f"0:{second['index']}"
            else:
                return "0:a:0"
    except Exception as e:
        PrettyLog.error(f"音轨分析出错: {e}")

    return "0:a:0"


def extract_audio(video_path, start, duration, output_path, map_arg="0:a:0"):
    cmd = [
        'ffmpeg', '-ss', str(start), '-t', str(duration),
        '-i', video_path,
        '-map', map_arg,
        '-vn', '-acodec', 'libmp3lame', '-q:a', '4',
        '-y', output_path
    ]
    res = run_cmd(cmd, capture=False, timeout=30)
    return res is not None and res.returncode == 0


def send_to_api(audio_path):
    if not os.path.exists(audio_path): return None
    try:
        headers = {"Authorization": f"Bearer {API_KEY}"}
        files = {"file": open(audio_path, "rb")}
        data = {"model": MODEL_NAME, "language": "zh", "response_format": "json"}

        session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries))

        response = session.post(API_URL, headers=headers, files=files, data=data, timeout=60)
        if response.status_code == 200:
            return response.json().get("text", "")
        else:
            PrettyLog.error(f"API Error {response.status_code}")
            return None
    except Exception as e:
        PrettyLog.error(f"请求异常: {e}")
        return None


def normalize_text(text):
    if not text: return ""
    text = re.sub(r'<\|.*?\|>', '', text)
    trans = str.maketrans("零一二三四五六七八九", "0123456789")
    text = text.translate(trans)
    return re.sub(r'[^\w\s,.，。？！:：0-9a-zA-Z\u4e00-\u9fa5/\-_.\[\]\(\)]', '', text)


def check_audio_keywords_detail(text):
    if not text: return False, None
    normalized_text = normalize_text(text)

    match = re.search(r'(资源|加群|入群|群号|QQ|TG|VX|微信).{0,12}\d{5,}', normalized_text, re.IGNORECASE)
    if match:
        context = normalized_text[max(0, match.start() - 10):min(len(normalized_text), match.end() + 10)]
        return True, f"正则匹配: [{match.group(0)}] (...{context}...)"

    for kw in AUDIO_BLACKLIST:
        if kw in normalized_text:
            return True, f"关键词匹配: {kw}"

    text_pinyin = "".join(lazy_pinyin(normalized_text))
    for kw in AUDIO_BLACKLIST:
        if "".join(lazy_pinyin(kw)) in text_pinyin:
            return True, f"拼音匹配: {kw}"

    return False, None


# ================= 🔄 主逻辑 =================
def process_single_source(source):
    if not os.path.exists(source): return
    PrettyLog.step(f"正在分析: {os.path.basename(source)}")

    sanitize_metadata_tags(source)

    new_source = sanitize_subtitle_content(source)
    if new_source and os.path.exists(new_source):
        source = new_source
        PrettyLog.info(f"🔄 切换后续扫描目标为: {os.path.basename(source)}")

    total_duration = get_duration(source)
    if total_duration == 0: sys.exit(0)

    audio_map_arg = get_smart_audio_map(source)

    tasks = []
    tail_dur = min(600 if total_duration >= 3600 else 300, total_duration)
    tasks.append({"start": max(0, total_duration - tail_dur), "duration": tail_dur, "name": "片尾优先"})
    if total_duration > 600:
        tasks.append({"start": (total_duration / 2) - 120, "duration": 240, "name": "中间抽查"})
        tasks.append({"start": 0, "duration": 240, "name": "片头抽查"})

    temp_wav = f"/tmp/scan_{os.getpid()}_{hashlib.md5(source.encode()).hexdigest()[:8]}.mp3"
    hit_reason = None
    api_fail_count = 0

    for idx, task in enumerate(tasks):
        if hit_reason: break
        PrettyLog.info(f"🔍 任务 ({idx + 1}/{len(tasks)}): [{task['name']}]")

        if extract_audio(source, task['start'], task['duration'], temp_wav, map_arg=audio_map_arg):
            segment_success = False
            for attempt in range(MAX_API_RETRIES):
                raw_text = send_to_api(temp_wav)
                if raw_text is not None:
                    clean_text = remove_emojis(raw_text)
                    is_hit, reason = check_audio_keywords_detail(clean_text)

                    if DEBUG_MODE:
                        PrettyLog.info(f"📝 结果: {clean_text[:100]}...")

                    if is_hit:
                        hit_reason = f"{task['name']} -> {reason}"

                    segment_success = True
                    break
                else:
                    if attempt < MAX_API_RETRIES - 1:
                        sleep_time = (attempt + 1) * 5 + random.randint(1, 3)
                        PrettyLog.warn(f"⚠️ API 失败，{sleep_time}秒后重试...")
                        time.sleep(sleep_time)

            if not segment_success:
                PrettyLog.error("❌ 分片重试失败，停止后续任务")
                api_fail_count += 1
                if os.path.exists(temp_wav): os.remove(temp_wav)
                break

            if os.path.exists(temp_wav): os.remove(temp_wav)
        else:
            PrettyLog.error("❌ 音频提取失败")
            api_fail_count += 1
            break

    if hit_reason:
        write_reason_to_env(hit_reason)
        PrettyLog.fatal(f"🚫 发现违规音频! 原因: {hit_reason}")
        sys.exit(1)

    if api_fail_count > 0:
        PrettyLog.warn(f"⚠️ 存在分析失败分片，转本地")
        sys.exit(2)

    PrettyLog.success("✅ [Cloud] 云端音频内容检测通过 (安全)")
    sys.exit(0)


def main():
    lock_file = None
    max_slots = 2
    lock_base = "/tmp/scan_audio_cloud.lock"

    PrettyLog.info(f"⏳ [Queue] 云端 API 频率控制中 (Limit: {max_slots})...")

    while lock_file is None:
        for i in range(max_slots):
            try:
                f = open(f"{lock_base}.{i}", "w")
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_file = f
                break
            except OSError:
                f.close()

        if lock_file is None:
            time.sleep(1)

    PrettyLog.info("🔓 [Queue] 队列通过，开始扫描")

    signal.alarm(600)

    try:
        if len(sys.argv) < 2: sys.exit(1)
        process_single_source(sys.argv[1])
    finally:
        if lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()


if __name__ == "__main__":
    main()