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
- `enable_local_model`: allow local model fallback after cloud retries are exhausted.
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

- `RETRY_LIMIT = 3` in `detection_worker()`.
- For retry attempts before the retry limit, local model fallback is forced off.
- After cloud retries are exhausted, local fallback follows the user's `enable_local_model` setting.

Checkpoint behavior:

- Successful segments are stored in task overrides as `_passed`.
- On retry, already successful segments are skipped.
- The failing segment is retried.

Failed-segment audio cache:

- Failed segment WAV is kept in `/tmp/scan_<task_id>_<segment>.wav` for retry reuse.
- A JSON sidecar validates source path, file size, mtime, segment name, start, duration, and audio map.
- On retry, a valid cache logs `♻️ 复用音频` and skips ffmpeg extraction.
- On successful recognition, dirty hit, local model failure, or final no-retry attempt, cache is removed.

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

Local model fallback can use a lot of memory. `drop_caches()` tries to release memory on Linux after local inference. Be careful changing this path.

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

Do not commit:

- `.idea/`
- `__pycache__/`
- secrets such as `.token_secret` or `.flask_secret`
- runtime DB files
- downloaded media
- model files

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
