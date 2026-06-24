# AGENTS.md

This file is the operational guide for future AI agents and developers working on this repository.

## Project Overview

Video-ASR-Ad-Cleaner is a Flask-based media audit and cleanup dashboard for Aria2 + Rclone workflows.

Main responsibilities:

- Receive completed download paths from `trigger.sh` / `/api/trigger`.
- Queue video files for metadata, subtitle, and audio checks.
- Remove dirty metadata and dirty subtitle tracks when possible.
- Run ASR checks against selected audio segments.
- Queue clean files for `rclone moveto` upload.
- Provide a web dashboard and settings page.

The current app version is defined in `app.py` as `APP_VERSION` and displayed on the settings page.

## Repository Layout

- `app.py`: Flask app, routes, task queues, retry logic, DB-backed settings, upload worker, detection worker.
- `core_logic.py`: Media processing core, ffprobe/ffmpeg wrappers, metadata cleanup, subtitle scanning, audio extraction, ASR logic, rclone upload helper.
- `database.py`: SQLAlchemy models for `Task`, `Config`, `Keyword`, and `User`.
- `templates/index.html`: Main dashboard UI.
- `templates/settings.html`: Settings UI, version badge, keyword management.
- `templates/login.html`: Login UI.
- `trigger.sh`: Aria2 completion hook.
- `install/install.sh`: Existing install/uninstall helper.
- `requirements.txt`: Python dependencies.

No Docker packaging is tracked in this repository. Do not add Docker packaging unless explicitly requested.

## Runtime Architecture

The app starts from `app.py` and launches two worker pools:

- Detection workers consume `detect_queue`.
- Upload workers consume `upload_queue`.

Task lifecycle:

1. `/api/trigger` creates a `Task` row and puts the task ID into `detect_queue`.
2. `detection_worker()` builds final settings and keyword lists.
3. `ScannerCore.process_file()` runs metadata cleanup, subtitle cleanup, and audio detection.
4. Clean files are marked `pending_upload` and queued to `upload_queue`.
5. `upload_worker()` runs `rclone moveto`.

Important statuses:

- `pending`: waiting for detection.
- `processing`: detection is running.
- `pending_upload`: waiting for upload.
- `uploading`: upload is running.
- `uploaded`: complete.
- `dirty`: blocked by audio keyword match.
- `error`: failed after retry policy.
- `cancelled`: manually stopped.

## Settings And Defaults

Global settings are stored in the `Config` table and merged by `get_final_config()` in `app.py`.

Common settings:

- `check_audio`: enable ASR audio checks.
- `check_subtitles`: enable subtitle checks and subtitle-track removal.
- `sanitize_metadata`: enable metadata cleanup.
- `enable_cloud_asr`: enable cloud ASR requests. When disabled, audio checks skip cloud requests and go directly to local GGUF ASR if `enable_local_model` is enabled.
- `enable_local_model`: allow local GGUF model fallback. When cloud ASR is enabled, fallback is only allowed after cloud retries are exhausted. When cloud ASR is disabled, fallback is allowed immediately.
- `local_model_concurrency`: maximum number of simultaneous local GGUF inference subprocesses. Default is `1`; raising it increases CPU and memory pressure.
- `detailed_mode`: log full recognized text even for successful checks.
- `concurrency_detect`: detection worker count. Requires restart to take effect.
- `concurrency_upload`: upload worker count. Requires restart to take effect.
- `audio_threshold_multi`: if video duration exceeds this, also scan middle and head segments.
- `audio_threshold_long`: long-video threshold for using longer tail sample.
- `audio_len_head`, `audio_len_mid`, `audio_len_tail`, `audio_len_tail_long`: ASR segment lengths.
- `api_url`, `api_key`, `api_model`: cloud ASR config.
- `scan_path`, `rclone_remote`: local download root and default remote.

Sensitive values include `api_key`, `tg_bot_token`, `tg_chat_id`, and `api_token`. Do not print or commit real secrets.

