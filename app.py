import os
import json
import shutil
import subprocess
import sys
import threading
import queue
import time
import random
import re
import concurrent.futures
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from database import db, Task, Config, Keyword, User
from core_logic import ScannerCore, sensevoice_gguf_ready
from sqlalchemy import text

app = Flask(__name__)

# ================= 🔐 Session 密钥持久化 =================
secret_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.flask_secret')
if os.path.exists(secret_file):
    try:
        with open(secret_file, 'rb') as f:
            app.secret_key = f.read()
    except:
        app.secret_key = os.urandom(24)
else:
    new_key = os.urandom(24)
    try:
        with open(secret_file, 'wb') as f:
            f.write(new_key)
    except:
        pass
    app.secret_key = new_key
app.permanent_session_lifetime = timedelta(days=30)

# ================= 🔧 基础配置 =================
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tasks.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

APP_VERSION = os.environ.get('APP_VERSION', 'v2026.05.18')

login_manager = LoginManager(app)
login_manager.login_view = 'login'

detect_queue = queue.Queue()
upload_queue = queue.Queue()
running_tasks = {}
active_detect_tasks = set()
active_upload_tasks = set()
task_state_lock = threading.Lock()
download_proc = None;
download_logs = [];
download_lock = threading.Lock()
LOGIN_ATTEMPTS = {}


def claim_task_stage(task_id, stage):
    current_set = active_detect_tasks if stage == 'detect' else active_upload_tasks
    other_set = active_upload_tasks if stage == 'detect' else active_detect_tasks
    with task_state_lock:
        if task_id in current_set or task_id in other_set:
            return False
        current_set.add(task_id)
        return True


def release_task_stage(task_id, stage):
    current_set = active_detect_tasks if stage == 'detect' else active_upload_tasks
    with task_state_lock:
        current_set.discard(task_id)


def clear_running_task(task_id, core):
    with task_state_lock:
        if running_tasks.get(task_id) is core:
            del running_tasks[task_id]


def check_ip_ban(ip):
    now = datetime.now()
    if ip in LOGIN_ATTEMPTS:
        r = LOGIN_ATTEMPTS[ip]
        if r['ban_until']:
            if now < r['ban_until']:
                return True, int((r['ban_until'] - now).total_seconds() / 60)
            else:
                LOGIN_ATTEMPTS.pop(ip)
    return False, 0


def record_login_fail(ip):
    now = datetime.now()
    if ip not in LOGIN_ATTEMPTS: LOGIN_ATTEMPTS[ip] = {'count': 0, 'ban_until': None}
    LOGIN_ATTEMPTS[ip]['count'] += 1
    if LOGIN_ATTEMPTS[ip]['count'] >= 3:
        LOGIN_ATTEMPTS[ip]['ban_until'] = now + timedelta(minutes=60)
        print(f"🚫 IP {ip} 封禁 60 分钟")


def reset_login_fail(ip):
    if ip in LOGIN_ATTEMPTS: LOGIN_ATTEMPTS.pop(ip)


@login_manager.user_loader
def load_user(user_id): return User.query.get(user_id)


AUDIO_BLACKLIST_INIT = ["加群", "交流群", "TG群", "Telegram", "QQ群", "Q群", "资源群", "微信号", "微信群", "微信公众号","加群","关注公众号",
                        "群36", "资源区"]
SUBTITLE_BLACKLIST_INIT = ["加群", "交流群", "微信号", "微信群", "QQ", "qq", "q群", "公众号", "网址", ".com", "Q群","http",
                           "www", "link3.cc", "ysepan.com", "Tacit0924", "资源群"]
SUB_META_BLACKLIST_INIT = ["http", "www", "weixin", "Telegram", "TG@", "TG频道@", "群：", "群:", "资源群", "加群",
                           "微信号", "微信群", "QQ", "qq", "q群", "公众号", "微博", "b站", "资源站", "资源网", "发布页",
                           "荣誉出品", "link3.cc", "ysepan.com", "GyWEB", "Qqun", "hehehe", ".com", "PTerWEB",
                           "panclub", "BT之家", "CMCT", "Byakuya", "ed3000", "yunpantv", "KKYY", "盘酱酱", "TREX",
                           "£yhq@tv", "1000fr", "HDCTV", "HHWEB", "ADWeb", "PanWEB", "BestWEB", "hanWEB", "it.com",
                           "Mandarin", "HDSky", "HDsky", "Feibanyama"]


def seed_default_keywords():
    try:
        for kw in AUDIO_BLACKLIST_INIT:
            if not Keyword.query.filter_by(type='audio', content=kw).first(): db.session.add(
                Keyword(type='audio', content=kw, enabled=True))
        for kw in SUBTITLE_BLACKLIST_INIT:
            if not Keyword.query.filter_by(type='subtitle', content=kw).first(): db.session.add(
                Keyword(type='subtitle', content=kw, enabled=True))
        for kw in SUB_META_BLACKLIST_INIT:
            if not Keyword.query.filter_by(type='meta', content=kw).first(): db.session.add(
                Keyword(type='meta', content=kw, enabled=True))
        db.session.commit()
    except:
        pass


def get_final_config(overrides_json=None):
    final_conf = {
        "check_audio": True, "check_subtitles": True, "sanitize_metadata": True, "enable_cloud_asr": True,
        "enable_local_model": False, "detailed_mode": False, "asr_use_flac": False, "audio_double_sample": False,
        "tg_bot_token": "", "tg_chat_id": "",
        "audio_threshold_multi": 600, "audio_threshold_long": 3600,
        "audio_len_head": 240, "audio_len_mid": 240, "audio_len_tail": 300, "audio_len_tail_long": 600,
        "api_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
        "api_key": "", "cloud_asr_api_keys": "",
        "api_model": "FunAudioLLM/SenseVoiceSmall", "cloud_asr_max_duration": 60, "cloud_asr_concurrency": 3,
        "scan_path": "/root/downloads", "rclone_remote": "s25", "api_token": "8pUoqOTHhEAhRnacl3c19",
        "notify_upload_success": False, "notify_errors": True,
        "concurrency_detect": 2, "concurrency_upload": 9, "detect_retry_limit": 3,
        "local_model_concurrency": 1, "download_proxy": ""
    }
    db_configs = {c.key: c.value for c in Config.query.all()}
    for k, v in db_configs.items():
        if k in ["check_audio", "check_subtitles", "sanitize_metadata", "enable_cloud_asr", "enable_local_model", "detailed_mode", "asr_use_flac", "audio_double_sample",
                 "notify_upload_success", "notify_errors"]:
            final_conf[k] = (str(v).lower() == 'true')
        elif k in ["audio_threshold_multi", "audio_threshold_long", "audio_len_head", "audio_len_mid", "audio_len_tail",
                   "audio_len_tail_long", "cloud_asr_max_duration", "cloud_asr_concurrency", "concurrency_detect", "concurrency_upload", "detect_retry_limit",
                   "local_model_concurrency"]:
            try:
                final_conf[k] = int(v)
            except:
                pass
        else:
            final_conf[k] = v
    if overrides_json:
        try:
            ov = json.loads(overrides_json)
            for k, v in ov.items():
                if v is not None:
                    if k in final_conf and isinstance(final_conf[k], bool):
                        final_conf[k] = (str(v).lower() == 'true' or v is True)
                    elif k in final_conf and isinstance(final_conf[k], int):
                        try:
                            final_conf[k] = int(v)
                        except:
                            pass
                    else:
                        final_conf[k] = v
        except:
            pass
    return final_conf


