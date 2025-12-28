#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import subprocess
import re
import time
import hashlib
import fcntl
from contextlib import contextmanager

# 保持日志级别设置
os.environ["MODELSCOPE_LOG_LEVEL"] = "40"

from pypinyin import lazy_pinyin
from thefuzz import fuzz

# ================= ⚙️ 本地配置 (纯离线版) =================
DEVICE = "cpu"
CPU_THREADS = 4

# 🔥🔥🔥 开启 Debug 模式 (显示完整识别内容) 🔥🔥🔥
DEBUG_MODE = True

# ⚠️⚠️⚠️ 请确认路径正确 ⚠️⚠️⚠️
LOCAL_MODEL_DIR = "/root/models/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_MODEL_ID = "/root/models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
PUNC_MODEL_ID = "/root/models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"

SANITIZE_METADATA = True
# 🔥 新增：字幕检测开关
CHECK_SUBTITLES = True

AUDIO_BLACKLIST = [
    "加群", "交流群", "TG群", "Telegram", "QQ群", "Q群",
    "资源群", "微信号", "微信群", "微信公众号", "关注公众号",
]

SUB_META_BLACKLIST = [
    "http", "www", "weixin", "Telegram", "TG@", "TG频道@",
    "群：", "群:", "资源群", "加群", "微信号", "微信群",
    "QQ", "qq", "q群", "公众号", "微博", "b站", "Tacit0924",
    "Arctime", "Lavf",
    "无人在意做自己", "资源站", "资源网",
    "发布页","荣誉出品", "字幕组", "我堡牛皮",
    "link3.cc", "ysepan.com", "GyWEB", "Qqun", "hehehe", ".com",
    "PTerWEB", "panclub", "BT之家", "CMCT", "Byakuya", "ed3000",
    "yunpantv", "KKYY", "盘酱酱", "TREX", "£yhq@tv", "1000fr",
    "HDCTV", "HHWEB", "ADWeb", "PanWEB", "BestWEB"
]

GLOBAL_TAGS_TO_CHECK = ["genre", "comment", "description", "synopsis", "title", "artist", "album", "copyright"]


# ================= 🛠️ 日志与工具 =================
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


@contextmanager
def suppress_output():
    with open(os.devnull, "w") as devnull:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = devnull, devnull
            yield
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


def write_reason_to_env(reason):
    if os.environ.get("SCAN_REASON_FILE"):
        try:
            with open(os.environ.get("SCAN_REASON_FILE"), "w", encoding="utf-8") as f:
                f.write(reason)
        except:
            pass


def run_cmd(cmd, capture=True, timeout=60):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                              text=True, encoding='utf-8', errors='ignore', timeout=timeout)
    except:
        return None


def verify_file_integrity(file_path):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024: return False
    try:
        res = run_cmd(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'format=duration', '-of',
                       'default=noprint_wrappers=1:nokey=1', file_path], timeout=30)
        return float(res.stdout.strip()) > 0 if res and res.stdout.strip() else False
    except:
        return False


def safe_replace(src, dst):
    try:
        if os.path.exists(dst): os.remove(dst)
        os.rename(src, dst)
        return True
    except OSError as e:
        PrettyLog.error(f"替换失败: {e}"); return False