## Media Processing Details

### Command Execution

Use `ScannerCore.run_cmd()` for ffmpeg/ffprobe commands when possible.

Current behavior:

- Works cross-platform with POSIX process groups and Windows process creation flags.
- Logs non-zero return codes and stderr snippets.
- Kills the current subprocess on timeout/stop.

### Metadata Cleanup

Implemented in `ScannerCore.sanitize_metadata()`.

Behavior:

- Scans format tags and stream tags for metadata keywords.
- Normalizes zero-width characters before matching.
- If dirty metadata is found, remuxes with cleaned metadata.
- Uses safe audio mapping to skip unknown/unsupported audio streams.

Important: unknown audio streams such as `av3a` may make MP4 remux fail if mapped blindly. Keep `get_safe_audio_map_args()` in metadata/subtitle-remux paths.

### Subtitle Cleanup

Implemented in `ScannerCore.check_subtitles()`.

Current optimized flow:

1. `get_subtitle_streams()` uses one `ffprobe` JSON call to collect subtitle `index`, `codec`, `language`, `title`, and `handler_name`.
2. Subtitle metadata is scanned first. If metadata hits a keyword, that track is marked dirty without extracting text.
3. Text subtitle tracks are batch-exported with one `ffmpeg` command into temporary WebVTT files.
4. Exported text is scanned for subtitle keywords after zero-width normalization.
5. Image subtitle tracks are not OCR-scanned. Only their metadata is scanned.
6. Dirty tracks are removed by remuxing video, safe audio streams, and clean subtitle tracks.

Image subtitle codecs currently treated as non-text:

- `hdmv_pgs_subtitle`
- `dvd_subtitle`
- `dvb_subtitle`
- `xsub`

Important behavior for PGS/image subtitles:

- Track titles can be checked and dirty tracks can be removed.
- Subtitle image content is not checked.
- OCR is not implemented and should not be added casually because full OCR can be very slow.

Known Task-3430 example:

- File had 6 `hdmv_pgs_subtitle` tracks.
- Log showed `字幕轨 6 条，待扫文本轨 0 条，图片轨 6 条`.
- The system scanned metadata only and retained tracks because no keyword matched.

### Audio ASR Detection

Implemented in `ScannerCore.scan_audio_cloud_fallback_local()`.

Segment order:

1. Tail.
2. Middle, if duration exceeds `audio_threshold_multi`.
3. Head, if duration exceeds `audio_threshold_multi`.

Cloud timeout policy:

- Connection timeout: `10s`.
- Read timeout: `120s` by default.
- Read timeout: `180s` when the current audio segment duration is `>= 450s`.

The log includes the timeout value:

```text
☁️ 云端识别中... (timeout=120s)
```

Cloud failure policy:

- `detect_retry_limit` is the number of automatic retries after the first attempt, not total attempts. For example, `detect_retry_limit = 1` means two total attempts and logs should show `第1/2次` then `第2/2次`.
- For retry attempts before the retry limit, local model fallback is forced off.
- After cloud retries are exhausted, local fallback follows the user's `enable_local_model` setting.
- If `enable_cloud_asr` is false, cloud upload is skipped and local GGUF ASR runs immediately when `enable_local_model` is true.
- If `enable_cloud_asr` is false and `enable_local_model` is also false, the task fails with a configuration error instead of being requeued.

Checkpoint behavior:

- Successful segments are stored in task overrides as `_passed`.
- On retry, already successful segments are skipped.
- The failing segment is retried.

Failed-segment audio cache:

- Failed segment WAV is kept in `/tmp/scan_<task_id>_<segment>.wav` for retry reuse.
- A JSON sidecar validates source path, file size, mtime, segment name, start, duration, and audio map.
- On retry, a valid cache logs `♻️ 复用音频` and skips ffmpeg extraction.
- On successful recognition, dirty hit, local model failure, or final no-retry attempt, cache is removed.