def get_task_overrides(task):
    try:
        if not task or not task.overrides:
            return {}
        data = json.loads(task.overrides)
        return data if isinstance(data, dict) else {}
    except:
        return {}


def set_task_overrides(task, data):
    task.overrides = json.dumps(data) if data else None


def update_task_overrides(task, patch=None, remove_keys=None):
    data = get_task_overrides(task)
    if patch:
        data.update(patch)
    if remove_keys:
        for key in remove_keys:
            data.pop(key, None)
    set_task_overrides(task, data)
    return data


def replace_public_task_overrides(task, new_values):
    private_data = {k: v for k, v in get_task_overrides(task).items() if str(k).startswith('_')}
    if new_values:
        private_data.update(new_values)
    set_task_overrides(task, private_data)
    return private_data


def is_directory_task(task, overrides=None):
    ov = overrides if overrides is not None else get_task_overrides(task)
    return bool(ov.get('_dir_task'))


def list_directory_task_files(root_path):
    files = []
    if not root_path or not os.path.isdir(root_path):
        return files

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith('.aria2'):
                continue
            full_path = os.path.join(dirpath, name)
            if os.path.isfile(full_path):
                files.append(full_path)
    return files


def resolve_directory_task_path(file_path, file_count, scan_path):
    if not file_path or file_count <= 1:
        return file_path

    abs_path = os.path.abspath(file_path)
    abs_scan = os.path.abspath(scan_path.rstrip('/\\'))
    candidate = os.path.dirname(abs_path)

    while candidate and candidate.startswith(abs_scan):
        count = 0
        try:
            for dirpath, dirnames, filenames in os.walk(candidate):
                dirnames.sort()
                for name in filenames:
                    if name.endswith('.aria2'):
                        continue
                    count += 1
                    if count >= file_count:
                        return candidate
        except:
            break

        if os.path.normpath(candidate) == os.path.normpath(abs_scan):
            break
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent

    return os.path.dirname(abs_path)


def build_directory_remote_path(root_path, file_path, root_dir_name, default_remote):
    normalized_root = root_path.rstrip('/\\')
    root_name = os.path.basename(normalized_root)
    parent_name = os.path.basename(os.path.dirname(normalized_root))
    remote_prefix = default_remote if (parent_name == root_dir_name or not parent_name) else parent_name
    rel_path = os.path.relpath(file_path, root_path).replace(os.sep, '/')
    remote_rel = f"{root_name}/{rel_path}" if rel_path and rel_path != '.' else root_name
    return remote_prefix, f"{remote_prefix}:{remote_rel}"


def get_next_persistent_id():
    c = Config.query.filter_by(key='sys_task_counter').first()
    if not c:
        current = 0;
        c = Config(key='sys_task_counter', value='0');
        db.session.add(c)
    else:
        try:
            current = int(c.value)
        except:
            current = 0
    next_id = current + 1
    if next_id > 9999: next_id = 1
    existing = Task.query.get(next_id)
    if existing:
        if next_id in running_tasks: running_tasks[next_id].stop(); del running_tasks[next_id]
        db.session.delete(existing);
        db.session.commit()
    c.value = str(next_id);
    db.session.add(c);
    db.session.commit()
    return next_id


