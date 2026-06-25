import os
import subprocess
import requests
import time
import signal
import json
import shutil
import gc
import re
import threading
import ctypes  # 🔥 [关键修改1] 必须引入这个库才能操作底层内存
import tempfile
from datetime import datetime

try:
    import syslog
except ImportError:
    class _SyslogFallback:
        LOG_PID = 0
        LOG_USER = 0
        LOG_INFO = 0

        @staticmethod
        def openlog(*args, **kwargs):
            pass

        @staticmethod
        def syslog(*args, **kwargs):
            pass

    syslog = _SyslogFallback()

# ================= ⚙️ 核心配置区域 =================
local_inference_condition = threading.Condition()
local_inference_active = 0

BASE_DIR = os.getcwd()
MODELS_ROOT = os.path.join(BASE_DIR, "models")
SCAN_IGNORED_CHARS_RE = re.compile(r'[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]')
IMAGE_SUBTITLE_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'xsub'}

VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.m4v', '.ts', '.mts', '.m2ts', '.vob', '.mpg', '.mpeg',
    '.3gp', '.rmvb', '.dat', '.asf', '.divx'
}


# ===================================================

def get_sensevoice_gguf_paths(base_dir=None):
    root = os.path.join(base_dir or BASE_DIR, "models", "sensevoice-gguf")
    binary_name = "llama-funasr-sensevoice.exe" if os.name == 'nt' else "llama-funasr-sensevoice"
    return {
        'root': root,
        'binary': os.path.join(root, binary_name),
        'model': os.path.join(root, "gguf", "sensevoice-small-q8.gguf"),
        'vad': os.path.join(root, "gguf", "fsmn-vad.gguf"),
    }


def sensevoice_gguf_ready(base_dir=None):
    paths = get_sensevoice_gguf_paths(base_dir)
    checks = [
        (paths['binary'], 1024 * 1024),
        (paths['model'], 100 * 1024 * 1024),
        (paths['vad'], 100 * 1024),
    ]
    for path, min_size in checks:
        try:
            if not os.path.exists(path) or os.path.getsize(path) < min_size:
                return False
            if path == paths['binary'] and os.name != 'nt' and not os.access(path, os.X_OK):
                return False
        except:
            return False
    return True