### Local GGUF ASR Fallback

The old local PyTorch/FunASR fallback has been replaced by a GGUF/llama.cpp runtime.

Relevant code:

- `get_sensevoice_gguf_paths()` and `sensevoice_gguf_ready()` in `core_logic.py` define and validate local model resources.
- `ScannerCore.run_local_sensevoice_gguf()` runs the local command-line ASR fallback.
- `ScannerCore.acquire_local_inference_slot()` / `release_local_inference_slot()` enforce `local_model_concurrency` with a process-local condition counter.
- `check_local_models_exist()` and `/api/model/download` in `app.py` now target GGUF resources.
- The settings page still uses the existing `enable_local_model` switch, but labels the resource as `SenseVoice GGUF / llama.cpp`.

Expected local resource layout:

```text
models/sensevoice-gguf/llama-funasr-sensevoice
models/sensevoice-gguf/gguf/sensevoice-small-q8.gguf
models/sensevoice-gguf/gguf/fsmn-vad.gguf
```

Windows may use `llama-funasr-sensevoice.exe` instead of `llama-funasr-sensevoice`.

The settings page download button fetches:

- FunAudioLLM SenseVoice llama.cpp runtime release.
- `FunAudioLLM/SenseVoiceSmall-GGUF` file `sensevoice-small-q8.gguf`.
- `FunAudioLLM/fsmn-vad-GGUF` file `fsmn-vad.gguf`.

Local GGUF concurrency behavior:

- `local_model_concurrency` defaults to `1` and is exposed on the settings page under `模型` -> `本地模型资源` -> `本地模型并发数`.
- The limiter is process-local. It gates simultaneous GGUF subprocesses inside the current Flask process, not across multiple independent service processes.
- Logs should show slot accounting, for example `等待本地模型资源槽... (并发上限 1)`, `获得本地模型资源槽 (1/1)`, and `本地 GGUF 推理资源已释放 (运行中 0/1)`.
- On netcup, recent logs before this setting showed the old single lock worked correctly: multiple tasks waited, but only one task held GGUF inference at a time. After deployment with default `1`, logs showed Task 7127 held `(1/1)` while Task 7128/7129 waited.
- Raising this above `1` can improve throughput only if CPU and memory headroom exist. Watch `systemctl status scanner` memory and CPU before increasing further.

Important ARM64 server behavior:

- On `hd东京绕`, the official prebuilt Linux ARM64 runtime started with `--help` but crashed during inference with `code=-4` / `SIGILL`.
- The cause was CPU instruction incompatibility on `aarch64` `Neoverse-N1`.
- The downloader now builds runtime from source on ARM64 Linux with `GGML_NATIVE=OFF` and writes `models/sensevoice-gguf/runtime-generic-arm64.txt`.
- If local GGUF inference fails with `code=-4`, rebuild the runtime on that server instead of redownloading the prebuilt binary.

Manual rebuild on ARM64 Linux:

```bash
build="/tmp/opencode_sensevoice_runtime_build_$(date +%s)"
git clone --depth 1 --branch runtime-llamacpp-v0.1.2 https://github.com/FunAudioLLM/SenseVoice.git "$build"
cd "$build/runtime/llama.cpp"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DLLAMA_CURL=OFF
cmake --build build -j 2 --target llama-funasr-sensevoice
install -m 755 build/bin/llama-funasr-sensevoice /www/wwwroot/scanner_web/models/sensevoice-gguf/llama-funasr-sensevoice
```

Quick local GGUF smoke test on a server:

```bash
cd /www/wwwroot/scanner_web
ffmpeg -hide_banner -loglevel error -f lavfi -i anullsrc=r=16000:cl=mono -t 1 -acodec pcm_s16le -y /tmp/sensevoice_silence.wav
./models/sensevoice-gguf/llama-funasr-sensevoice -m ./models/sensevoice-gguf/gguf/sensevoice-small-q8.gguf --vad ./models/sensevoice-gguf/gguf/fsmn-vad.gguf -a /tmp/sensevoice_silence.wav
python3 -c "from core_logic import ScannerCore, sensevoice_gguf_ready; print('ready', sensevoice_gguf_ready()); c=ScannerCore(logger_callback=print); print(c.run_local_sensevoice_gguf('/tmp/sensevoice_silence.wav', 1))"
```

For speech smoke tests, extract a small real sample first:

```bash
ffmpeg -hide_banner -loglevel error -ss 0 -t 5 -i "$SOURCE" -map 0:a:0 -vn -acodec pcm_s16le -ar 16000 -ac 1 -y /tmp/sensevoice_real_5s.wav
./models/sensevoice-gguf/llama-funasr-sensevoice -m ./models/sensevoice-gguf/gguf/sensevoice-small-q8.gguf --vad ./models/sensevoice-gguf/gguf/fsmn-vad.gguf -a /tmp/sensevoice_real_5s.wav
```

## Important Known Issues And Pitfalls

### Unknown Audio Streams

Some files contain an audio stream that ffmpeg reports as `Audio: none`, such as `av3a`.

Blindly using `-map 0:a?` can fail with:

```text
Could not find tag for codec none ... codec not currently supported in container
Could not write header: Invalid argument
```

Always use `get_safe_audio_map_args()` when remuxing output files.

### Zero-Width Ad Text

Ad strings may insert zero-width characters between digits or letters.

Always use `find_keywords()` / `normalize_scan_text()` for keyword checks instead of raw `kw in text`.

### Subtitle Performance

Do not reintroduce one-ffmpeg-per-subtitle-track scanning.

Multi-language files can have 20-40+ subtitle tracks. Per-track full-file extraction can take 5-7 minutes. Current batch extraction reduces this to roughly 15-20 seconds for many files.

### Local Model Memory

Local GGUF fallback can still use significant memory and CPU. `drop_caches()` tries to release memory on Linux after local inference. Be careful changing this path.

### Service Restarts

Restarting the Flask process requeues tasks in `processing`, `pending`, `uploading`, and `pending_upload` according to startup recovery logic.

## Development Workflow

Recommended local checks before deploying:

```powershell
python -m py_compile app.py core_logic.py database.py
```

There is currently no formal automated test suite.

For targeted behavior tests, prefer small synthetic media files created in:

```text
C:\Users\Administrator\AppData\Local\Temp\opencode
```

Clean generated test files after verification.

Useful targeted checks after ASR/local-model changes:

```powershell
python -m py_compile app.py core_logic.py database.py
```

Remote checks on a deployed server:

```bash
cd /www/wwwroot/scanner_web
python3 -m py_compile app.py core_logic.py database.py
python3 -c "from core_logic import ScannerCore; c=ScannerCore(logger_callback=lambda m: None); print(c.get_retry_attempt_label({'current_retry':1,'retry_limit':1})); print(c.get_retry_attempt_label({'current_retry':2,'retry_limit':1}))"
python3 -c "from core_logic import sensevoice_gguf_ready; print(sensevoice_gguf_ready())"
python3 -c "import app; ctx=app.app.app_context(); ctx.push(); conf=app.get_final_config(None); print(conf.get('enable_cloud_asr'), conf.get('local_model_concurrency')); ctx.pop()"
journalctl -u scanner --since "10 minutes ago" --no-pager | grep -E "本地模型资源槽|本地 GGUF 推理资源已释放|本地 GGUF 推理中" | tail -n 80
systemctl restart scanner
systemctl is-active scanner
```

Do not commit:

- `.idea/`
- `__pycache__/`
- secrets such as `.token_secret` or `.flask_secret`
- runtime DB files
- downloaded media
- model files

Also do not commit local GGUF runtime/model artifacts under `models/sensevoice-gguf/`.