# ----------------- Worker Functions -----------------
def detection_worker():
    with app.app_context():
        seed_default_keywords()
        while True:
            try:
                task_id = detect_queue.get()
                if not claim_task_stage(task_id, 'detect'):
                    detect_queue.task_done()
                    continue
                task = Task.query.get(task_id)
                if not task or task.status != 'pending':
                    release_task_stage(task_id, 'detect')
                    detect_queue.task_done()
                    continue
                task.status = 'processing';
                task.progress = 0;
                db.session.commit()

                final_settings = get_final_config(task.overrides)

                try:
                    RETRY_LIMIT = max(0, int(final_settings.get('detect_retry_limit', 3)))
                except:
                    RETRY_LIMIT = 3
                user_local_pref = final_settings.get('enable_local_model', False)
                cloud_enabled = final_settings.get('enable_cloud_asr', True)

                final_settings['current_retry'] = task.retry_count + 1
                final_settings['retry_limit'] = RETRY_LIMIT

                if not cloud_enabled:
                    final_settings['enable_local_model'] = user_local_pref
                elif task.retry_count < RETRY_LIMIT:
                    final_settings['enable_local_model'] = False
                else:
                    final_settings['enable_local_model'] = user_local_pref

                scan_path = final_settings.get('scan_path', '/root/downloads')
                rclone_remote = final_settings.get('rclone_remote', 's25')
                current_root_name = os.path.basename(scan_path.rstrip('/'))

                audio_kws = [k.content for k in Keyword.query.filter_by(type='audio', enabled=True).all()]
                sub_kws = [k.content for k in Keyword.query.filter_by(type='subtitle', enabled=True).all()]
                meta_kws = [k.content for k in Keyword.query.filter_by(type='meta', enabled=True).all()]
                keywords_config = {'audio': audio_kws, 'subtitle': sub_kws, 'meta': meta_kws}

                def db_logger(msg):
                    t = Task.query.get(task_id);
                    if t: t.log = (t.log or "") + f"{msg}\n"; db.session.commit()

                task_overrides = get_task_overrides(task)
                dir_task = is_directory_task(task, task_overrides)
                current_process_path = task.filepath
                passed_segments = []

                if dir_task:
                    remaining_files = list_directory_task_files(task.filepath)
                    if not remaining_files:
                        total_files = max(1, int(task_overrides.get('_dir_total_files', 1) or 1))
                        uploaded_count = int(task_overrides.get('_dir_uploaded_count', 0) or 0)
                        task.status = 'uploaded' if uploaded_count >= total_files else 'error'
                        task.progress = 100 if task.status == 'uploaded' else 0
                        task.upload_eta = "完成" if task.status == 'uploaded' else "-"
                        task.finished_at = datetime.now()
                        db_logger("✅ 目录任务已完成" if task.status == 'uploaded' else "❌ 目录任务中未找到可处理文件")
                        detect_queue.task_done()
                        continue

                    current_process_path = remaining_files[0]
                    if task_overrides.get('_current_item') != current_process_path or task_overrides.get('_dir_stage') != 'detect':
                        task_overrides = update_task_overrides(
                            task,
                            {'_current_item': current_process_path, '_dir_stage': 'detect'},
                            remove_keys=['_passed', '_passed_file']
                        )
                        db.session.commit()
                    if task_overrides.get('_passed_file') == current_process_path:
                        passed_segments = task_overrides.get('_passed', [])
                else:
                    passed_segments = task_overrides.get('_passed', [])

                def detect_prog(pct, msg, _):
                    t = Task.query.get(task_id);
                    if not t:
                        return
                    if dir_task:
                        ov = get_task_overrides(t)
                        total_files = max(1, int(ov.get('_dir_total_files', 1) or 1))
                        uploaded_count = int(ov.get('_dir_uploaded_count', 0) or 0)
                        t.progress = int(min(99, ((uploaded_count + (pct / 100.0) * 0.5) / total_files) * 100))
                    else:
                        t.progress = pct
                    db.session.commit()

                def save_checkpoint(seg_name):
                    try:
                        t = Task.query.get(task_id)
                        ov = get_task_overrides(t)
                        current_item = ov.get('_current_item') if dir_task else current_process_path
                        passed = ov.get('_passed', [])
                        if seg_name not in passed:
                            passed.append(seg_name)
                            ov['_passed'] = passed
                            ov['_passed_file'] = current_item
                            set_task_overrides(t, ov)
                            db.session.commit()
                    except:
                        pass

                def update_filepath(new_path):
                    try:
                        t = Task.query.get(task_id)
                        if not t or not new_path:
                            return
                        if dir_task:
                            ov = update_task_overrides(t, {'_current_item': new_path})
                            if ov.get('_passed_file') and ov.get('_passed_file') != new_path:
                                ov['_passed_file'] = new_path
                                set_task_overrides(t, ov)
                            t.log = (t.log or "") + f"🔄 当前文件已更新为: {os.path.basename(new_path)}\n"
                        elif t.filepath != new_path:
                            t.filepath = new_path
                            t.filename = os.path.basename(new_path)
                            t.log = (t.log or "") + f"🔄 文件已更新为: {t.filename}\n"
                        db.session.commit()
                    except:
                        pass

                core = ScannerCore(logger_callback=db_logger, task_id=task_id, root_dir_name=current_root_name,
                                   rclone_remote=rclone_remote)
                core.prog_cb = detect_prog
                with task_state_lock:
                    running_tasks[task_id] = core

                try:
                    res = core.process_file(
                        current_process_path, final_settings, keywords_config,
                        passed_segments=passed_segments,
                        checkpoint_cb=save_checkpoint,
                        rename_cb=update_filepath
                    )

                    if res['status'] == 'cancelled':
                        task.status = 'cancelled'
                        task.finished_at = datetime.now()
                        db_logger("⏹ 任务已手动停止")
                    elif res['status'] == 'dirty':
                        task.status = 'dirty';
                        task.finished_at = datetime.now()
                        if dir_task:
                            update_task_overrides(task, remove_keys=['_passed', '_passed_file'])
                            db_logger(f"🚫 命中文件: {os.path.basename(current_process_path)}")
                        elif os.path.exists(task.filepath):
                            os.remove(task.filepath)
                        if final_settings.get('notify_errors', True): core.send_tg_msg(final_settings,
                                                                                      f"🚫 拦截: {task.filename}\n原因: {res['msg']}")
                    elif res['status'] == 'ready_to_upload':
                        if dir_task:
                            current_upload_path = res.get('new_filepath') or get_task_overrides(task).get('_current_item') or current_process_path
                            update_task_overrides(task, {'_current_item': current_upload_path, '_dir_stage': 'upload'}, remove_keys=['_passed', '_passed_file'])
                        elif res.get('new_filepath'):
                            task.filepath = res['new_filepath'];
                            task.filename = os.path.basename(res['new_filepath'])
                        task.status = 'pending_upload';
                        if dir_task:
                            ov = get_task_overrides(task)
                            total_files = max(1, int(ov.get('_dir_total_files', 1) or 1))
                            uploaded_count = int(ov.get('_dir_uploaded_count', 0) or 0)
                            task.progress = int(min(99, ((uploaded_count + 0.5) / total_files) * 100))
                            db_logger(f"✅ 文件检测通过，加入上传队列: {os.path.basename(current_upload_path)}")
                        else:
                            task.progress = 0
                            db_logger("✅ 检测通过，加入上传队列")
                        db.session.commit();
                        upload_queue.put(task_id)
                    else:
                        err_msg = str(res.get('msg', ''))
                        if task.retry_count < RETRY_LIMIT and '云端 API 已停用且本地模型未启用' not in err_msg:
                            task.retry_count += 1
                            task.status = 'pending'
                            db_logger(f"⚠️ 云端异常/超时 -> 重新排队 (尝试 {task.retry_count}/{RETRY_LIMIT})")
                            db.session.commit()
                            detect_queue.put(task_id)
                        else:
                            task.status = 'error';
                            task.finished_at = datetime.now();
                            db_logger(f"❌ 最终失败: 本地模型也无法处理 (或未启用)")
                            if final_settings.get('notify_errors', True): core.send_tg_msg(final_settings,
                                                                                           f"❌ 任务出错: {task.filename}\n原因: {res.get('msg')}")

                except Exception as e:
                    if task.retry_count < RETRY_LIMIT:
                        task.retry_count += 1
                        task.status = 'pending'
                        db_logger(f"⚠️ 异常 -> 重新排队 (尝试 {task.retry_count}/{RETRY_LIMIT})\nErr: {str(e)}")
                        db.session.commit()
                        detect_queue.put(task_id)
                    else:
                        task.status = 'error';
                        task.finished_at = datetime.now();
                        db_logger(f"❌ 最终异常: {e}")
                        if final_settings.get('notify_errors', True): core.send_tg_msg(final_settings,
                                                                                       f"❌ 系统异常: {task.filename}")
                finally:
                    clear_running_task(task_id, core)
                    release_task_stage(task_id, 'detect')
                    db.session.commit();
                    detect_queue.task_done()
            except Exception as e:
                print(e)