class ScannerCore:
    CLOUD_ASR_KEY_CONCURRENCY = 2
    CLOUD_ASR_CHUNK_OVERLAP = 2.0
    _cloud_asr_cond = threading.Condition()
    _cloud_asr_active_total = 0
    _cloud_asr_active_by_key = {}
    _cloud_asr_next_key = 0

    def __init__(self, logger_callback=None, progress_callback=None, task_id=None, root_dir_name="downloads",
                 rclone_remote="s25"):
        self.log_cb = logger_callback if logger_callback else print
        self.prog_cb = progress_callback if progress_callback else lambda p, s, e: None
        self.task_id = task_id
        self.root_dir_name = root_dir_name
        self.rclone_remote = rclone_remote
        self.current_proc = None
        self._stopped = False
        try:
            syslog.openlog("arup", syslog.LOG_PID, syslog.LOG_USER)
        except:
            pass

    def log(self, msg):
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_cb(f"[{timestamp}] {msg}")
        try:
            syslog.syslog(syslog.LOG_INFO, msg if not self.task_id else f"[Task-{self.task_id}] {msg}")
        except:
            pass

    def stop(self):
        self._stopped = True
        self.log("🛑 收到停止指令...")

        self._kill_current_proc()

    def _popen_group_kwargs(self):
        if os.name == 'posix':
            return {'preexec_fn': os.setsid}
        if os.name == 'nt':
            flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            return {'creationflags': flags} if flags else {}
        return {}

    def _kill_current_proc(self):
        proc = self.current_proc
        if not proc:
            return
        try:
            if os.name == 'posix':
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except:
            pass

    def get_cloud_asr_concurrency(self, config):
        try:
            return max(1, int(config.get('cloud_asr_concurrency', 3)))
        except:
            return 3

    def get_cloud_api_keys(self, config):
        raw_keys = config.get('cloud_asr_api_keys') or ''
        if isinstance(raw_keys, (list, tuple)):
            candidates = raw_keys
        else:
            candidates = str(raw_keys).splitlines()
        keys = []
        seen = set()
        for key in candidates:
            key = str(key or '').strip()
            if key and key not in seen:
                keys.append(key)
                seen.add(key)
        legacy_key = str(config.get('api_key') or '').strip()
        if not keys and legacy_key:
            keys.append(legacy_key)
        return keys

    def acquire_cloud_asr_slot(self, api_keys, global_limit):
        if not api_keys:
            raise RuntimeError("云端 API Key 未配置")
        cls = type(self)
        per_key_limit = cls.CLOUD_ASR_KEY_CONCURRENCY
        waited = False
        with cls._cloud_asr_cond:
            while not self._stopped:
                if cls._cloud_asr_active_total < global_limit:
                    for offset in range(len(api_keys)):
                        idx = (cls._cloud_asr_next_key + offset) % len(api_keys)
                        key = api_keys[idx]
                        if cls._cloud_asr_active_by_key.get(key, 0) < per_key_limit:
                            cls._cloud_asr_next_key = (idx + 1) % len(api_keys)
                            cls._cloud_asr_active_total += 1
                            cls._cloud_asr_active_by_key[key] = cls._cloud_asr_active_by_key.get(key, 0) + 1
                            return key
                if not waited:
                    self.log(f"⏳ 等待云端模型并发槽... (全局上限 {global_limit}, Key数 {len(api_keys)}, 单Key上限 {per_key_limit})")
                    waited = True
                cls._cloud_asr_cond.wait(timeout=1)
        return None

    def release_cloud_asr_slot(self, api_key):
        if not api_key:
            return
        cls = type(self)
        with cls._cloud_asr_cond:
            cls._cloud_asr_active_total = max(0, cls._cloud_asr_active_total - 1)
            current = cls._cloud_asr_active_by_key.get(api_key, 0) - 1
            if current > 0:
                cls._cloud_asr_active_by_key[api_key] = current
            else:
                cls._cloud_asr_active_by_key.pop(api_key, None)
            cls._cloud_asr_cond.notify_all()

    def run_cmd(self, cmd, timeout=300, capture=True):
        if self._stopped: return None
        try:
            self.current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='ignore', **self._popen_group_kwargs()
            )
            stdout, stderr = self.current_proc.communicate(timeout=timeout)
            result = subprocess.CompletedProcess(cmd, self.current_proc.returncode, stdout, stderr)
            if result.returncode != 0 and capture and not self._stopped:
                self.log(f"⚠️ 命令失败 ({cmd[0]}, code={result.returncode})")
                detail = (stderr or stdout or '').strip()
                if detail:
                    self.log(f"↳ {detail[-1200:]}")
            return result
        except subprocess.TimeoutExpired:
            self.log(f"⚠️ 命令超时 ({timeout}s)")
            self._kill_current_proc()
            return None
        except Exception as e:
            if not self._stopped: self.log(f"命令出错: {e}")
            return None
        finally:
            self.current_proc = None

    def send_tg_msg(self, config, msg):
        token = config.get('tg_bot_token')
        chat_id = config.get('tg_chat_id')
        if token and chat_id:
            try:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat_id, "text": msg}, timeout=10)
            except:
                pass

    # 🔥 [关键修改2] 彻底的内存释放函数
    def drop_caches(self):
        try:
            # 1. 清理 Python 对象垃圾
            gc.collect()

            # 2. 🔥【核心】强制 C 语言层面的内存管理器归还物理内存给系统
            # 如果没有这一步，top 命令里的 RES/RSS 内存占用很难降下来
            try:
                libc = ctypes.CDLL("libc.so.6")
                libc.malloc_trim(0)
            except:
                pass

            # 3. 清理系统层面的 PageCache (辅助)
            subprocess.run(['sync'])
            with open('/proc/sys/vm/drop_caches', 'w') as f:
                f.write('3')
        except:
            pass

    def cleanup_empty_dirs(self, file_path):
        try:
            parent_dir = os.path.dirname(file_path)
            if not os.listdir(parent_dir):
                os.rmdir(parent_dir)
                self.log(f"🗑️ 已删除空目录: {os.path.basename(parent_dir)}")
                grand_parent = os.path.dirname(parent_dir)
                if os.path.basename(grand_parent) != self.root_dir_name:
                    if not os.listdir(grand_parent): os.rmdir(grand_parent)
        except:
            pass

    def normalize_scan_text(self, text):
        if not text: return ""
        return SCAN_IGNORED_CHARS_RE.sub('', str(text)).lower()

    def find_keywords(self, text, keywords):
        if not text or not keywords: return []
        scan_text = self.normalize_scan_text(text)
        hit_words = []
        for kw in keywords:
            normalized_kw = self.normalize_scan_text(kw)
            if normalized_kw and normalized_kw in scan_text:
                hit_words.append(kw)
        return hit_words

    def get_audio_streams(self, file_path):
        res = self.run_cmd(
            ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index,codec_name', '-of',
             'json', file_path], timeout=30)
        if not res or res.returncode != 0 or not res.stdout:
            return None
        try:
            data = json.loads(res.stdout)
        except Exception as e:
            self.log(f"⚠️ 音频流解析失败: {e}")
            return None

        streams = []
        for stream in data.get('streams', []):
            index = stream.get('index')
            if index is None:
                continue
            streams.append({
                'index': str(index),
                'codec': (stream.get('codec_name') or '').strip().lower()
            })
        return streams

    def is_copyable_audio_stream(self, stream):
        codec = stream.get('codec')
        return bool(codec and codec not in ('unknown', 'none'))

    def get_safe_audio_map_args(self, file_path):
        streams = self.get_audio_streams(file_path)
        if streams is None:
            self.log("⚠️ 音频流探测失败，退回默认音频映射")
            return ['-map', '0:a?']

        args = []
        skipped = []
        for stream in streams:
            if self.is_copyable_audio_stream(stream):
                args.extend(['-map', f"0:{stream['index']}"])
            else:
                skipped.append(f"#{stream['index']}({stream.get('codec') or 'unknown'})")

        if skipped:
            self.log(f"⚠️ 跳过无法复制的音频流: {', '.join(skipped)}")
        if streams and not args:
            self.log("⚠️ 未发现可复制音频流，输出将不包含音频")
        return args

    def get_subtitle_streams(self, file_path):
        res = self.run_cmd(
            ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries',
             'stream=index,codec_name:stream_tags=language,title,handler_name', '-of', 'json', file_path], timeout=30)
        if not res or res.returncode != 0 or not res.stdout:
            return None
        try:
            data = json.loads(res.stdout)
        except Exception as e:
            self.log(f"⚠️ 字幕流解析失败: {e}")
            return None

        streams = []
        for stream in data.get('streams', []):
            index = stream.get('index')
            if index is None:
                continue
            tags = stream.get('tags') or {}
            streams.append({
                'index': str(index),
                'codec': (stream.get('codec_name') or '').strip().lower(),
                'language': tags.get('language') or '',
                'title': tags.get('title') or '',
                'handler_name': tags.get('handler_name') or ''
            })
        return streams

    def is_text_subtitle_stream(self, stream):
        codec = stream.get('codec') or ''
        return codec not in IMAGE_SUBTITLE_CODECS

    def subtitle_metadata_text(self, stream):
        return "\n".join([
            stream.get('codec') or '',
            stream.get('language') or '',
            stream.get('title') or '',
            stream.get('handler_name') or ''
        ])

    def extract_subtitle_texts(self, source, streams):
        if not streams: return {}
        timeout = max(120, min(300, 30 + len(streams) * 5))
        texts = {}
        with tempfile.TemporaryDirectory(prefix='subscan_') as tmp_dir:
            outputs = {}
            cmd = ['ffmpeg', '-v', 'error', '-y', '-i', source]
            for stream in streams:
                idx = stream['index']
                output = os.path.join(tmp_dir, f"sub_{idx}.vtt")
                outputs[idx] = output
                cmd.extend(['-map', f'0:{idx}', '-f', 'webvtt', output])

            res = self.run_cmd(cmd, timeout=timeout)
            if not res:
                return texts

            for idx, output in outputs.items():
                if not os.path.exists(output):
                    continue
                try:
                    with open(output, 'r', encoding='utf-8', errors='ignore') as f:
                        texts[idx] = f.read()
                except Exception as e:
                    self.log(f"⚠️ 字幕轨 #{idx} 读取失败: {e}")
        return texts

    def get_smart_audio_map(self, file_path):
        try:
            streams = self.get_audio_streams(file_path)
            if streams:
                streams = [s for s in streams if self.is_copyable_audio_stream(s)]
                if streams and 'flac' in streams[0]['codec'] and len(streams) > 1:
                    second = streams[1]['index']
                    self.log(f"⚠️ 首选音轨为 FLAC，自动切换至 Stream #{second}")
                    return f"0:{second}"
                if streams:
                    return f"0:{streams[0]['index']}"
        except:
            pass
        return "0:a:0"

    def get_media_duration(self, path):
        res = self.run_cmd(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
             path], timeout=30)
        try:
            return float(res.stdout.strip()) if res and res.stdout.strip() else 0
        except:
            return 0

    def verify_integrity(self, path):
        if not os.path.exists(path) or os.path.getsize(path) < 1024: return False
        return self.get_media_duration(path) > 0

    def verify_audio_segment(self, path, min_duration=1.0):
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            return False
        return self.get_media_duration(path) >= min_duration

    def check_keywords(self, text, keywords):
        hit_words = self.find_keywords(text, keywords)
        if hit_words:
            self.log(f"💥 [音频违规] 命中: {', '.join(hit_words)}")
            return True, f"命中: {', '.join(hit_words)}"
        return False, None

    def extract_audio(self, video, start, duration, output, map_arg="0:a:0", min_duration=1.0):
        cmd = ['ffmpeg', '-ss', str(start), '-t', str(duration), '-i', video,
               '-map', map_arg, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', output]
        res = self.run_cmd(cmd, timeout=120)
        if not res or res.returncode != 0:
            return False
        if not self.verify_audio_segment(output, min_duration=min_duration):
            self.log(f"⚠️ 提取音频无有效时长: {os.path.basename(output)}")
            self.remove_audio_cache(output)
            return False
        return True

    def get_cloud_flac_path(self, audio_path):
        base, _ = os.path.splitext(audio_path)
        return f"{base}_cloud.flac"

    def prepare_cloud_audio(self, audio_path, use_flac=False, quiet=False):
        if not self.verify_audio_segment(audio_path):
            if not quiet:
                self.log("⚠️ ASR 音频无有效时长，跳过云端上传")
            return None, None

        if not use_flac:
            return audio_path, None

        cloud_path = self.get_cloud_flac_path(audio_path)
        try:
            src_size = os.path.getsize(audio_path)
            src_mtime = os.path.getmtime(audio_path)
        except:
            return audio_path, None

        try:
            if (os.path.exists(cloud_path) and self.verify_audio_segment(cloud_path)
                    and os.path.getmtime(cloud_path) >= src_mtime):
                if not quiet:
                    self.log(f"♻️ 复用 FLAC 音频: {os.path.basename(cloud_path)} ({os.path.getsize(cloud_path) / 1048576:.1f}MB)")
                return cloud_path, 'audio/flac'
        except:
            pass

        self.remove_audio_cache(cloud_path)
        cmd = ['ffmpeg', '-i', audio_path, '-vn', '-acodec', 'flac', '-y', cloud_path]
        res = self.run_cmd(cmd, timeout=120)
        try:
            cloud_size = os.path.getsize(cloud_path)
        except:
            cloud_size = 0

        if res and res.returncode == 0 and self.verify_audio_segment(cloud_path):
            if not quiet:
                self.log(f"☁️ ASR 音频源: FLAC 无损 ({src_size / 1048576:.1f}MB -> {cloud_size / 1048576:.1f}MB)")
            return cloud_path, 'audio/flac'

        self.remove_audio_cache(cloud_path)
        if not quiet:
            self.log("⚠️ FLAC 生成失败，回退使用 WAV 音频源")
        return audio_path, None

    def extract_cloud_audio_chunk(self, audio_path, start, duration, output):
        cmd = ['ffmpeg', '-ss', str(start), '-t', str(duration), '-i', audio_path,
               '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', output]
        res = self.run_cmd(cmd, timeout=120)
        min_duration = min(1.0, max(0.2, float(duration) * 0.5))
        if res and res.returncode == 0 and self.verify_audio_segment(output, min_duration=min_duration):
            return True
        self.remove_audio_cache(output, self.get_cloud_flac_path(output))
        return False

    def get_audio_cache_paths(self, task_id, segment_name):
        label = str(segment_name)
        prefix_match = re.match(r'(\d+)', label)
        prefix = prefix_match.group(1) if prefix_match else ''
        if '片头' in label:
            safe_segment = f"{prefix}_head" if prefix else 'head'
        elif '片尾' in label:
            safe_segment = f"{prefix}_tail" if prefix else 'tail'
        elif '中间' in label:
            safe_segment = f"{prefix}_middle" if prefix else 'middle'
        elif '抽样' in label:
            m = re.search(r'(\d+)', label)
            safe_segment = f"{prefix}_sample" if prefix else (f"sample_{m.group(1)}" if m else None)
        else:
            safe_segment = None
        safe_segment = safe_segment or re.sub(r'[^a-zA-Z0-9_-]+', '_', label).strip('_')
        if not safe_segment:
            safe_segment = 'segment'
        task_part = str(task_id) if task_id is not None else 'manual'
        base = os.path.join(tempfile.gettempdir(), f"scan_{task_part}_{safe_segment}")
        return f"{base}.wav", f"{base}.json"

    def get_audio_cache_meta(self, video, task, map_arg):
        try:
            size = os.path.getsize(video)
            mtime = int(os.path.getmtime(video))
        except:
            size = 0
            mtime = 0
        return {
            'source': os.path.abspath(video),
            'size': size,
            'mtime': mtime,
            'segment': task.get('name'),
            'start': round(float(task.get('start', 0)), 3),
            'duration': round(float(task.get('duration', 0)), 3),
            'map': map_arg
        }

    def can_reuse_audio_cache(self, audio_path, meta_path, expected_meta, min_duration=1.0):
        if not os.path.exists(audio_path) or not os.path.exists(meta_path):
            return False
        try:
            if not self.verify_audio_segment(audio_path, min_duration=min_duration):
                return False
            with open(meta_path, 'r', encoding='utf-8') as f:
                return json.load(f) == expected_meta
        except:
            return False

    def write_audio_cache_meta(self, meta_path, meta):
        try:
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
        except:
            pass

    def remove_audio_cache(self, *paths):
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except:
                pass

    def clean_transcription(self, text):
        if not text: return ""
        text = re.sub(r'<\|[^|]*\|>', '', text)
        text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
        text = re.sub(r'[🎼♪♫♬♭♮♯😡😔]', '', text)
        return text.strip()

    def extract_local_asr_text(self, output):
        if not output:
            return ""
        lines = [line.strip() for line in str(output).splitlines() if line.strip()]
        filtered = []
        noise_prefixes = ('main:', 'ggml_', 'llama_', 'build:', 'system_info:', 'load_', 'init:', '[sensevoice]')
        for line in lines:
            lower = line.lower()
            if lower.startswith(noise_prefixes):
                continue
            filtered.append(line)
        return "\n".join(filtered or lines).strip()

    def run_local_sensevoice_gguf(self, audio_path, segment_duration):
        paths = get_sensevoice_gguf_paths()
        if not sensevoice_gguf_ready():
            raise RuntimeError("本地 GGUF 模型资源缺失，请在设置页下载")

        timeout = max(300, int(float(segment_duration or 0) * 3) + 120)
        cmd = [paths['binary'], '-m', paths['model'], '--vad', paths['vad'], '-a', audio_path]
        res = self.run_cmd(cmd, timeout=timeout)
        if not res or res.returncode != 0:
            raise RuntimeError("GGUF 推理命令失败")
        text = self.extract_local_asr_text((res.stdout or '') + "\n" + (res.stderr or ''))
        text = self.clean_transcription(text)
        if not text:
            raise RuntimeError("GGUF 推理无识别文本")
        return text

    def get_retry_attempt_label(self, config):
        try:
            current = int(config.get('current_retry', 1))
            retry_limit = int(config.get('retry_limit', 3))
        except:
            current = 1
            retry_limit = 3
        total = max(1, retry_limit + 1, current)
        return current, total

    def get_local_model_concurrency(self, config):
        try:
            return max(1, min(8, int(config.get('local_model_concurrency', 1))))
        except:
            return 1

    def acquire_local_inference_slot(self, limit):
        global local_inference_active
        with local_inference_condition:
            while local_inference_active >= limit:
                if self._stopped:
                    return 0
                local_inference_condition.wait(timeout=1)
            local_inference_active += 1
            return local_inference_active

    def release_local_inference_slot(self):
        global local_inference_active
        with local_inference_condition:
            local_inference_active = max(0, local_inference_active - 1)
            local_inference_condition.notify_all()
            return local_inference_active

    def sanitize_metadata(self, source, meta_keywords):
        if source.lower().endswith('.rmvb'): return
        self.log("🧹 [检测] 检查元数据标签...")
        res_format = self.run_cmd(['ffprobe', '-v', 'error', '-show_entries', 'format_tags', '-of', 'csv=p=0', source],
                                  timeout=30)
        res_stream = self.run_cmd(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream_tags=language,title,handler_name', '-of', 'csv=p=0',
             source],
            timeout=30
        )

        scan_text = ""
        if res_format and res_format.stdout:
            scan_text += res_format.stdout + "\n"
        if res_stream and res_stream.stdout:
            scan_text += res_stream.stdout
        hit_words = self.find_keywords(scan_text, meta_keywords)

        if hit_words:
            self.log(f"🚫 发现敏感标签: {hit_words} -> 执行清洗...")
            dir_name = os.path.dirname(source);
            name, ext = os.path.splitext(os.path.basename(source))
            output = os.path.join(dir_name, f"{name}_clean_meta{ext}")
            cmd = ['ffmpeg', '-err_detect', 'ignore_err', '-i', source, '-map', '0:v:0']
            cmd.extend(self.get_safe_audio_map_args(source))
            cmd.extend(['-map', '0:s?', '-c', 'copy', '-dn', '-ignore_unknown', '-strict', '-2', '-map_metadata', '-1',
                   '-metadata', 'title=', '-metadata', 'comment=',
                   '-metadata', 'description=', '-metadata', 'synopsis=',
                   '-metadata', 'artist=', '-metadata', 'album=', '-metadata', 'copyright=',
                   '-metadata:s', 'title=', '-metadata:s', 'language=und', '-metadata:s', 'handler_name=',
                   '-y', output])
            res = self.run_cmd(cmd, timeout=300)
            if res and res.returncode == 0 and self.verify_integrity(output):
                shutil.move(output, source);
                self.log("✅ 元数据已清洗")
            else:
                if os.path.exists(output): os.remove(output)

    def check_subtitles(self, source, sub_keywords):
        if not sub_keywords: return None
        started_at = time.time()
        self.log(f"📝 [检测] 分析字幕内容...")
        streams = self.get_subtitle_streams(source)
        if not streams: return None

        all_idxs = [stream['index'] for stream in streams]
        dirty_idxs = set()
        image_count = 0

        for stream in streams:
            idx = stream['index']
            hit_words = self.find_keywords(self.subtitle_metadata_text(stream), sub_keywords)
            if hit_words:
                self.log(f"🚫 字幕轨 #{idx} 元数据命中: {', '.join(hit_words)}")
                dirty_idxs.add(idx)

        text_streams = []
        for stream in streams:
            if not self.is_text_subtitle_stream(stream):
                image_count += 1
                continue
            if stream['index'] not in dirty_idxs:
                text_streams.append(stream)

        self.log(f"ℹ️ 字幕轨 {len(streams)} 条，待扫文本轨 {len(text_streams)} 条，图片轨 {image_count} 条")
        subtitle_texts = self.extract_subtitle_texts(source, text_streams)
        for stream in text_streams:
            idx = stream['index']
            text = subtitle_texts.get(idx, '')
            if not text:
                continue
            hit_words = self.find_keywords(text, sub_keywords)
            if hit_words:
                self.log(f"🚫 字幕轨 #{idx} 内容命中: {', '.join(hit_words)}")
                dirty_idxs.add(idx)

        self.log(f"⏱️ 字幕分析完成: {len(streams)}轨/命中{len(dirty_idxs)}轨，用时 {time.time() - started_at:.1f}s")

        if dirty_idxs:
            self.log(f"🧹 剔除违规字幕...")
            dir_name = os.path.dirname(source);
            name, ext = os.path.splitext(os.path.basename(source))
            output = os.path.join(dir_name, f"{name}_clean{ext}")
            cmd = ['ffmpeg', '-err_detect', 'ignore_err', '-i', source, '-map', '0:v:0']
            cmd.extend(self.get_safe_audio_map_args(source))
            for idx in all_idxs:
                if idx not in dirty_idxs: cmd.extend(['-map', f'0:{idx}'])
            cmd.extend(['-c', 'copy', '-dn', '-ignore_unknown', '-y', output])
            res = self.run_cmd(cmd, timeout=300)
            if res and res.returncode == 0 and self.verify_integrity(output):
                os.remove(source);
                self.log(f"✅ 字幕清洗完成");
                return output
            else:
                if os.path.exists(output): os.remove(output)
        return None

    def scan_audio_cloud_fallback_local(self, file_path, duration, task_id, audio_map, audio_keywords, enable_local,
                                        config, passed_segments=None, checkpoint_cb=None):
        tasks = []
        TH_MULTI = config.get('audio_threshold_multi', 600)
        TH_LONG = config.get('audio_threshold_long', 3600)
        LEN_HEAD = config.get('audio_len_head', 240);
        LEN_MID = config.get('audio_len_mid', 240)
        LEN_TAIL = config.get('audio_len_tail', 300);
        LEN_TAIL_LONG = config.get('audio_len_tail_long', 600)
        try:
            CLOUD_MAX_DURATION = max(0, int(config.get('cloud_asr_max_duration', 60)))
        except:
            CLOUD_MAX_DURATION = 60

        if config.get('audio_double_sample') and duration > max(1, LEN_MID):
            sample_count = 6
            sample_len = max(1, LEN_MID)
            max_start = max(0, duration - sample_len)
            sample_points = [
                (idx, (max_start * idx) / (sample_count - 1) if sample_count > 1 else max_start)
                for idx in range(sample_count)
            ]
            ordered_points = [sample_points[0], sample_points[-1]] + sample_points[1:-1]
            for order_idx, (_, start) in enumerate(ordered_points, 1):
                if order_idx == 1:
                    name = "01片头"
                elif order_idx == 2:
                    name = "02片尾"
                else:
                    name = f"{order_idx:02d}抽样"
                tasks.append({"start": start, "duration": sample_len, "name": name})
            self.log(f"🎚️ 双倍抽样开启: {sample_count}段 x {sample_len}s，顺序 01片头 -> 02片尾 -> 03-06抽样")
        else:
            tail_dur = LEN_TAIL_LONG if duration >= TH_LONG else LEN_TAIL
            tasks.append({"start": max(0, duration - tail_dur), "duration": tail_dur, "name": "片尾"})
            if duration > TH_MULTI:
                tasks.append({"start": max(0, (duration / 2) - (LEN_MID / 2)), "duration": LEN_MID, "name": "中间"})
                tasks.append({"start": 0, "duration": LEN_HEAD, "name": "片头"})

        audio_progress_base = 50
        audio_progress_span = 45

        def audio_progress_pct(index):
            return int(audio_progress_base + ((index + 1) * audio_progress_span / max(1, len(tasks))))

        for i, task in enumerate(tasks):
            if self._stopped: return False, None

            if passed_segments and task['name'] in passed_segments:
                self.log(f"⏭️ [断点] 跳过: {task['name']}")
                self.prog_cb(audio_progress_pct(i), f"跳过: {task['name']}", "")
                continue

            temp_audio, temp_meta = self.get_audio_cache_paths(task_id, task['name'])
            min_audio_duration = min(5.0, max(1.0, float(task['duration']) * 0.05))
            extract_tasks = [task]
            if '片尾' in str(task['name']) and task['start'] > 0:
                fallback_start = max(0, task['start'] - task['duration'])
                if fallback_start < task['start']:
                    fallback_task = dict(task)
                    fallback_task['start'] = fallback_start
                    extract_tasks.append(fallback_task)

            cache_meta = None
            reused = False
            for cache_idx, cache_task in enumerate(extract_tasks):
                candidate_meta = self.get_audio_cache_meta(file_path, cache_task, audio_map)
                if self.can_reuse_audio_cache(temp_audio, temp_meta, candidate_meta, min_duration=min_audio_duration):
                    cache_meta = candidate_meta
                    reused = True
                    if cache_idx > 0:
                        self.log(f"♻️ 复用回退音频 [{task['name']}]: {os.path.basename(temp_audio)}")
                    else:
                        self.log(f"♻️ 复用音频 [{task['name']}]: {os.path.basename(temp_audio)}")
                    break

            if not reused:
                cache_meta = self.get_audio_cache_meta(file_path, task, audio_map)
                self.remove_audio_cache(temp_audio, temp_meta, self.get_cloud_flac_path(temp_audio))

                extracted = False
                for extract_idx, extract_task in enumerate(extract_tasks):
                    if extract_idx > 0:
                        self.remove_audio_cache(temp_audio, self.get_cloud_flac_path(temp_audio))
                        self.log(f"↩️ 音频片尾为空，向前重试: {extract_task['start']:.1f}s - {extract_task['duration']}s")
                    else:
                        self.log(f"✂️ 提取音频 [{task['name']}]: {extract_task['start']:.1f}s - {extract_task['duration']}s")

                    if self.extract_audio(file_path, extract_task['start'], extract_task['duration'], temp_audio,
                                          map_arg=audio_map, min_duration=min_audio_duration):
                        cache_meta = self.get_audio_cache_meta(file_path, extract_task, audio_map)
                        extracted = True
                        break

                if not extracted:
                    self.remove_audio_cache(temp_audio, temp_meta, self.get_cloud_flac_path(temp_audio))
                    if self._stopped: return False, None
                    raise RuntimeError(f"音频提取失败: {task['name']}")
                self.write_audio_cache_meta(temp_meta, cache_meta)

            cloud_success = False
            cloud_audio = None
            cloud_artifacts = []
            cloud_chunked = False
            try:
                if not config.get('enable_cloud_asr', True):
                    self.log("☁️ 云端 API 已停用，跳过云端识别")
                else:
                    read_timeout = 180 if task['duration'] >= 450 else 120
                    data = {"model": config.get('api_model'), "language": "zh", "response_format": "json"}
                    api_keys = self.get_cloud_api_keys(config)
                    cloud_global_limit = self.get_cloud_asr_concurrency(config)

                    def submit_cloud_audio(source_audio, label=None, log_request=True):
                        nonlocal cloud_audio
                        cloud_audio, cloud_mime = self.prepare_cloud_audio(source_audio, config.get('asr_use_flac'), quiet=not log_request)
                        if not cloud_audio:
                            raise RuntimeError("ASR 音频无有效时长")
                        if cloud_audio != source_audio:
                            cloud_artifacts.append(cloud_audio)
                        cloud_size = os.path.getsize(cloud_audio) if os.path.exists(cloud_audio) else 0
                        source_type = 'FLAC' if cloud_mime else 'WAV'
                        label_text = f" [{label}]" if label else ""
                        if log_request:
                            self.log(f"☁️ 云端识别中{label_text}... (source={source_type}, timeout={read_timeout}s, size={cloud_size / 1048576:.1f}MB)")
                        api_key = self.acquire_cloud_asr_slot(api_keys, cloud_global_limit)
                        if not api_key:
                            raise RuntimeError("云端识别已停止")
                        try:
                            headers = {"Authorization": f"Bearer {api_key}"}
                            with open(cloud_audio, "rb") as f:
                                if cloud_mime:
                                    files = {"file": (os.path.basename(cloud_audio), f, cloud_mime)}
                                else:
                                    files = {"file": f}
                                return requests.post(config.get('api_url'), headers=headers, files=files, data=data,
                                                     timeout=(10, read_timeout))
                        finally:
                            self.release_cloud_asr_slot(api_key)

                    actual_audio_duration = self.get_media_duration(temp_audio) or float(task['duration'])
                    if CLOUD_MAX_DURATION and actual_audio_duration > CLOUD_MAX_DURATION:
                        cloud_chunked = True
                        chunks = []
                        chunk_start = 0.0
                        chunk_overlap = min(type(self).CLOUD_ASR_CHUNK_OVERLAP, max(0.0, float(CLOUD_MAX_DURATION) - 1.0))
                        chunk_step = max(1.0, float(CLOUD_MAX_DURATION) - chunk_overlap)
                        while chunk_start < actual_audio_duration - 0.5:
                            chunk_duration = min(float(CLOUD_MAX_DURATION), actual_audio_duration - chunk_start)
                            if chunk_duration < 1.0 and chunks:
                                break
                            chunks.append((chunk_start, chunk_duration))
                            chunk_start += chunk_step

                        self.log(f"☁️ 云端分块识别: {actual_audio_duration:.1f}s -> {len(chunks)}段，每段≤{CLOUD_MAX_DURATION}s，重叠{chunk_overlap:.0f}s")
                        cloud_success = True
                        chunk_statuses = []
                        chunk_base, _ = os.path.splitext(temp_audio)
                        for chunk_idx, (chunk_start, chunk_duration) in enumerate(chunks, 1):
                            chunk_audio = f"{chunk_base}_cloud_part{chunk_idx:02d}.wav"
                            chunk_flac = self.get_cloud_flac_path(chunk_audio)
                            cloud_artifacts.extend([chunk_audio, chunk_flac])
                            self.remove_audio_cache(chunk_audio, chunk_flac)
                            if not self.extract_cloud_audio_chunk(temp_audio, chunk_start, chunk_duration, chunk_audio):
                                chunk_statuses.append(f"{chunk_idx}/{len(chunks)}提取失败")
                                self.log(f"⚠️ 云端分块失败: {' '.join(chunk_statuses)}")
                                raise RuntimeError(f"云端分块音频提取失败: {chunk_idx}/{len(chunks)}")
                            resp = submit_cloud_audio(chunk_audio, f"{chunk_idx}/{len(chunks)}", log_request=False)
                            if resp.status_code != 200:
                                c, m = self.get_retry_attempt_label(config)
                                chunk_statuses.append(f"{chunk_idx}/{len(chunks)}×{resp.status_code}")
                                self.log(f"⚠️ 云端分块失败 (第{c}/{m}次): {' '.join(chunk_statuses)}")
                                cloud_success = False
                                break

                            text = self.clean_transcription(resp.json().get('text', ''))
                            hit, reason = self.check_keywords(text, audio_keywords)
                            if hit:
                                hit_text = text or '<空>'
                                chunk_statuses.append(f"{chunk_idx}/{len(chunks)} 命中 {hit_text}")
                                self.log(f"☁️ 云端分块命中: {' '.join(chunk_statuses)}")
                                self.log(f"☁️ [违规] 分块 {chunk_idx}/{len(chunks)} 内容: {text}")
                                self.remove_audio_cache(temp_audio, temp_meta, cloud_audio, *cloud_artifacts)
                                return True, reason
                            if config.get('detailed_mode'):
                                chunk_text = text or '<空>'
                                chunk_statuses.append(f"{chunk_idx}/{len(chunks)} {chunk_text}")
                            else:
                                chunk_statuses.append(f"{chunk_idx}/{len(chunks)}✓")
                        if cloud_success:
                            self.log(f"✅ 云端分块识别通过: {' '.join(chunk_statuses)}")
                    else:
                        resp = submit_cloud_audio(temp_audio)
                        if resp.status_code == 200:
                            text = self.clean_transcription(resp.json().get('text', ''))
                            hit, reason = self.check_keywords(text, audio_keywords)
                            if hit:
                                self.log(f"☁️ [违规] 内容: {text}")
                                self.remove_audio_cache(temp_audio, temp_meta, cloud_audio, *cloud_artifacts)
                                return True, reason

                            if config.get('detailed_mode'):
                                self.log(f"✅ [通过] 内容: {text}")
                            else:
                                self.log("✅ 云端识别通过")
                            cloud_success = True
                        else:
                            c, m = self.get_retry_attempt_label(config)
                            self.log(f"⚠️ 云端 API 报错 (第{c}/{m}次): {resp.status_code}")

            except Exception as e:
                c, m = self.get_retry_attempt_label(config)
                self.log(f"⚠️ 云端连接异常 (第{c}/{m}次): {str(e)}")

            if not cloud_success:
                if self._stopped: return False, None
                if cloud_chunked:
                    self.remove_audio_cache(cloud_audio, *cloud_artifacts)

                if not enable_local:
                    if not config.get('enable_cloud_asr', True):
                        self.remove_audio_cache(temp_audio, temp_meta, cloud_audio, *cloud_artifacts)
                        raise RuntimeError("云端 API 已停用且本地模型未启用")
                    try:
                        has_retry = int(config.get('current_retry', 1)) <= int(config.get('retry_limit', 3))
                    except:
                        has_retry = True
                    if has_retry:
                        self.log(f"♻️ 保留音频供重试复用: {task['name']}")
                    else:
                        self.remove_audio_cache(temp_audio, temp_meta, cloud_audio, *cloud_artifacts)
                    raise RuntimeError(f"云端失败且策略限制本地模型 -> 请求重排队")

                local_limit = self.get_local_model_concurrency(config)
                self.log(f"⏳ 等待本地模型资源槽... (并发上限 {local_limit})")
                active_slots = self.acquire_local_inference_slot(local_limit)
                if not active_slots:
                    return False, None
                try:
                    if self._stopped: return False, None

                    self.log(f"🔒 获得本地模型资源槽 ({active_slots}/{local_limit})，本地 GGUF 推理中...")
                    self.drop_caches()

                    try:
                        st = time.time();
                        text = self.run_local_sensevoice_gguf(temp_audio, task['duration'])
                        dur = time.time() - st

                        hit, reason = self.check_keywords(text, audio_keywords)
                        if hit:
                            self.log(f"🏠 [违规] 本地内容: {text}")
                            self.remove_audio_cache(temp_audio, temp_meta, cloud_audio, *cloud_artifacts)
                            return True, f"本地拦截: {reason}"

                        if config.get('detailed_mode'):
                            self.log(f"✅ [通过] 本地内容: {text}")
                        else:
                            self.log(f"✅ 本地识别通过 ({dur:.1f}s)")

                    except Exception as e:
                        self.log(f"❌ 本地模型崩溃: {e}")
                        self.remove_audio_cache(temp_audio, temp_meta, cloud_audio, *cloud_artifacts)
                        raise RuntimeError(f"本地模型失败: {e}")
                finally:
                    self.drop_caches()
                    remaining_slots = self.release_local_inference_slot()
                    self.log(f"🧹 [系统] 本地 GGUF 推理资源已释放 (运行中 {remaining_slots}/{local_limit})")

            self.remove_audio_cache(temp_audio, temp_meta, cloud_audio, *cloud_artifacts)
            if checkpoint_cb: checkpoint_cb(task['name'])
            self.prog_cb(audio_progress_pct(i), "检测进行中", "")

        return False, None

    def process_file(self, file_path, config, keywords_config, passed_segments=None, checkpoint_cb=None,
                     rename_cb=None):
        if self._stopped: return {"status": "cancelled"}
        try:
            if config.get('direct_upload'):
                self.log("⏩ 直传模式")
                return {"status": "ready_to_upload"}
            if not os.path.exists(file_path):
                return {"status": "error", "msg": "文件不存在"}

            current_path = file_path

            _, ext = os.path.splitext(current_path)
            if ext.lower() not in VIDEO_EXTENSIONS:
                self.log(f"⏩ 非视频文件 ({ext}) -> 跳过检测，直接上传")
                return {"status": "ready_to_upload"}

            if self._stopped: return {"status": "cancelled"}
            if config.get('sanitize_metadata'): self.sanitize_metadata(current_path, keywords_config.get('meta', []))
            self.prog_cb(10, "元数据处理完毕", "")

            if self._stopped: return {"status": "cancelled"}
            if config.get('check_subtitles'):
                new_path = self.check_subtitles(current_path, keywords_config.get('subtitle', []))
                if new_path:
                    current_path = new_path
                    if rename_cb: rename_cb(current_path)
            self.prog_cb(30, "字幕处理完毕", "")

            if self._stopped: return {"status": "cancelled"}
            if config.get('check_audio'):
                self.drop_caches();
                self.log("🔍 准备音频检测...");
                self.prog_cb(40, "准备音频检测", "")
                d_res = self.run_cmd(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of',
                                      'default=noprint_wrappers=1:nokey=1', current_path])
                duration = float(d_res.stdout) if d_res and d_res.stdout else 0
                if duration > 0:
                    map_arg = self.get_smart_audio_map(current_path)
                    hit, reason = self.scan_audio_cloud_fallback_local(
                        current_path, duration, self.task_id, map_arg,
                        keywords_config.get('audio', []),
                        config.get('enable_local_model', True),
                        config,
                        passed_segments=passed_segments,
                        checkpoint_cb=checkpoint_cb
                    )

                    if self._stopped:
                        self.log("🛑 检测过程已中断")
                        return {"status": "cancelled"}

                    if hit: return {"status": "dirty", "msg": reason}

            if self._stopped:
                self.log("🛑 最终阶段收到停止指令")
                return {"status": "cancelled"}

            self.log("✅ 全流程通过")
            self.prog_cb(100, "检测完成", "")
            return {"status": "ready_to_upload", "new_filepath": current_path if current_path != file_path else None}

        except Exception as e:
            err_str = str(e)
            if "请求重排队" in err_str:
                self.log(f"⚠️ {err_str}")
            else:
                self.log(f"❌ 流程中断: {e}")
            return {"status": "error", "msg": err_str}

    def upload_with_progress(self, local_path, remote_path=None):
        if self._stopped: return False
        if not remote_path:
            filename = os.path.basename(local_path)
            parent_dir = os.path.dirname(local_path)
            folder_name = os.path.basename(parent_dir)
            remote_prefix = self.rclone_remote if (folder_name == self.root_dir_name or not folder_name) else folder_name
            remote_path = f"{remote_prefix}:{filename}"

        self.log(f"☁️ 上传: {remote_path}")
        cmd = ['rclone', 'moveto', local_path, remote_path, '--use-json-log', '--stats', '1s', '-v', '--ignore-size','--no-traverse','--drive-chunk-size', '64M']

        try:
            self.current_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                                 encoding='utf-8', errors='ignore', **self._popen_group_kwargs())
            while True:
                if self._stopped: self._kill_current_proc(); return False
                line = self.current_proc.stderr.readline()
                if not line and self.current_proc.poll() is not None: break
                if line:
                    try:
                        data = json.loads(line)
                        if 'stats' in data:
                            st = data['stats'];
                            trans = st.get('transferring', [{}])[0]
                            pct = int((trans.get('bytes', 0) / trans.get('size', 1)) * 100)

                            eta_val = int(st.get('eta', 0))
                            if eta_val > 60:
                                h = eta_val // 3600
                                m = (eta_val % 3600) // 60
                                s = eta_val % 60
                                eta_str = f"{h}h {m}m {s}s" if h > 0 else f"{m}m {s}s"
                            else:
                                eta_str = f"{eta_val}s"

                            self.prog_cb(pct, f"{st.get('speed', 0) / 1048576:.1f} MB/s", eta_str)
                    except:
                        pass
            return self.current_proc.returncode == 0
        except Exception as e:
            self.log(f"上传出错: {e}");
            return False
        finally:
            self.current_proc = None
