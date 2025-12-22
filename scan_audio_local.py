#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import subprocess
import re
import time
import hashlib
from contextlib import contextmanager  # 🔥 新增引入

# 保持日志级别设置
os.environ["MODELSCOPE_LOG_LEVEL"] = "40"

from pypinyin import lazy_pinyin
from thefuzz import fuzz

# ================= ⚙️ 本地配置 =================
DEVICE = "cpu"
CPU_THREADS = 4
DEBUG_MODE = False

LOCAL_MODEL_DIR = "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_MODEL_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
PUNC_MODEL_ID = "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"

SANITIZE_METADATA = True

AUDIO_BLACKLIST = [
    "加群", "交流群", "TG群", "Telegram", "QQ群", "Q群",
    "资源群", "微信号", "微信群", "微信公众号", "关注公众号",
]

SUB_META_BLACKLIST = [
    #基础社交与链接
    "http", "www", "weixin", "Telegram", "TG@", "TG频道@",
    "群：", "群:", "资源群", "加群", "微信号", "微信群",
    #社交平台与工具
    "QQ", "qq", "q群", "公众号", "微博", "b站", "Tacit0924",
    #关键词与短语
    "by", "整理", "无人在意做自己", "资源站", "资源网",
    "发布页", "压制", "荣誉出品", "字幕组", "我堡牛皮",
    #特定站点与标识符
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


# ================= 🛠️ 辅助函数 =================
# 🔥 新增：静音模式，屏蔽所有底层库的输出 (解决 blob data 问题)
@contextmanager
def suppress_output():
    if DEBUG_MODE:
        yield
        return
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


def write_reason_to_env(reason):
    reason_file = os.environ.get("SCAN_REASON_FILE")
    if reason_file:
        try:
            with open(reason_file, "w", encoding="utf-8") as f:
                f.write(reason)
        except:
            pass


def run_cmd(cmd, capture=True, timeout=60):
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            text=True, timeout=timeout
        )
        return result
    except subprocess.TimeoutExpired:
        PrettyLog.error(f"命令超时: {cmd[0]}")
        return None
    except Exception:
        return None


def verify_file_integrity(file_path):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024: return False
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'format=duration', '-of',
               'default=noprint_wrappers=1:nokey=1', file_path]
        res = run_cmd(cmd, capture=True, timeout=10)
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

    for tag in GLOBAL_TAGS_TO_CHECK:
        res = run_cmd(['ffprobe', '-v', 'error', '-show_entries', f'format_tags={tag}', '-of', 'csv=p=0', source],
                      timeout=10)
        val = res.stdout.strip() if res else ""
        if val:
            for kw in SUB_META_BLACKLIST:
                if kw.lower() in val.lower():
                    clean_needed = True;
                    break
        if clean_needed: break

    if not clean_needed:
        res = run_cmd(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=index:stream_tags=language,title,handler_name', '-of',
             'csv=p=0', source], timeout=10)
        for line in (res.stdout.splitlines() if res else []):
            for kw in SUB_META_BLACKLIST:
                if kw.lower() in line.lower():
                    clean_needed = True;
                    break
            if clean_needed: break

    if clean_needed:
        PrettyLog.info("🧹 [Clean] 发现脏标签，正在深度清洗元数据...")
        dir_name = os.path.dirname(source)
        name, ext = os.path.splitext(os.path.basename(source))
        output_path = os.path.join(dir_name, f"{name}_clean_meta{ext}")

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
        if run_cmd(cmd_nuclear, capture=False, timeout=90) and verify_file_integrity(output_path):
            if safe_replace(output_path, source):
                PrettyLog.success("✨ [Clean] 元数据深度净化 (Data流已剥离)")
                return True
        if os.path.exists(output_path): os.remove(output_path)
    return False