def upload_worker():
    with app.app_context():
        while True:
            try:
                task_id = upload_queue.get()
                if not claim_task_stage(task_id, 'upload'):
                    upload_queue.task_done()
                    continue
                task = Task.query.get(task_id)
                if not task or task.status != 'pending_upload':
                    release_task_stage(task_id, 'upload')
                    upload_queue.task_done()
                    continue
                task.status = 'uploading';
                db.session.commit()

                final_settings = get_final_config(task.overrides)
                scan_path = final_settings.get('scan_path', '/root/downloads')
                rclone_remote = final_settings.get('rclone_remote', 's25')
                current_root_name = os.path.basename(scan_path.rstrip('/'))
                task_overrides = get_task_overrides(task)
                dir_task = is_directory_task(task, task_overrides)
                current_upload_path = task_overrides.get('_current_item') if dir_task else task.filepath

                if dir_task and (not current_upload_path or not os.path.exists(current_upload_path)):
                    remaining_files = list_directory_task_files(task.filepath)
                    if remaining_files:
                        current_upload_path = remaining_files[0]
                        update_task_overrides(task, {'_current_item': current_upload_path, '_dir_stage': 'upload'}, remove_keys=['_passed', '_passed_file'])
                        db.session.commit()

                if dir_task and not current_upload_path:
                    task.status = 'error'
                    task.finished_at = datetime.now()
                    task.log = (task.log or "") + "❌ 上传失败：目录任务未找到待上传文件\n"
                    db.session.commit()
                    upload_queue.task_done()
                    continue

                if dir_task:
                    dest_remote, remote_path = build_directory_remote_path(task.filepath, current_upload_path, current_root_name,
                                                                          rclone_remote)
                else:
                    folder_name = os.path.basename(os.path.dirname(task.filepath))
                    dest_remote = rclone_remote if (folder_name == current_root_name or not folder_name) else folder_name
                    remote_path = None

                def db_logger(msg):
                    try:
                        t = Task.query.get(task_id);
                        if t: t.log = (t.log or "") + f"{msg}\n"; db.session.commit()
                    except:
                        pass

                def upload_prog(pct, speed, eta):
                    try:
                        t = Task.query.get(task_id);
                        if not t:
                            return
                        if dir_task:
                            ov = get_task_overrides(t)
                            total_files = max(1, int(ov.get('_dir_total_files', 1) or 1))
                            uploaded_count = int(ov.get('_dir_uploaded_count', 0) or 0)
                            t.progress = int(min(99, ((uploaded_count + 0.5 + (pct / 100.0) * 0.5) / total_files) * 100))
                        else:
                            t.progress = pct
                        t.upload_speed = speed; t.upload_eta = eta; db.session.commit()
                    except:
                        pass

                core = ScannerCore(logger_callback=db_logger, task_id=task_id, root_dir_name=current_root_name,
                                   rclone_remote=rclone_remote)
                core.prog_cb = upload_prog
                with task_state_lock:
                    running_tasks[task_id] = core
                try:
                    if core.upload_with_progress(current_upload_path, remote_path=remote_path):
                        if dir_task:
                            core.cleanup_empty_dirs(current_upload_path)
                            ov = update_task_overrides(
                                task,
                                {
                                    '_dir_uploaded_count': int(get_task_overrides(task).get('_dir_uploaded_count', 0) or 0) + 1,
                                    '_dir_stage': 'detect'
                                },
                                remove_keys=['_current_item', '_passed', '_passed_file']
                            )
                            total_files = max(1, int(ov.get('_dir_total_files', 1) or 1))
                            uploaded_count = int(ov.get('_dir_uploaded_count', 0) or 0)
                            remaining_files = list_directory_task_files(task.filepath)
                            task.upload_speed = ""
                            if not remaining_files:
                                task.status = 'uploaded';
                                task.progress = 100;
                                task.upload_eta = "完成";
                                task.finished_at = datetime.now();
                                db_logger("✅ 目录任务上传完成")
                                if os.path.isdir(task.filepath):
                                    shutil.rmtree(task.filepath, ignore_errors=True)
                                if final_settings.get('notify_upload_success', False): core.send_tg_msg(
                                    final_settings,
                                    f"🎉 上传成功: {task.filename}\n☁️ 节点: {dest_remote}"
                                )
                            else:
                                task.status = 'pending'
                                task.progress = int(min(99, (uploaded_count / total_files) * 100))
                                task.upload_eta = "-"
                                task.finished_at = None
                                db_logger(f"✅ 文件上传成功 ({uploaded_count}/{total_files})，继续处理下一文件")
                                db.session.commit()
                                detect_queue.put(task_id)
                        else:
                            task.status = 'uploaded';
                            task.progress = 100;
                            task.upload_eta = "完成";
                            task.finished_at = datetime.now();
                            db_logger("✅ 上传成功");
                            core.cleanup_empty_dirs(task.filepath)
                            if final_settings.get('notify_upload_success', False): core.send_tg_msg(final_settings,
                                                                                                    f"🎉 上传成功: {task.filename}\n☁️ 节点: {dest_remote}")
                    else:
                        # 🔥🔥🔥 修复逻辑：检查是“失败”还是“手动停止”
                        if core._stopped:
                            # 仅仅记录停止日志，不要报错，不要发通知
                            db_logger("⏹ 上传已停止/删除")
                        elif task.status != 'cancelled':
                            task.status = 'error';
                            task.finished_at = datetime.now();
                            db_logger(f"❌ 上传失败{': ' + os.path.basename(current_upload_path) if dir_task and current_upload_path else ''}")
                            if final_settings.get('notify_errors', True): core.send_tg_msg(final_settings,
                                                                                           f"❌ 上传失败: {task.filename}")
                except Exception as e:
                    if core._stopped:
                        db_logger(f"⏹ 上传中断: {e}")
                    else:
                        task.status = 'error';
                        db_logger(f"上传异常: {e}")
                        if final_settings.get('notify_errors', True): core.send_tg_msg(final_settings,
                                                                                       f"❌ 上传异常: {task.filename}")
                finally:
                    clear_running_task(task_id, core)
                    release_task_stage(task_id, 'upload')
                    try:
                        db.session.commit()
                    except:
                        pass
                    upload_queue.task_done()
            except Exception as e:
                print(e)


# ----------------- Model & System Routes -----------------
def check_local_models_exist():
    return sensevoice_gguf_ready(os.getcwd())