# ================= 🧹 清洗逻辑 =================
def sanitize_metadata_tags(source):
    if not SANITIZE_METADATA: return False
    clean_needed = False
    for tag in GLOBAL_TAGS_TO_CHECK:
        res = run_cmd(['ffprobe', '-v', 'error', '-show_entries', f'format_tags={tag}', '-of', 'csv=p=0', source],
                      timeout=30)
        if res and res.stdout and any(
            kw.lower() in res.stdout.lower() for kw in SUB_META_BLACKLIST): clean_needed = True; break
    if not clean_needed:
        res = run_cmd(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=index:stream_tags=language,title,handler_name', '-of',
             'csv=p=0', source], timeout=30)
        if res and res.stdout and any(
            kw.lower() in res.stdout.lower() for kw in SUB_META_BLACKLIST): clean_needed = True

    if clean_needed:
        PrettyLog.info("🧹 [Clean] 发现脏标签，正在深度清洗元数据...")
        dir_name = os.path.dirname(source)
        name, ext = os.path.splitext(os.path.basename(source))

        if name.endswith("_clean_meta"):
            name = name[:-11]
        elif name.endswith("_clean"):
            name = name[:-6]

        output_path = os.path.join(dir_name, f"{name}_clean_meta{ext}")
        cmd = ['ffmpeg', '-err_detect', 'ignore_err', '-i', source,
               '-map', '0:v:0', '-map', '0:a?', '-map', '0:s?',
               '-c', 'copy', '-strict', '-2',
               '-dn', '-ignore_unknown', '-map_metadata', '-1',
               '-metadata', 'title=', '-metadata', 'comment=', '-metadata', 'description=', '-metadata', 'copyright=',
               '-metadata:s', 'title=', '-metadata:s', 'language=und', '-metadata:s', 'handler_name=', '-y',
               output_path]

        res = run_cmd(cmd, capture=True, timeout=300)

        if res and res.returncode == 0 and verify_file_integrity(output_path):
            if safe_replace(output_path, source): PrettyLog.success("✨ [Clean] 元数据深度净化"); return True
        else:
            PrettyLog.error("❌ 元数据清洗失败")
            if res and res.stderr:
                err_log = res.stderr.splitlines()[-3:]
                for l in err_log: PrettyLog.warn(f"FFmpeg Error: {l}")

        if os.path.exists(output_path): os.remove(output_path)
    return False


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
        try:
            extract_cmd = ['ffmpeg', '-v', 'error', '-i', source, '-map', f'0:{idx}', '-f', 'webvtt', '-']
            proc = subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                  timeout=120)
            sub_content = proc.stdout

            if not sub_content: continue

            for kw in SUB_META_BLACKLIST:
                if kw in sub_content:
                    PrettyLog.hit(f"字幕轨 [Stream #{idx}] 内容包含: '{kw}'")
                    dirty_indices.append(idx)
                    break

        except subprocess.TimeoutExpired:
            PrettyLog.warn(f"⚠️ 字幕提取超时 (Stream #{idx})，已跳过检查")
            continue
        except Exception as e:
            PrettyLog.warn(f"⚠️ 字幕提取出错: {e}")
            continue

    if not dirty_indices: return None

    PrettyLog.info(f"🧹 [Clean] 正在移除 {len(dirty_indices)} 个违规字幕轨...")
    dir_name = os.path.dirname(source)
    name, ext = os.path.splitext(os.path.basename(source))

    if name.endswith("_clean"):
        final_clean_name = name
    else:
        final_clean_name = f"{name}_clean"

    temp = os.path.join(dir_name, f"{name}_temp_clean{ext}")
    final = os.path.join(dir_name, f"{final_clean_name}{ext}")

    cmd = ['ffmpeg', '-err_detect', 'ignore_err', '-i', source, '-map', '0:v:0', '-map', '0:a?']
    for s_idx in subtitle_indices:
        if s_idx not in dirty_indices: cmd.extend(['-map', f'0:{s_idx}'])

    cmd.extend(['-c', 'copy', '-strict', '-2', '-dn', '-ignore_unknown', '-y', temp])

    if run_cmd(cmd, capture=False, timeout=120) and verify_file_integrity(temp):
        try:
            if os.path.exists(source): os.remove(source)
            if os.path.exists(final): os.remove(final)
            os.rename(temp, final)
            PrettyLog.success(f"✨ [Clean] 违规字幕已移除，重命名为: {os.path.basename(final)}")
            return final
        except:
            pass
    if os.path.exists(temp): os.remove(temp)
    return None


def normalize_text(text):
    if not text: return ""
    text = re.sub(r'<\|.*?\|>', '', text)
    trans = str.maketrans("零一二三四五六七八九", "0123456789")
    return re.sub(r'[^\w\s,.，。？！:：0-9a-zA-Z\u4e00-\u9fa5/\-_.\[\]\(\)]', '', text.translate(trans))


def check_audio_keywords_detail(text):
    if not text: return False, None
    norm = normalize_text(text)
    match = re.search(r'(资源|加群|入群|群号|QQ|TG|VX|微信).{0,12}\d{5,}', norm, re.IGNORECASE)
    if match: return True, f"正则匹配: [{match.group(0)}]"
    for kw in AUDIO_BLACKLIST:
        if kw in norm: return True, f"关键词匹配: {kw}"
    return False, None


def get_duration(file_path):
    res = run_cmd(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
         file_path], timeout=10)
    return float(res.stdout.strip()) if res and res.stdout.strip() else 0


def get_smart_audio_map(file_path):
    try:
        res = run_cmd(
            ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index,codec_name', '-of',
             'csv=p=0', file_path], capture=True)
        if res and res.stdout:
            streams = [{'index': l.split(',')[0], 'codec': l.split(',')[1].strip().lower()} for l in
                       res.stdout.strip().splitlines() if ',' in l]
            if streams and 'flac' in streams[0]['codec'] and len(streams) > 1:
                PrettyLog.warn(f"⚠️ 首选音轨为 FLAC，自动切换至次选: Stream #{streams[1]['index']}")
                return f"0:{streams[1]['index']}"
    except:
        pass
    return "0:a:0"


