import os
import json
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
from core_logic import ScannerCore
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

login_manager = LoginManager(app)
login_manager.login_view = 'login'

detect_queue = queue.Queue()
upload_queue = queue.Queue()
running_tasks = {}
download_proc = None;
download_logs = [];
download_lock = threading.Lock()
LOGIN_ATTEMPTS = {}


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


AUDIO_BLACKLIST_INIT = ["加群", "交流群", "TG群", "Telegram", "QQ群", "Q群", "资源群", "微信号", "微信群", "微信公众号",
                        "关注公众号"]
SUBTITLE_BLACKLIST_INIT = ["加群", "交流群", "微信号", "微信群", "QQ", "qq", "q群", "公众号", "网址", ".com",
                           "http", "www", "link3.cc", "ysepan.com", "Tacit0924"]
SUB_META_BLACKLIST_INIT = ["http", "www", "weixin", "Telegram", "TG@", "TG频道@", "群：", "群:", "资源群", "加群",
                           "微信号", "微信群", "QQ", "qq", "q群", "公众号", "微博", "b站", "资源站", "资源网", "发布页",
                           "荣誉出品", "link3.cc", "ysepan.com", "GyWEB", "Qqun", "hehehe", ".com", "PTerWEB",
                           "panclub", "BT之家", "CMCT", "Byakuya", "ed3000", "yunpantv", "KKYY", "盘酱酱", "TREX",
                           "£yhq@tv", "1000fr", "HDCTV", "HHWEB", "ADWeb", "PanWEB", "BestWEB"]


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
        "check_audio": True, "check_subtitles": True, "sanitize_metadata": True, "enable_local_model": False,
        "detailed_mode": False,
        "tg_bot_token": "", "tg_chat_id": "",
        "audio_threshold_multi": 600, "audio_threshold_long": 3600,
        "audio_len_head": 240, "audio_len_mid": 240, "audio_len_tail": 300, "audio_len_tail_long": 600,
        "api_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
        "api_key": "",
        "api_model": "FunAudioLLM/SenseVoiceSmall",
        "scan_path": "/root/downloads", "rclone_remote": "s25", "api_token": "8pUoqOTHhEAhRnacl3c19",
        "notify_upload_success": False, "notify_errors": True,
        "concurrency_detect": 2, "concurrency_upload": 9, "download_proxy": ""
    }
    db_configs = {c.key: c.value for c in Config.query.all()}
    for k, v in db_configs.items():
        if k in ["check_audio", "check_subtitles", "sanitize_metadata", "enable_local_model", "detailed_mode",
                 "notify_upload_success", "notify_errors"]:
            final_conf[k] = (str(v).lower() == 'true')
        elif k in ["audio_threshold_multi", "audio_threshold_long", "audio_len_head", "audio_len_mid", "audio_len_tail",
                   "audio_len_tail_long", "concurrency_detect", "concurrency_upload"]:
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
                task = Task.query.get(task_id)
                if not task or task.status == 'cancelled': detect_queue.task_done(); continue
                task.status = 'processing';
                task.progress = 0;
                db.session.commit()

                final_settings = get_final_config(task.overrides)

                RETRY_LIMIT = 3
                user_local_pref = final_settings.get('enable_local_model', False)

                final_settings['current_retry'] = task.retry_count + 1
                final_settings['retry_limit'] = RETRY_LIMIT

                if task.retry_count < RETRY_LIMIT:
                    final_settings['enable_local_model'] = False
                else:
                    final_settings['enable_local_model'] = user_local_pref

                passed_segments = []
                try:
                    if task.overrides:
                        ov = json.loads(task.overrides)
                        passed_segments = ov.get('_passed', [])
                except:
                    pass

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

                def detect_prog(pct, msg, _):
                    t = Task.query.get(task_id);
                    if t: t.progress = pct; db.session.commit()

                def save_checkpoint(seg_name):
                    try:
                        t = Task.query.get(task_id)
                        ov = json.loads(t.overrides) if t.overrides else {}
                        passed = ov.get('_passed', [])
                        if seg_name not in passed:
                            passed.append(seg_name)
                            ov['_passed'] = passed
                            t.overrides = json.dumps(ov)
                            db.session.commit()
                    except:
                        pass

                def update_filepath(new_path):
                    try:
                        t = Task.query.get(task_id)
                        if t and new_path and t.filepath != new_path:
                            t.filepath = new_path
                            t.filename = os.path.basename(new_path)
                            t.log = (t.log or "") + f"🔄 文件已更新为: {t.filename}\n"
                            db.session.commit()
                    except:
                        pass

                core = ScannerCore(logger_callback=db_logger, task_id=task_id, root_dir_name=current_root_name,
                                   rclone_remote=rclone_remote)
                core.prog_cb = detect_prog
                running_tasks[task_id] = core

                try:
                    res = core.process_file(
                        task.filepath, final_settings, keywords_config,
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
                        if os.path.exists(task.filepath): os.remove(task.filepath)
                        if final_settings.get('notify_errors', True): core.send_tg_msg(final_settings,
                                                                                       f"🚫 拦截: {task.filename}\n原因: {res['msg']}")
                    elif res['status'] == 'ready_to_upload':
                        if res.get('new_filepath'):
                            task.filepath = res['new_filepath'];
                            task.filename = os.path.basename(res['new_filepath'])
                        task.status = 'pending_upload';
                        task.progress = 0;
                        db_logger("✅ 检测通过，加入上传队列");
                        db.session.commit();
                        upload_queue.put(task_id)
                    else:
                        if task.retry_count < RETRY_LIMIT:
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
                    if task_id in running_tasks: del running_tasks[task_id]
                    db.session.commit();
                    detect_queue.task_done()
            except Exception as e:
                print(e)


def upload_worker():
    with app.app_context():
        while True:
            try:
                task_id = upload_queue.get()
                task = Task.query.get(task_id)
                if not task or task.status == 'cancelled': upload_queue.task_done(); continue
                task.status = 'uploading';
                db.session.commit()

                final_settings = get_final_config(task.overrides)
                scan_path = final_settings.get('scan_path', '/root/downloads')
                rclone_remote = final_settings.get('rclone_remote', 's25')
                current_root_name = os.path.basename(scan_path.rstrip('/'))
                folder_name = os.path.basename(os.path.dirname(task.filepath))
                dest_remote = rclone_remote if (folder_name == current_root_name or not folder_name) else folder_name

                def db_logger(msg):
                    try:
                        t = Task.query.get(task_id);
                        if t: t.log = (t.log or "") + f"{msg}\n"; db.session.commit()
                    except:
                        pass

                def upload_prog(pct, speed, eta):
                    try:
                        t = Task.query.get(task_id);
                        if t: t.progress = pct; t.upload_speed = speed; t.upload_eta = eta; db.session.commit()
                    except:
                        pass

                core = ScannerCore(logger_callback=db_logger, task_id=task_id, root_dir_name=current_root_name,
                                   rclone_remote=rclone_remote)
                core.prog_cb = upload_prog
                running_tasks[task_id] = core
                try:
                    if core.upload_with_progress(task.filepath):
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
                            db_logger("❌ 上传失败")
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
                    if task_id in running_tasks: del running_tasks[task_id]
                    try:
                        db.session.commit()
                    except:
                        pass
                    upload_queue.task_done()
            except Exception as e:
                print(e)


# ----------------- Model & System Routes -----------------
def check_local_models_exist():
    base = os.path.join(os.getcwd(), 'models', 'iic')
    paths = [os.path.join(base, 'speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch'),
             os.path.join(base, 'speech_fsmn_vad_zh-cn-16k-common-pytorch'),
             os.path.join(base, 'punc_ct-transformer_zh-cn-common-vocab272727-pytorch')]
    return all(os.path.exists(p) for p in paths)


@app.route('/api/model/download', methods=['POST'])
@login_required
def download_model():
    global download_proc, download_logs
    sys_conf = get_final_config(None);
    proxy_url = sys_conf.get('download_proxy', '')
    with download_lock:
        if download_proc and download_proc.poll() is None: return jsonify({"code": 409, "msg": "下载任务正在进行"})
        download_logs = ["=== 🚀 初始化并行下载任务 (支持自动重试) ==="]
        env = os.environ.copy()
        if proxy_url: env['HTTP_PROXY'] = proxy_url; env['HTTPS_PROXY'] = proxy_url
        script = """
import sys, os, time, concurrent.futures
try: from modelscope.hub.snapshot_download import snapshot_download
except: print("❌ 未安装 modelscope", flush=True); sys.exit(1)
root_dir = os.path.join(os.getcwd(), 'models'); os.makedirs(root_dir, exist_ok=True)
models = [{'id':'iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch','name':'主模型'},{'id':'iic/speech_fsmn_vad_zh-cn-16k-common-pytorch','name':'VAD'},{'id':'iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch','name':'标点'}]
def dl(m):
    for i in range(10):
        try: print(f"⬇️ [{m['name']}] 下载中...",flush=True); snapshot_download(m['id'], cache_dir=root_dir); print(f"✅ [{m['name']}] 完成",flush=True); return
        except Exception as e: print(f"⚠️ [{m['name']}] 错误: {str(e).splitlines()[0]}",flush=True); time.sleep(5)
    raise Exception(f"{m['name']} 失败")
with concurrent.futures.ThreadPoolExecutor(3) as ex:
    for f in concurrent.futures.as_completed([ex.submit(dl, m) for m in models]):
        try: f.result()
        except: sys.exit(1)
print('🎉 下载完成', flush=True)
"""

        def run():
            global download_proc;
            download_proc = subprocess.Popen(['python3', '-u', '-c', script], stdout=subprocess.PIPE,
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
def settings_page(): return render_template('settings.html')


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
    path = request.json.get('path')
    if not path or not os.path.exists(path): return jsonify({"code": 400})

    new_id = get_next_persistent_id()
    task = Task(id=new_id, filename=os.path.basename(path), filepath=path, status="pending")
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
        if t.status in ['pending_upload', 'uploading', 'uploaded']:
            return True
        if t.status in ['error', 'cancelled', 'dirty']:
            try:
                if t.overrides:
                    ov = json.loads(t.overrides)
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
        is_up = '上传' in (t.log or "") or t.status in ['uploading', 'pending_upload', 'uploaded']

        if target == 'detect':
            if action == 'retry' and t.status in ['error', 'cancelled', 'dirty'] and not is_up:
                t.status = 'pending';
                t.retry_count = 0;
                t.log += "\n=== 批量重试 (检测) ===\n";
                detect_ids.append(t.id);
                count += 1
                if t.overrides:
                    try:
                        ov = json.loads(t.overrides)
                        if '_passed' in ov: del ov['_passed']
                        t.overrides = json.dumps(ov)
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
            ov = json.loads(t.overrides)
            if '_passed' in ov:
                del ov['_passed']
                t.overrides = json.dumps(ov)
        except:
            pass

    is_up = t.status == 'uploading' or (t.log and '上传' in t.log)
    t.log += "\n=== 人工重试 ===\n";
    t.finished_at = None;
    t.retry_count = 0
    if is_up:
        t.status = 'pending_upload'; db.session.commit(); upload_queue.put(t.id)
    else:
        t.status = 'pending'; db.session.commit(); detect_queue.put(t.id)
    return jsonify({"code": 200})


@app.route('/api/task/<int:tid>/direct_upload', methods=['POST'])
@login_required
def direct_upload(tid):
    t = Task.query.get(tid);
    if t: t.overrides = json.dumps({
                                       "direct_upload": True}); t.status = 'pending'; t.log += "\n=== 直传 ===\n"; t.finished_at = None; t.retry_count = 0; db.session.commit(); detect_queue.put(
        t.id)
    return jsonify({"code": 200})


@app.route('/api/task/<int:tid>/save_and_retry', methods=['POST'])
@login_required
def save_and_retry(tid):
    t = Task.query.get(tid);
    if t: t.overrides = json.dumps(
        request.json); t.status = 'pending'; t.log += "\n=== 调整重试 ===\n"; t.finished_at = None; t.retry_count = 0; db.session.commit(); detect_queue.put(
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
    for fp in files_to_remove:
        if fp and os.path.exists(fp):
            try:
                os.remove(fp); deleted.append(os.path.basename(fp))
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
        for k, v in request.json.items():
            if k in ["check_audio", "check_subtitles", "sanitize_metadata", "enable_local_model", "detailed_mode",
                     "notify_upload_success", "notify_errors"]:
                val = "true" if (v is True or str(v).lower() == 'true') else "false"
            else:
                val = str(v)
            c = Config.query.get(k) or Config(key=k);
            c.value = val;
            db.session.add(c)
        db.session.commit()
        if 'api_token' in request.json:
            tk = str(request.json['api_token']).strip()
            if re.match(r'^[a-zA-Z0-9_\-]+$', tk):
                try:
                    open(os.path.join(os.path.dirname(__file__), '.token_secret'), 'w').write(tk)
                except:
                    pass
        return jsonify({"code": 200})
    c = get_final_config(None);
    c['model_exists'] = check_local_models_exist();
    c['username'] = current_user.id
    return jsonify(c)


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
            ['journalctl', '-t', 'arup', '-n', str(request.args.get('lines', 200)), '--no-pager', '--output',
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
    if t: t.overrides = json.dumps(request.json); db.session.commit()
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
            db.session.execute(text("PRAGMA journal_mode=WAL")); db.session.commit(); print("🚀 SQLite WAL Enabled")
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