@app.route('/api/model/download', methods=['POST'])
@login_required
def download_model():
    global download_proc, download_logs
    sys_conf = get_final_config(None);
    proxy_url = sys_conf.get('download_proxy', '')
    with download_lock:
        if download_proc and download_proc.poll() is None: return jsonify({"code": 409, "msg": "下载任务正在进行"})
        download_logs = ["=== 🚀 初始化 GGUF 本地模型资源下载 ==="]
        env = os.environ.copy()
        if proxy_url: env['HTTP_PROXY'] = proxy_url; env['HTTPS_PROXY'] = proxy_url
        script = """
import json, os, platform, shutil, stat, subprocess, sys, tarfile, tempfile, time, urllib.request, zipfile

ROOT = os.path.join(os.getcwd(), 'models', 'sensevoice-gguf')
GGUF_DIR = os.path.join(ROOT, 'gguf')
RUNTIME_VERSION = 'runtime-llamacpp-v0.1.2'
os.makedirs(GGUF_DIR, exist_ok=True)

def log(msg):
    print(msg, flush=True)

def download(url, dest, min_size=1):
    if os.path.exists(dest) and os.path.getsize(dest) >= min_size:
        log(f"✅ 已存在，跳过: {os.path.basename(dest)}")
        return
    tmp = dest + '.part'
    for attempt in range(1, 6):
        try:
            log(f"⬇️ 下载: {os.path.basename(dest)} (尝试 {attempt}/5)")
            req = urllib.request.Request(url, headers={'User-Agent': 'scanner-web/gguf-downloader'})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, 'wb') as f:
                shutil.copyfileobj(r, f)
            if os.path.getsize(tmp) < min_size:
                raise RuntimeError('下载文件过小')
            os.replace(tmp, dest)
            log(f"✅ 完成: {os.path.basename(dest)} ({os.path.getsize(dest) / 1048576:.1f}MB)")
            return
        except Exception as e:
            try:
                if os.path.exists(tmp): os.remove(tmp)
            except: pass
            log(f"⚠️ 下载失败: {str(e).splitlines()[0]}")
            time.sleep(5)
    raise RuntimeError(f"下载失败: {url}")

def normalized_machine():
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        return 'x64'
    if machine in ('aarch64', 'arm64'):
        return 'arm64'
    raise RuntimeError(f'暂不支持的平台架构: {platform.system().lower()}/{machine}')

def runtime_asset_name():
    system = platform.system().lower()
    machine = normalized_machine()
    if system == 'linux':
        return 'funasr-llamacpp-linux-arm64.tar.gz' if machine == 'arm64' else 'funasr-llamacpp-linux-x64.tar.gz'
    if system == 'darwin' and machine == 'arm64':
        return 'funasr-llamacpp-macos-arm64.tar.gz'
    if system == 'windows' and machine == 'x64':
        return 'funasr-llamacpp-windows-x64.zip'
    raise RuntimeError(f'暂不支持的平台: {system}/{platform.machine().lower()}')

def is_linux():
    return platform.system().lower() == 'linux'

def runtime_build_id():
    return f'{RUNTIME_VERSION} GGML_NATIVE=OFF linux/{normalized_machine()}'

def runtime_marker_path():
    return os.path.join(ROOT, f'runtime-source-build-linux-{normalized_machine()}.txt')

def stream_cmd(cmd, cwd=None, timeout=None):
    log('$ ' + ' '.join(cmd))
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:
            line = line.strip()
            if line:
                log(line)
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError('命令超时: ' + ' '.join(cmd))
    if rc != 0:
        raise RuntimeError(f'命令失败({rc}): ' + ' '.join(cmd))

def build_runtime_from_source():
    build_id = runtime_build_id()
    marker_path = runtime_marker_path()
    binary_name = 'llama-funasr-sensevoice'
    binary_path = os.path.join(ROOT, binary_name)
    if os.path.exists(marker_path) and os.path.exists(binary_path) and os.path.getsize(binary_path) > 1024 * 1024:
        with open(marker_path, 'r', encoding='utf-8') as f:
            marker = f.read().strip()
        if marker == build_id:
            log(f'✅ Linux 本机编译 runtime 已存在，跳过编译 ({normalized_machine()})')
            return
        log(f'⚠️ runtime 架构标记不匹配，将重新编译: {marker or "empty"}')

    if os.path.exists(binary_path) and not os.path.exists(marker_path):
        log('⚠️ 发现旧 runtime 但缺少本机编译标记，将重新编译以适配当前 CPU/GLIBC')

    missing = [tool for tool in ('git', 'cmake', 'c++') if not shutil.which(tool)]
    if missing:
        raise RuntimeError('Linux 服务器需要本机编译 runtime，缺少工具: ' + ', '.join(missing))

    log(f'🛠️ Linux {normalized_machine()} 开始本机编译 GGML_NATIVE=OFF')
    with tempfile.TemporaryDirectory(prefix='sensevoice_runtime_build_') as tmpdir:
        repo = os.path.join(tmpdir, 'SenseVoice')
        stream_cmd(['git', 'clone', '--depth', '1', '--branch', RUNTIME_VERSION,
                    'https://github.com/FunAudioLLM/SenseVoice.git', repo], timeout=300)
        runtime_dir = os.path.join(repo, 'runtime', 'llama.cpp')
        stream_cmd(['cmake', '-B', 'build', '-DCMAKE_BUILD_TYPE=Release', '-DGGML_NATIVE=OFF', '-DLLAMA_CURL=OFF'],
                   cwd=runtime_dir, timeout=300)
        stream_cmd(['cmake', '--build', 'build', '-j', '2', '--target', 'llama-funasr-sensevoice'],
                   cwd=runtime_dir, timeout=1200)
        built = os.path.join(runtime_dir, 'build', 'bin', binary_name)
        if not os.path.exists(built):
            raise RuntimeError('编译完成但未找到 llama-funasr-sensevoice')
        shutil.copy2(built, binary_path)
        os.chmod(binary_path, os.stat(binary_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with open(marker_path, 'w', encoding='utf-8') as f:
            f.write(build_id + '\\n')
        log(f'✅ Linux 本机编译 runtime 编译完成 ({normalized_machine()})')

def latest_runtime_url(asset_name):
    try:
        req = urllib.request.Request('https://api.github.com/repos/FunAudioLLM/SenseVoice/releases/latest', headers={'User-Agent': 'scanner-web/gguf-downloader'})
        with urllib.request.urlopen(req, timeout=30) as r:
            release = json.load(r)
        for asset in release.get('assets', []):
            if asset.get('name') == asset_name:
                return asset.get('browser_download_url')
    except Exception as e:
        log(f"⚠️ 获取最新 release 失败，使用固定版本: {str(e).splitlines()[0]}")
    return f'https://github.com/FunAudioLLM/SenseVoice/releases/download/{RUNTIME_VERSION}/{asset_name}'

def extract_runtime(archive_path):
    log('📦 解压 llama.cpp runtime...')
    with tempfile.TemporaryDirectory(prefix='sensevoice_runtime_') as tmpdir:
        if archive_path.endswith('.zip'):
            with zipfile.ZipFile(archive_path) as z:
                z.extractall(tmpdir)
        else:
            with tarfile.open(archive_path, 'r:gz') as t:
                t.extractall(tmpdir)

        binary_name = 'llama-funasr-sensevoice.exe' if platform.system().lower() == 'windows' else 'llama-funasr-sensevoice'
        script_name = 'download-funasr-model.sh'
        found_binary = None
        found_script = None
        for dirpath, _, filenames in os.walk(tmpdir):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                if filename == binary_name:
                    found_binary = full
                elif filename == script_name:
                    found_script = full
        if not found_binary:
            raise RuntimeError('runtime 包中未找到 llama-funasr-sensevoice')
        shutil.copy2(found_binary, os.path.join(ROOT, binary_name))
        if found_script:
            shutil.copy2(found_script, os.path.join(ROOT, script_name))
        if platform.system().lower() != 'windows':
            for name in (binary_name, script_name):
                path = os.path.join(ROOT, name)
                if os.path.exists(path):
                    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        log('✅ runtime 已就绪')

if is_linux():
    build_runtime_from_source()
else:
    asset = runtime_asset_name()
    archive = os.path.join(ROOT, asset)
    download(latest_runtime_url(asset), archive, 1024 * 1024)
    extract_runtime(archive)
download('https://huggingface.co/FunAudioLLM/SenseVoiceSmall-GGUF/resolve/main/sensevoice-small-q8.gguf', os.path.join(GGUF_DIR, 'sensevoice-small-q8.gguf'), 100 * 1024 * 1024)
download('https://huggingface.co/FunAudioLLM/fsmn-vad-GGUF/resolve/main/fsmn-vad.gguf', os.path.join(GGUF_DIR, 'fsmn-vad.gguf'), 100 * 1024)
log('🎉 GGUF 本地模型资源下载完成')
"""

        def run():
            global download_proc;
            download_proc = subprocess.Popen([sys.executable, '-u', '-c', script], stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, env=env)
            for l in download_proc.stdout: download_logs.append(l.strip()); (
                download_logs.pop(0) if len(download_logs) > 500 else None)
            download_proc.wait();
            download_logs.append("=== ✅ 成功 ===" if download_proc.returncode == 0 else "=== ❌ 失败 ===")

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"code": 200})