def extract_audio(video_path, start, duration, output_path, map_arg="0:a:0"):
    cmd = ['ffmpeg', '-ss', str(start), '-t', str(duration), '-i', video_path, '-map', map_arg, '-vn', '-acodec',
           'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', output_path]
    res = run_cmd(cmd, capture=False, timeout=30)
    return res is not None and res.returncode == 0


# ================= 🤖 本地模型 =================
model_instance = None


def init_model():
    global model_instance
    if model_instance is None:
        try:
            PrettyLog.info(f"⏳ 正在加载本地模型 (离线路径: {LOCAL_MODEL_DIR})...")
            if not os.path.exists(LOCAL_MODEL_DIR): raise FileNotFoundError(f"主模型路径不存在: {LOCAL_MODEL_DIR}")
            from funasr import AutoModel
            with suppress_output():
                model_instance = AutoModel(
                    model=LOCAL_MODEL_DIR, vad_model=VAD_MODEL_ID, punc_model=PUNC_MODEL_ID,
                    device=DEVICE, ncpu=CPU_THREADS, disable_update=True, log_level="ERROR"
                )
            PrettyLog.success("本地模型加载完成")
        except Exception as e:
            PrettyLog.fatal(f"模型加载失败: {e}")
            sys.exit(2)


def transcribe_local(audio_path):
    if not os.path.exists(audio_path): return None
    try:
        with suppress_output():
            res = model_instance.generate(input=audio_path, batch_size_s=300)
        return res[0].get("text", "") if res and isinstance(res, list) and len(res) > 0 else ""
    except:
        PrettyLog.error("识别出错")
        return None


def process_single_source(source):
    # 智能寻址
    if not os.path.exists(source):
        dir_name = os.path.dirname(source)
        base_name = os.path.basename(source)
        name, ext = os.path.splitext(base_name)
        possible_clean = os.path.join(dir_name, f"{name}_clean{ext}")
        if os.path.exists(possible_clean):
            PrettyLog.warn(f"⚠️ 原文件不存在，自动切换到已清洗文件: {os.path.basename(possible_clean)}")
            source = possible_clean
        else:
            return

    PrettyLog.step(f"正在分析 (Local): {os.path.basename(source)}")
    sanitize_metadata_tags(source)
    new_source = sanitize_subtitle_content(source)
    if new_source and os.path.exists(new_source):
        source = new_source
        PrettyLog.info(f"🔄 切换后续扫描目标为: {os.path.basename(source)}")

    dur = get_duration(source)
    if dur == 0: sys.exit(0)

    tasks = [{"start": max(0, dur - (600 if dur >= 3600 else 300)), "duration": (600 if dur >= 3600 else 300),
              "name": "片尾优先"}]
    if dur > 600:
        tasks.extend([{"start": (dur / 2) - 120, "duration": 240, "name": "中间抽查"},
                      {"start": 0, "duration": 240, "name": "片头抽查"}])

    init_model()
    audio_map = get_smart_audio_map(source)
    temp_wav = f"/tmp/scan_local_{os.getpid()}_{hashlib.md5(source.encode()).hexdigest()[:8]}.wav"
    hit_reason = None

    for i, task in enumerate(tasks):
        if hit_reason: break
        PrettyLog.info(f"🔍 任务 ({i + 1}/{len(tasks)}): [{task['name']}]")
        if extract_audio(source, task['start'], task['duration'], temp_wav, audio_map):
            text = transcribe_local(temp_wav)
            if text:
                is_hit, reason = check_audio_keywords_detail(text)
                if DEBUG_MODE: PrettyLog.info(f"📝 {text}...")
                if is_hit: hit_reason = f"{task['name']} -> {reason}"
            if os.path.exists(temp_wav): os.remove(temp_wav)
        else:
            PrettyLog.warn("音频提取失败")

    if hit_reason:
        write_reason_to_env(hit_reason)
        PrettyLog.fatal(f"🚫 发现违规音频! 原因: {hit_reason}")
        sys.exit(1)

    PrettyLog.success("✅ [Local] 本地音频内容检测通过 (安全)")
    sys.exit(0)


def main():
    lock_file = open("/tmp/scan_audio_local.lock", "w")
    try:
        PrettyLog.info("⏳ [Queue] 本地模型资源紧张，正在排队等候 (Limit: 1)...")
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        PrettyLog.info("🔓 [Queue] 获取资源锁成功，开始执行")

        if len(sys.argv) < 2: sys.exit(1)
        process_single_source(sys.argv[1])
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    main()