# ================= 🧹 2. 字幕内容检测 =================
def sanitize_subtitle_content(source):
    res = run_cmd(
        ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries', 'stream=index', '-of', 'csv=p=0', source],
        timeout=10)
    if not res or not res.stdout.strip(): return False

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
                hit_kw = kw;
                break

        if hit_kw:
            PrettyLog.hit(f"字幕轨 [Stream #{idx}] 内容包含: '{hit_kw}' -> 计划移除")
            dirty_indices.append(idx)

    if not dirty_indices: return False

    PrettyLog.info(f"🧹 [Clean] 正在移除 {len(dirty_indices)} 个违规字幕轨...")
    dir_name = os.path.dirname(source)
    name, ext = os.path.splitext(os.path.basename(source))
    output_path = os.path.join(dir_name, f"{name}_clean_sub{ext}")

    cmd_clean = ['ffmpeg', '-err_detect', 'ignore_err', '-i', source, '-map', '0:v:0', '-map', '0:a?']
    for s_idx in subtitle_indices:
        if s_idx not in dirty_indices:
            cmd_clean.extend(['-map', f'0:{s_idx}'])

    cmd_clean.extend([
        '-c', 'copy', '-dn', '-ignore_unknown',
        '-map_metadata', '-1',
        '-metadata', 'title=', '-metadata', 'comment=',
        '-metadata:s', 'title=', '-metadata:s', 'language=und', '-metadata:s', 'handler_name=',
        '-y', output_path
    ])

    if run_cmd(cmd_clean, capture=False, timeout=120) and verify_file_integrity(output_path):
        if safe_replace(output_path, source):
            PrettyLog.success("✨ [Clean] 违规字幕轨已移除 & 元数据已同步净化")
            return True
    if os.path.exists(output_path): os.remove(output_path)
    return False


# ================= 🎙️ 音频检测相关 =================
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
    return False, None


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


def extract_audio(video_path, start, duration, output_path):
    cmd = ['ffmpeg', '-ss', str(start), '-t', str(duration), '-i', video_path, '-vn', '-acodec', 'pcm_s16le', '-ar',
           '16000', '-ac', '1', '-y', output_path]
    res = run_cmd(cmd, capture=False, timeout=30)
    return res is not None and res.returncode == 0


# ================= 🤖 本地模型 =================
model_instance = None


def init_model():
    global model_instance
    if model_instance is None:
        try:
            PrettyLog.info("⏳ 正在加载本地模型 (Paraformer)...")
            from funasr import AutoModel
            # 🔥 包裹 suppress_output 上下文，彻底屏蔽下载进度条
            with suppress_output():
                model_instance = AutoModel(
                    model=LOCAL_MODEL_DIR,
                    vad_model=VAD_MODEL_ID,
                    punc_model=PUNC_MODEL_ID,
                    device=DEVICE,
                    ncpu=CPU_THREADS,
                    disable_update=True,
                    log_level="ERROR"
                )
            PrettyLog.success("本地模型加载完成")
        except Exception as e:
            PrettyLog.fatal(f"模型加载失败: {e}")
            sys.exit(1)


def transcribe_local(audio_path):
    if not os.path.exists(audio_path): return None
    try:
        # 🔥 包裹 suppress_output 上下文，彻底屏蔽推理进度条
        with suppress_output():
            res = model_instance.generate(input=audio_path, batch_size_s=300)
        if res and isinstance(res, list) and len(res) > 0:
            return res[0].get("text", "")
        return ""
    except Exception as e:
        PrettyLog.error(f"识别出错: {e}")
        return None


# ================= 🔄 主流程 =================
def process_single_source(source):
    if not os.path.exists(source): return
    PrettyLog.step(f"正在分析 (Local): {os.path.basename(source)}")

    sanitize_metadata_tags(source)
    sanitize_subtitle_content(source)

    total_duration = get_duration(source)
    if total_duration == 0: sys.exit(0)

    tasks = []
    tail_dur = min(600 if total_duration >= 3600 else 300, total_duration)
    tasks.append({"start": max(0, total_duration - tail_dur), "duration": tail_dur, "name": "片尾优先"})
    if total_duration > 600:
        tasks.append({"start": (total_duration / 2) - 120, "duration": 240, "name": "中间抽查"})
        tasks.append({"start": 0, "duration": 240, "name": "片头抽查"})

    init_model()

    import hashlib
    temp_wav = f"/tmp/scan_local_{os.getpid()}_{hashlib.md5(source.encode()).hexdigest()[:8]}.wav"
    hit_reason = None

    for idx, task in enumerate(tasks):
        if hit_reason: break
        PrettyLog.info(f"🔍 任务 ({idx + 1}/{len(tasks)}): [{task['name']}]")

        if extract_audio(source, task['start'], task['duration'], temp_wav):
            text = transcribe_local(temp_wav)
            if text:
                is_hit, reason = check_audio_keywords_detail(text)
                if DEBUG_MODE:
                    PrettyLog.info(f"📝 结果: {text[:100]}...")

                if is_hit:
                    hit_reason = f"{task['name']} -> {reason}"

            if os.path.exists(temp_wav): os.remove(temp_wav)
        else:
            PrettyLog.warn("音频提取失败")

    if hit_reason:
        write_reason_to_env(hit_reason)
        PrettyLog.fatal(f"🚫 发现违规音频! 原因: {hit_reason}")
        sys.exit(1)

    PrettyLog.success("✅ [Local] 本地音频内容检测通过 (安全)")
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit(1)
    process_single_source(sys.argv[1])