@app.route('/api/model/log', methods=['GET'])
@login_required
def get_download_log(): return jsonify(
    {"code": 200, "running": (download_proc and download_proc.poll() is None), "logs": "\n".join(download_logs)})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('index'))
    if request.method == 'POST':
        u = request.form.get('username');
        p = request.form.get('password');
        ip = request.remote_addr
        is_b, w = check_ip_ban(ip)
        if is_b: return render_template('login.html', error=f"⚠️ IP封禁中，剩余 {w} 分钟")
        user = User.query.get(u)
        if user and check_password_hash(user.password_hash, p): reset_login_fail(ip); login_user(user,
                                                                                                 remember=True); return redirect(
            url_for('index'))
        record_login_fail(ip);
        time.sleep(1);
        return render_template('login.html', error="❌ 用户名或密码错误")
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect(url_for('login'))


@app.route('/')
@login_required
def index(): return render_template('index.html')


@app.route('/settings_page')
@login_required
def settings_page(): return render_template('settings.html', app_version=APP_VERSION)


@app.route('/api/trigger', methods=['POST'])
def trigger():
    c = get_final_config(None);
    t = c.get('api_token', '8pUoqOTHhEAhRnacl3c19')
    if os.path.exists(os.path.join(os.path.dirname(__file__), '.token_secret')):
        try:
            t = open(os.path.join(os.path.dirname(__file__), '.token_secret')).read().strip()
        except:
            pass
    if request.headers.get('X-API-Token') != t: return jsonify({"code": 403}), 403
    req = request.json or {}
    path = req.get('path')
    try:
        file_count = int(req.get('file_count', 1) or 1)
    except:
        file_count = 1
    if not path or not os.path.exists(path): return jsonify({"code": 400})

    task_path = path
    task_name = os.path.basename(path)
    task_overrides = {}
    if file_count > 1:
        task_path = resolve_directory_task_path(path, file_count, c.get('scan_path', '/root/downloads'))
        if not os.path.exists(task_path):
            task_path = os.path.dirname(path)
        task_name = os.path.basename(task_path.rstrip('/\\'))
        task_overrides = {
            '_dir_task': True,
            '_dir_total_files': file_count,
            '_dir_uploaded_count': 0
        }

    new_id = get_next_persistent_id()
    task = Task(
        id=new_id,
        filename=task_name,
        filepath=task_path,
        status="pending",
        overrides=json.dumps(task_overrides) if task_overrides else None
    )
    db.session.add(task);
    db.session.commit();
    detect_queue.put(task.id)
    return jsonify({"code": 200, "task_id": new_id})


@app.route('/api/tasks')
@login_required
def get_tasks():
    # UI 需要同时展示“检测队列/上传队列”，并且两边都最多展示 N 条。
    # 这里用与 batch 操作、前端同样的规则来判断任务属于上传还是检测。
    LIMIT_EACH = 200
    SCAN_LIMIT = 2000

    def _is_upload_task(t: Task) -> bool:
        ov = get_task_overrides(t)
        if t.status in ['pending_upload', 'uploading', 'uploaded']:
            return True
        if ov.get('_dir_task'):
            return ov.get('_dir_stage') == 'upload'
        if t.status in ['error', 'cancelled', 'dirty']:
            try:
                if ov.get('direct_upload') is True:
                    return True
            except:
                pass
            if t.upload_speed:
                return True
            if t.upload_eta and t.upload_eta != '-':
                return True
            log = t.log or ''
            if '☁️ 上传' in log or '=== 批量重传 ===' in log or '=== 直传 ===' in log:
                return True
        return False

    scan = Task.query.order_by(Task.id.desc()).limit(SCAN_LIMIT).all()
    detect_sel = []
    upload_sel = []

    for t in scan:
        if _is_upload_task(t):
            if len(upload_sel) < LIMIT_EACH:
                upload_sel.append(t)
        else:
            if len(detect_sel) < LIMIT_EACH:
                detect_sel.append(t)
        if len(detect_sel) >= LIMIT_EACH and len(upload_sel) >= LIMIT_EACH:
            break

    res = []
    for t in (detect_sel + upload_sel):
        res.append({"id": t.id, "filename": t.filename, "status": t.status, "log": t.log,
                    "created_at": t.created_at.strftime("%m-%d %H:%M"),
                    "finished_at": t.finished_at.strftime("%H:%M:%S") if t.finished_at else "-", "progress": t.progress,
                    "upload_speed": t.upload_speed, "upload_eta": t.upload_eta,
                    "config": get_final_config(t.overrides)})
    return jsonify(res)