Current untracked local directory commonly present:

```text
.idea/
```

Leave it alone unless the user explicitly asks to manage IDE files.

## Git Workflow

Before committing:

```powershell
git status --short --branch
git diff
git log --oneline -5
```

Commit only intended files. Do not include `.idea/` or generated caches.

Typical commit message style in this repo is concise, for example:

```text
optimize subtitle stream scanning
tune cloud asr timeout and reuse audio cache
bump app version to v2026.05.18
```

Push target:

```powershell
git push origin main
```

## Deployment Notes

Known SSH hosts used for this project:

- `netcup`: `152.53.164.190`, port `4571`, path `/www/wwwroot/scanner_web`, service `scanner`.
- `hd东京绕`: `142.91.108.225`, port `4557`, path `/www/wwwroot/scanner_web`, service `scanner`.

The short alias `hd` is not present in `C:\Users\Administrator\.ssh\config`; use `hd东京绕` or the explicit host/port.

Sync files after code changes:

```powershell
scp "E:\Pro_PY\Video-ASR-Ad-Cleaner\app.py" "E:\Pro_PY\Video-ASR-Ad-Cleaner\core_logic.py" netcup:/www/wwwroot/scanner_web/
scp "E:\Pro_PY\Video-ASR-Ad-Cleaner\templates\settings.html" netcup:/www/wwwroot/scanner_web/templates/settings.html
ssh netcup 'cd /www/wwwroot/scanner_web && python3 -m py_compile app.py core_logic.py database.py && systemctl restart scanner && systemctl is-active scanner'
```

For `hd东京绕`, PowerShell/scp can have trouble with the Unicode SSH alias in target syntax. Use explicit IP and port if needed:

```powershell
scp -P 4557 -i "C:\Users\Administrator\.ssh\id_ed25519" "E:\Pro_PY\Video-ASR-Ad-Cleaner\app.py" "E:\Pro_PY\Video-ASR-Ad-Cleaner\core_logic.py" root@142.91.108.225:/www/wwwroot/scanner_web/
scp -P 4557 -i "C:\Users\Administrator\.ssh\id_ed25519" "E:\Pro_PY\Video-ASR-Ad-Cleaner\templates\settings.html" root@142.91.108.225:/www/wwwroot/scanner_web/templates/settings.html
ssh "hd东京绕" 'cd /www/wwwroot/scanner_web && python3 -m py_compile app.py core_logic.py database.py && systemctl restart scanner && systemctl is-active scanner'
```

After deployment, verify key behavior:

```bash
cd /www/wwwroot/scanner_web
python3 -c "import app; ctx=app.app.app_context(); ctx.push(); print(app.get_final_config(None).get('enable_cloud_asr')); ctx.pop()"
python3 -c "from core_logic import ScannerCore; c=ScannerCore(logger_callback=lambda m: None); print(c.get_retry_attempt_label({'current_retry':1,'retry_limit':1})); print(c.get_retry_attempt_label({'current_retry':2,'retry_limit':1}))"
python3 -c "import app; ctx=app.app.app_context(); ctx.push(); print(app.get_final_config(None).get('local_model_concurrency')); ctx.pop()"
systemctl status scanner --no-pager -l | sed -n '1,10p'
```

## Versioning

Version naming currently uses dates:

```text
vYYYY.MM.DD
```

Update `APP_VERSION` in `app.py` when the user asks to bump the visible web version.

The settings page displays the version via:

```python
render_template('settings.html', app_version=APP_VERSION)
```

## Future Improvement Ideas

Do not implement these unless requested.

- Config export/import with versioned JSON.
- Safer installer update mode with backup and rollback.
- Optional image-subtitle handling policy: keep, metadata-only remove, remove all image subtitles, or OCR sampling.
- Formal tests for `ScannerCore` helpers.
- More robust API retry/backoff policy.
- ASR segment cache retention cleanup on process startup.
