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
inference_lock = threading.Lock()

BASE_DIR = os.getcwd()
MODELS_ROOT = os.path.join(BASE_DIR, "models")
SCAN_IGNORED_CHARS_RE = re.compile(r'[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]')
IMAGE_SUBTITLE_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'xsub'}

MODEL_DIR = os.path.join(MODELS_ROOT, "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
VAD_MODEL = os.path.join(MODELS_ROOT, "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")
PUNC_MODEL = os.path.join(MODELS_ROOT, "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch")

VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.m4v', '.ts', '.mts', '.m2ts', '.vob', '.mpg', '.mpeg',
    '.3gp', '.rmvb', '.dat', '.asf', '.divx'
}


# ===================================================

class ScannerCore:
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

    def verify_integrity(self, path):
        if not os.path.exists(path) or os.path.getsize(path) < 1024: return False
        res = self.run_cmd(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
             path], timeout=30)
        return float(res.stdout.strip()) > 0 if res and res.stdout.strip() else False

    def check_keywords(self, text, keywords):
        hit_words = self.find_keywords(text, keywords)
        if hit_words:
            self.log(f"💥 [音频违规] 命中: {', '.join(hit_words)}")
            return True, f"命中: {', '.join(hit_words)}"
        return False, None

    def extract_audio(self, video, start, duration, output, map_arg="0:a:0"):
        cmd = ['ffmpeg', '-ss', str(start), '-t', str(duration), '-i', video,
               '-map', map_arg, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', output]
        res = self.run_cmd(cmd, timeout=120)
        return res is not None and res.returncode == 0

    def clean_transcription(self, text):
        if not text: return ""
        text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
        text = re.sub(r'[🎼♪♫♬♭♮♯😡😔]', '', text)
        return text.strip()

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

        tail_dur = LEN_TAIL_LONG if duration >= TH_LONG else LEN_TAIL
        tasks.append({"start": max(0, duration - tail_dur), "duration": tail_dur, "name": "片尾"})
        if duration > TH_MULTI:
            tasks.append({"start": max(0, (duration / 2) - (LEN_MID / 2)), "duration": LEN_MID, "name": "中间"})
            tasks.append({"start": 0, "duration": LEN_HEAD, "name": "片头"})

        temp_audio = f"/tmp/scan_{task_id}.wav"

        for i, task in enumerate(tasks):
            if self._stopped: return False, None

            if passed_segments and task['name'] in passed_segments:
                self.log(f"⏭️ [断点] 跳过: {task['name']}")
                self.prog_cb(50 + ((i + 1) * 15), f"跳过: {task['name']}", "")
                continue

            self.log(f"✂️ 提取音频 [{task['name']}]: {task['start']:.1f}s - {task['duration']}s")
            if not self.extract_audio(file_path, task['start'], task['duration'], temp_audio, map_arg=audio_map):
                if self._stopped: return False, None
                raise RuntimeError(f"音频提取失败: {task['name']}")

            cloud_success = False
            try:
                self.log(f"☁️ 云端识别中...")
                files = {"file": open(temp_audio, "rb")}
                data = {"model": config.get('api_model'), "language": "zh", "response_format": "json"}
                headers = {"Authorization": f"Bearer {config.get('api_key')}"}
                resp = requests.post(config.get('api_url'), headers=headers, files=files, data=data, timeout=(10, 60))

                if resp.status_code == 200:
                    text = self.clean_transcription(resp.json().get('text', ''))
                    hit, reason = self.check_keywords(text, audio_keywords)
                    if hit:
                        self.log(f"☁️ [违规] 内容: {text}")
                        if os.path.exists(temp_audio): os.remove(temp_audio)
                        return True, reason

                    if config.get('detailed_mode'):
                        self.log(f"✅ [通过] 内容: {text}")
                    else:
                        self.log("✅ 云端识别通过")
                    cloud_success = True
                else:
                    c = config.get('current_retry', 1);
                    m = config.get('retry_limit', 3)
                    self.log(f"⚠️ 云端 API 报错 (第{c}/{m}次): {resp.status_code}")

            except Exception as e:
                c = config.get('current_retry', 1);
                m = config.get('retry_limit', 3)
                self.log(f"⚠️ 云端连接异常 (第{c}/{m}次): {str(e)}")

            if not cloud_success:
                if self._stopped: return False, None

                if not enable_local:
                    if os.path.exists(temp_audio): os.remove(temp_audio)
                    raise RuntimeError(f"云端失败且策略限制本地模型 -> 请求重排队")

                self.log("⏳ 等待本地模型资源锁...")
                with inference_lock:
                    if self._stopped: return False, None

                    self.log("🔒 获得锁，本地推理中...")
                    self.drop_caches()

                    # 🔥 引入 finally 结构，确保 100% 内存回收
                    model = None
                    try:
                        from funasr import AutoModel
                        model = AutoModel(model=MODEL_DIR, vad_model=VAD_MODEL, punc_model=PUNC_MODEL,
                                          disable_update=True, log_level="ERROR")
                        st = time.time();
                        res = model.generate(input=temp_audio);
                        dur = time.time() - st
                        text = self.clean_transcription(res[0]['text'] if res else "")

                        hit, reason = self.check_keywords(text, audio_keywords)
                        if hit:
                            self.log(f"🏠 [违规] 本地内容: {text}")
                            if os.path.exists(temp_audio): os.remove(temp_audio)
                            return True, f"本地拦截: {reason}"

                        if config.get('detailed_mode'):
                            self.log(f"✅ [通过] 本地内容: {text}")
                        else:
                            self.log(f"✅ 本地识别通过 ({dur:.1f}s)")

                    except Exception as e:
                        self.log(f"❌ 本地模型崩溃: {e}")
                        if os.path.exists(temp_audio): os.remove(temp_audio)
                        raise RuntimeError(f"本地模型失败: {e}")
                    finally:
                        # 🔥 [关键修改3] 无论推理成功与否，强制销毁对象并调用 drop_caches
                        if model:
                            del model

                        # 如果有 torch，尝试清空 CUDA 缓存(如果有的话)
                        try:
                            import torch
                            if torch.cuda.is_available(): torch.cuda.empty_cache()
                        except:
                            pass

                        # 调用我们上方定义的、带 malloc_trim 的强力回收函数
                        self.drop_caches()
                        self.log("🧹 [系统] 本地模型内存已强制回收")

            if checkpoint_cb: checkpoint_cb(task['name'])
            self.prog_cb(50 + ((i + 1) * 15), "检测进行中", "")

        if os.path.exists(temp_audio): os.remove(temp_audio)
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