@app.route('/api/tasks/batch', methods=['POST'])
@login_required
def batch_tasks():
    d = request.json;
    action = d.get('action');
    target = d.get('type');
    count = 0
    if not action or not target: return jsonify({"code": 400})

    detect_ids = [];
    upload_ids = []

    for t in Task.query.all():
        ov = get_task_overrides(t)
        is_up = t.status in ['uploading', 'pending_upload', 'uploaded'] or ov.get('_dir_stage') == 'upload' or (
            not ov.get('_dir_task') and '上传' in (t.log or "")
        )

        if target == 'detect':
            if action == 'retry' and t.status in ['error', 'cancelled', 'dirty'] and not is_up:
                t.status = 'pending';
                t.retry_count = 0;
                t.log += "\n=== 批量重试 (检测) ===\n";
                detect_ids.append(t.id);
                count += 1
                if t.overrides:
                    try:
                        ov = get_task_overrides(t)
                        ov.pop('_passed', None)
                        ov.pop('_passed_file', None)
                        set_task_overrides(t, ov)
                    except:
                        pass

            elif action == 'stop' and t.status in ['pending', 'processing']:
                if t.id in running_tasks: running_tasks[t.id].stop()
                t.status = 'cancelled';
                t.finished_at = datetime.now();
                count += 1

        elif target == 'upload':
            if action == 'retry' and t.status in ['error', 'cancelled'] and is_up:
                t.status = 'pending_upload';
                t.retry_count = 0;
                t.log += "\n=== 批量重传 ===\n";
                upload_ids.append(t.id);
                count += 1
            elif action == 'stop' and t.status == 'uploading':
                if t.id in running_tasks: running_tasks[t.id].stop()
                t.status = 'cancelled';
                t.finished_at = datetime.now();
                count += 1

    db.session.commit()
    for i in detect_ids: detect_queue.put(i)
    for i in upload_ids: upload_queue.put(i)

    return jsonify({"code": 200, "msg": f"操作了 {count} 个任务"})


@app.route('/api/retry/<int:tid>', methods=['POST'])
@login_required
def retry(tid):
    t = Task.query.get(tid);
    if not t: return jsonify({"code": 404})

    if t.overrides:
        try:
            ov = get_task_overrides(t)
            ov.pop('_passed', None)
            ov.pop('_passed_file', None)
            set_task_overrides(t, ov)
        except:
            pass

    ov = get_task_overrides(t)
    is_up = t.status in ['uploading', 'pending_upload'] or ov.get('_dir_stage') == 'upload' or (t.log and '上传' in t.log and not is_directory_task(t, ov))
    t.log += "\n=== 人工重试 ===\n";
    t.finished_at = None;
    t.retry_count = 0
    if is_up:
        t.status = 'pending_upload';
        db.session.commit();
        upload_queue.put(t.id)
    else:
        t.status = 'pending';
        db.session.commit();
        detect_queue.put(t.id)
    return jsonify({"code": 200})


@app.route('/api/task/<int:tid>/direct_upload', methods=['POST'])
@login_required
def direct_upload(tid):
    t = Task.query.get(tid);
    if t:
        update_task_overrides(t, {'direct_upload': True})
        t.status = 'pending'; t.log += "\n=== 直传 ===\n"; t.finished_at = None; t.retry_count = 0; db.session.commit(); detect_queue.put(
            t.id)
    return jsonify({"code": 200})


@app.route('/api/task/<int:tid>/double_sample', methods=['POST'])
@login_required
def double_sample_task(tid):
    t = Task.query.get(tid)
    if not t:
        return jsonify({"code": 404}), 404
    if t.status in ['processing', 'uploading']:
        return jsonify({"code": 409, "msg": "任务运行中，无法切换抽样"}), 409

    update_task_overrides(
        t,
        {'check_audio': True, 'audio_double_sample': True},
        remove_keys=['_passed', '_passed_file']
    )
    t.status = 'pending'
    t.log += "\n=== 单任务双倍抽样 ===\n"
    t.finished_at = None
    t.retry_count = 0
    db.session.commit()
    detect_queue.put(t.id)
    return jsonify({"code": 200})


@app.route('/api/task/<int:tid>/save_and_retry', methods=['POST'])
@login_required
def save_and_retry(tid):
    t = Task.query.get(tid);
    if t:
        replace_public_task_overrides(t, request.json or {})
        t.status = 'pending'; t.log += "\n=== 调整重试 ===\n"; t.finished_at = None; t.retry_count = 0; db.session.commit(); detect_queue.put(
            t.id)
    return jsonify({"code": 200})


@app.route('/api/task/<int:tid>/delete', methods=['POST'])
@login_required
def delete_task_file(tid):
    t = Task.query.get(tid);
    if not t: return jsonify({"code": 404})
    if tid in running_tasks:
        running_tasks[tid].stop()
        del running_tasks[tid]
        time.sleep(0.1)

    files_to_remove = set()
    if t.filepath:
        files_to_remove.add(t.filepath)
        try:
            dirname = os.path.dirname(t.filepath)
            basename = os.path.basename(t.filepath)
            name, ext = os.path.splitext(basename)
            files_to_remove.add(os.path.join(dirname, f"{name}_clean{ext}"))
            files_to_remove.add(os.path.join(dirname, f"{name}_clean_meta{ext}"))
            if "_clean" in name:
                orig = name.replace("_clean_meta", "").replace("_clean", "")
                files_to_remove.add(os.path.join(dirname, f"{orig}{ext}"))
        except:
            pass

    deleted = []
    if is_directory_task(t):
        if t.filepath and os.path.isdir(t.filepath):
            try:
                shutil.rmtree(t.filepath)
                deleted.append(os.path.basename(t.filepath.rstrip('/\\')))
            except:
                pass
    else:
        for fp in files_to_remove:
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp);
                    deleted.append(os.path.basename(fp))
                except:
                    pass

    db.session.delete(t);
    db.session.commit()
    msg = f"任务及文件已删除 ({', '.join(deleted)})" if deleted else "任务记录已删除 (未找到文件)"
    return jsonify({"code": 200, "msg": msg})


@app.route('/api/cancel/<int:tid>', methods=['POST'])
@login_required
def cancel(tid):
    if tid in running_tasks: running_tasks[tid].stop()
    t = Task.query.get(tid);
    if t: t.status = 'cancelled'; t.finished_at = datetime.now(); db.session.commit()
    return jsonify({"code": 200})


@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        data = dict(request.json or {})
        if 'cloud_asr_api_keys' in data or 'api_key' in data:
            raw_keys = str(data.get('cloud_asr_api_keys') or data.get('api_key') or '')
            keys = []
            seen = set()
            for key in raw_keys.splitlines():
                key = key.strip()
                if key and key not in seen:
                    keys.append(key)
                    seen.add(key)
            data['cloud_asr_api_keys'] = "\n".join(keys)
            data['api_key'] = keys[0] if keys else ""
        for k, v in data.items():
            if k in ["check_audio", "check_subtitles", "sanitize_metadata", "enable_cloud_asr", "enable_local_model", "detailed_mode", "asr_use_flac", "audio_double_sample",
                     "notify_upload_success", "notify_errors"]:
                val = "true" if (v is True or str(v).lower() == 'true') else "false"
            else:
                val = str(v)
            c = Config.query.get(k) or Config(key=k);
            c.value = val;
            db.session.add(c)
        db.session.commit()
        if 'api_token' in data:
            tk = str(data['api_token']).strip()
            if re.match(r'^[a-zA-Z0-9_\-]+$', tk):
                try:
                    open(os.path.join(os.path.dirname(__file__), '.token_secret'), 'w').write(tk)
                except:
                    pass
        return jsonify({"code": 200})
    c = get_final_config(None);
    c['model_exists'] = check_local_models_exist();
    c['username'] = current_user.id
    c['app_version'] = APP_VERSION
    return jsonify(c)


@app.route('/api/settings/backup', methods=['GET'])
@login_required
def export_settings_backup():
    configs = {
        k: v
        for k, v in get_final_config(None).items()
        if not str(k).startswith('sys_')
    }
    keywords = [
        {'type': k.type, 'content': k.content, 'enabled': bool(k.enabled)}
        for k in Keyword.query.order_by(Keyword.type.asc(), Keyword.id.asc()).all()
    ]
    return jsonify({
        'schema_version': 1,
        'app_version': APP_VERSION,
        'exported_at': datetime.now().isoformat(timespec='seconds'),
        'config': configs,
        'keywords': keywords
    })


@app.route('/api/settings/restore', methods=['POST'])
@login_required
def restore_settings_backup():
    data = request.json or {}
    configs = data.get('config') or data.get('configs') or {}
    keywords = data.get('keywords')

    if not isinstance(configs, dict):
        return jsonify({'code': 400, 'msg': '配置备份格式无效'}), 400

    for k, v in configs.items():
        key = str(k).strip()
        if not key or key.startswith('sys_'):
            continue
        c = Config.query.get(key) or Config(key=key)
        c.value = str(v)
        db.session.add(c)

    if isinstance(keywords, list):
        Keyword.query.delete()
        for item in keywords:
            if not isinstance(item, dict):
                continue
            kw_type = str(item.get('type', '')).strip()
            content = str(item.get('content', '')).strip()
            if kw_type not in ['audio', 'subtitle', 'meta'] or not content:
                continue
            enabled = item.get('enabled', True)
            db.session.add(Keyword(type=kw_type, content=content, enabled=(enabled is True or str(enabled).lower() == 'true')))

    db.session.commit()
    token = configs.get('api_token') if isinstance(configs, dict) else None
    if token:
        tk = str(token).strip()
        if re.match(r'^[a-zA-Z0-9_\-]+$', tk):
            try:
                open(os.path.join(os.path.dirname(__file__), '.token_secret'), 'w').write(tk)
            except:
                pass

    return jsonify({'code': 200})


@app.route('/api/account/update', methods=['POST'])
@login_required
def update_account():
    d = request.json;
    op = d.get('old_password');
    np = d.get('new_password');
    nu = d.get('new_username')
    if not op: return jsonify({"code": 400, "msg": "需旧密码"})
    if not check_password_hash(current_user.password_hash, op): return jsonify({"code": 403, "msg": "密码错误"})
    if np: current_user.password_hash = generate_password_hash(np)
    if nu and nu != current_user.id:
        if User.query.get(nu): return jsonify({"code": 409, "msg": "用户名已存在"})
        db.session.execute(db.update(User).where(User.id == current_user.id).values(id=nu))
    db.session.commit();
    return jsonify({"code": 200})


@app.route('/api/system_logs', methods=['GET'])
@login_required
def get_system_logs():
    try:
        r = subprocess.run(
            ['journalctl', '-t', 'arup', '-n', str(request.args.get('lines', 9999)), '--no-pager', '--output',
             'short-iso'], capture_output=True, text=True)
        return jsonify({"code": 200, "data": r.stdout})
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)})


@app.route('/api/system_logs/clear', methods=['POST'])
@login_required
def clear_system_logs():
    try:
        subprocess.run(['journalctl', '--rotate'], check=True);
        subprocess.run(['journalctl', '--vacuum-time=1s'], check=True)
        return jsonify({"code": 200, "msg": "已清理"})
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)})


@app.route('/api/keywords', methods=['GET', 'POST'])
@login_required
def manage_keywords():
    if request.method == 'GET': return jsonify(
        [{'id': k.id, 'type': k.type, 'content': k.content, 'enabled': k.enabled} for k in Keyword.query.all()])
    d = request.json;
    for i in [x.strip() for x in d.get('content', '').split('|') if x.strip()]:
        if not Keyword.query.filter_by(type=d.get('type', 'audio'), content=i).first(): db.session.add(
            Keyword(type=d.get('type', 'audio'), content=i, enabled=True))
    db.session.commit();
    return jsonify({"code": 200})


@app.route('/api/keyword/<int:kid>', methods=['DELETE', 'PUT'])
@login_required
def update_keyword(kid):
    k = Keyword.query.get(kid)
    if k:
        if request.method == 'DELETE':
            db.session.delete(k)
        else:
            k.enabled = request.json.get('enabled', k.enabled)
        db.session.commit()
    return jsonify({"code": 200})


@app.route('/api/tasks/clear', methods=['POST'])
@login_required
def clear_tasks():
    Task.query.filter(Task.status.in_(['uploaded', 'dirty', 'error', 'cancelled'])).delete(synchronize_session=False);
    db.session.commit()
    return jsonify({"code": 200, "msg": "已清理"})


@app.route('/api/update_task_config/<int:tid>', methods=['POST'])
@login_required
def update_task_config(tid):
    t = Task.query.get(tid);
    if t:
        replace_public_task_overrides(t, request.json or {})
        db.session.commit()
    return jsonify({"code": 200})


@app.route('/api/restart', methods=['POST'])
@login_required
def restart_service():
    def _restart(): time.sleep(1); os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_restart).start();
    return jsonify({"code": 200, "msg": "重启中..."})


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.first(): db.session.add(
            User(id="admin", password_hash=generate_password_hash("admin123"))); db.session.commit()

        # 🔥 开启 WAL 模式 (大幅优化 I/O)
        try:
            db.session.execute(text("PRAGMA journal_mode=WAL"));
            db.session.commit();
            print("🚀 SQLite WAL Enabled")
        except:
            pass

        # 🔥 Startup Recovery
        print("🔎 正在恢复中断的任务队列...")
        recover_d = 0;
        recover_u = 0
        for t in Task.query.filter(Task.status.in_(['processing', 'pending'])).all():
            t.status = 'pending';
            detect_queue.put(t.id);
            recover_d += 1
            if t.status == 'processing': t.log += "\n=== 系统重启：恢复检测 ===\n"
        for t in Task.query.filter(Task.status.in_(['uploading', 'pending_upload'])).all():
            t.status = 'pending_upload';
            upload_queue.put(t.id);
            recover_u += 1
            if t.status == 'uploading': t.log += "\n=== 系统重启：恢复上传 ===\n"
        db.session.commit()
        print(f"🔄 已重新排队: {recover_d} 检测, {recover_u} 上传")

        c = get_final_config(None);
        n_d = max(1, c.get('concurrency_detect', 2));
        n_u = max(1, c.get('concurrency_upload', 9))
        print(f"🚀 启动检测: {n_d} | 上传: {n_u}")

    for _ in range(n_d): threading.Thread(target=detection_worker, daemon=True).start()
    for _ in range(n_u): threading.Thread(target=upload_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
