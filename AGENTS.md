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
- `templates/index.html`: Main dashboard UI. One-line header (masked public IP badge `1.2.x.x` and free-disk badge `可用 20GB`, hijack remote dropdown and `开启劫持` switch, no extra label; on phones the header stays one line: logo, stacked total download/upload speed (tab speed badges are hidden there), stacked masked IP (hidden below 360px) and free disk space (`盘20GB`), a `劫持` button that opens the hijack panel, and icon buttons with tightened padding; the title text is hidden). IP and disk come from `/api/server_info`, polled every 30s. Both queues show each task's file size (desktop: under the filename; phones: in the meta line). Detect and upload queues share one toolbar plus a status filter row (with counts) and a filename/ID search on desktop; phones collapse all of that into one `.m-toolbar` row (select-all checkbox, status `<select>` with counts, search, `批量` dropdown). Select-all and selection apply to the filtered list. Desktop renders tables (the upload table stacks ID over the status badge in one column, clamps filenames to two lines, shows progress as a translucent fill across the whole row height (`tr.progress-row` + `.row-progress`, `pointer-events: none`; phones keep a thin bottom line) and puts `%` · speed · ETA in one `进度 / 速度` column); phones (<768px) render both queues as compact `.m-item` entries: filename clamped to two lines, one small meta line (`#ID`, colored status text, then detect dots + time or upload speed/% + remote), a `⋯` menu holding `taskActions(t)` (plus `日志` for detect), and a thin progress line for uploads; tapping the entry opens the log. Per-task buttons for both layouts come from `taskActions(t)`; status dots carry tooltips via `dotTitle()`; only `uploading` progress bars are animated. Hotkey: on the detect, upload and downloader tabs, two Space presses within 400ms run `清空历史` (`/api/tasks/clear`) directly without the confirm dialog (`onQueueHotkey`). The same-origin AriaNg iframe gets the listener too (`attachFrameHotkey()`, re-attached on every frame load) because key events inside it do not reach the parent. It is ignored while typing in inputs, while a dashboard or AriaNg modal/dropdown is open, on key repeat or with modifiers, and on the log tab.
- `templates/settings.html`: Settings UI, version badge, keyword management. Sections (desktop left nav, mobile sticky horizontal nav; last section remembered in `localStorage`): `检测`, `识别模型`, `关键词`, `下载与上传`, `队列并发`, `通知`, `清理`, `账户安全`. Every settings row uses the `.set-row` label/description/control layout. One sticky save bar saves all sections (including the Trigger API Token); it tracks unsaved changes against a snapshot, offers `撤销`, warns on page unload, and switches to `保存并重启` when a restart-only key (`concurrency_detect`, `concurrency_upload`) changed. Keywords save immediately, so the bar is hidden there. Account credentials keep their own button.
- `templates/ariang.html`: AriaNg launcher that applies the Scanner-local RPC endpoint before loading AriaNg.
- `templates/login.html`: Login UI.
- `static/ariang-scanner.css`: Scanner-owned visual overrides for the AriaNg static UI.
- `trigger.sh`: Aria2 completion hook.
- `reset_password.py`: Root-local Dashboard password reset tool. It replaces a selected account password with a newly generated one and prints it once.
- `install/install.sh`: One-command-capable interactive install/uninstall helper. It downloads the GitHub `main` archive itself, then configures Aria2/service integration and optional Nginx HTTPS/WSS.
- `install/aria2-config/default.tar`: Scanner-managed Aria2 configuration template, extracted to `/root/.aria2c` only during a new Scanner installation.
- `requirements.txt`: Python dependencies.

## Runtime Architecture

The app starts from `app.py` and launches two worker pools:

- Detection workers consume `detect_queue`.
- Upload workers consume `upload_queue`.
- Detection retries are priority requeues: automatic detection retry, manual detect retry, dynamic-sample retry, and save-and-retry go to the front of `detect_queue`. New `/api/trigger` tasks still go to the back. This ensures a task that already started detection gets retried before never-started tasks.
- 批量直传 (`POST /api/tasks/batch` with `type=detect`, `action=direct_upload`, required `ids`) applies the single-task `直传` override to selected detection tasks in `pending`, `dirty`, `error` or `cancelled`: queued `pending` tasks move to the front, the others are reset to `pending` and requeued at the front. Tasks being detected (`processing`) and upload tasks are skipped and counted in the reply.
- Deleting a running task (`/api/task/<id>/delete`, `/api/tasks/batch_delete`) stops it first and then deletes it: `stop_and_delete_tasks()` stops the core, marks it `cancelled`, adds it to `pending_delete_ids`, and waits up to 5s. Whichever of the request or the worker `finally` block (`finish_pending_delete()`) sees the task no longer active deletes the record and local files; a task that needs longer is deleted automatically when its worker exits. Tests live in `tests/test_task_batch_ops.py`.
- 插队 (`POST /api/tasks/prioritize`) only reorders `detect_queue`: it moves already-queued `pending` detection tasks to the front via `FrontQueue.move_to_front()` without touching status, `retry_count`, or overrides, and without re-running detection. Selection order is preserved (the first selected task ends up first). Non-`pending` tasks and upload tasks are rejected/skipped. A `pending` task that is no longer in the in-memory queue is re-enqueued at the front. The dashboard exposes it as a per-row button and a detect-queue toolbar batch button driven by the shared checkbox selection.

Task lifecycle:

1. `/api/trigger` creates a `Task` row and puts the task ID into `detect_queue`.
2. `detection_worker()` builds final settings and keyword lists.
3. `ScannerCore.process_file()` runs metadata cleanup, subtitle cleanup, and audio detection.
4. Clean files are marked `pending_upload` and queued to `upload_queue`.
5. `upload_worker()` runs `rclone moveto`.

Upload destination override:

- Default destination (`resolve_path_remote()` in `app.py`): the first folder level under `scan_path` names the remote, and files directly in `scan_path` use `rclone_remote`. Deeper download sub-folders are dropped, so `<scan_path>/g01/Season 3/a.mkv` uploads to `g01:a.mkv`. A directory (multi-file) task keeps its own folder name and inner structure (`g01:<task folder>/...`). Paths outside `scan_path` keep the legacy parent-folder rule. Tests live in `tests/test_upload_remote_path.py`.
- The one-time batch `修改remote` action and its `/api/tasks/batch_upload_remote` endpoint were removed; remote hijack is the only toolbar way to redirect uploads.
- Batch queue actions support selected task IDs; with no selection, they retain their all-task behavior.
- A per-task `upload_remote` is stored in task overrides and takes precedence over the default `rclone_remote` only for that task. It does not move local files.
- Running uploads keep the destination they already started with; the override applies when they are retried.
- The dashboard header has global `upload_remote_hijack_enabled`, `upload_remote_hijack_remote` and `upload_remote_hijack_candidates` (newline-separated) settings, served by `/api/upload_remote_hijack`. The remote is picked from a candidate dropdown: `+` adds and switches to a custom remote, `−` removes the selected candidate, and every change saves immediately. When enabled (`开启劫持`), tasks newly entering `upload_queue` and upload retries are assigned that remote; existing pending and running uploads are unchanged. The dashboard re-reads the hijack state every 10s because auto switching changes it server-side. Tests live in `tests/test_upload_remote_hijack.py`.
- Upload-limit auto switch (`upload_remote_auto_switch`, settings page `下载与上传` -> `上传策略`, default off): `ScannerCore.is_upload_limit_error()` matches rclone `warning`/`error` log lines against `UPLOAD_LIMIT_RE` (Drive `userRateLimitExceeded` / `upload limit exceeded`, `storageQuotaExceeded`, `teamDriveFileLimitExceeded`, `dailyLimitExceeded`, `quotaExceeded`, OneDrive `quotaLimitReached` / insufficient storage) and sets `upload_limit_hit`. On such a failure `switch_upload_remote_on_limit()` marks the task's remote exhausted for 24h (process-local, cleared on restart), picks the next non-exhausted hijack candidate in list order, sets it as the task `upload_remote`, enables hijack with that remote, and requeues the task without consuming `retry_count`. When no candidate is left, the task errors with `账号超限，无可切换的候选 remote`. Tests live in `tests/test_upload_limit_switch.py`.
- Slow-upload restart (`upload_slow_restart`, same section, default on) gates the existing watchdog: files >= 500MB uploading below 3MB/s for 35s are restarted, at most 3 times.

AriaNg download manager:

- The Dashboard `下载器` tab embeds the `/aria2/` launcher, which loads static files from `/ariang/`.
- `/api/aria2/jsonrpc` is the authenticated same-origin RPC proxy. It reads the local Aria2 port and secret server-side; never expose the secret to browser code.
- The launcher always resets the embedded AriaNg RPC configuration to Scanner's authenticated same-origin proxy at `api/aria2/jsonrpc` and clears the browser-side secret. The embedded RPC settings UI must remain hidden.
- The Dashboard polls Aria2 global stats every 3 seconds regardless of the active tab so the `下载器` navigation badge remains current. The badge counts active plus waiting downloads.
- The download tab can import, export, or clear the current browser's `AriaNg.Options` JSON. This is browser-local only and must not be sent to the server; clearing it reloads the launcher defaults.
- The AriaNg static package is installed outside Git under `ariang/`; do not commit it. The installer downloads official AriaNg 1.3.14 `AllInOne` plus the same-tag source `src/views` templates when `index.html`, `views/settings-ariang.html`, or its `.scanner_ariang_allinone_1.3.14` marker is missing. Do not use the ordinary release ZIP alone because it lacks the Angular settings templates needed by the dashboard.
- When the optional Nginx HTTPS configuration finds `enable-rpc=true` and a non-empty `rpc-secret` in the Aria2 config, it exposes direct external RPC at `wss://<domain>/jsonrpc`. It proxies only to local Aria2. The installer prints the generated secret once after installation; never log or otherwise expose it.
- External clients use the Nginx direct route at `https://<domain>/jsonrpc` or `wss://<domain>/jsonrpc` on port `443` with their existing Aria2 `rpc-secret`. The legacy `/aria2/jsonrpc` route remains accepted. This route must not depend on Scanner login or `X-API-Token`. Never derive, print, or inject the Aria2 secret into browser code.

Image/NFO download rejection:

- The settings page has independent `discard_images` and `discard_nfo` switches (default `true`) plus `discard_extra_extensions` (comma/whitespace-separated extensions, empty by default). `DISCARD_DOWNLOAD_EXTENSIONS` in `core_logic.py` is the built-in image/NFO list. Custom extensions can include other single-file types but `.mp4` and `.mkv` are rejected to avoid blanket video deletion; size-based small-video rejection was rolled back because fast downloads complete before the sweeper can inspect them.
- `/api/aria2/jsonrpc` filters `aria2.addUri` requests whose `out` option (preferred) or, without `out`, every URI matches an enabled built-in or custom discarded extension. The proxy never forwards them and answers with a synthetic GID so AriaNg stays consistent. `system.multicall` is filtered per inner call and the merged reply keeps the original call order and length.
- `aria2.addTorrent` / `aria2.addMetalink` and multi-file tasks are never filtered; a torrent that contains images keeps all its files.
- `discard_download_worker()` polls `aria2.tellActive` / `aria2.tellWaiting` every 5s as the fallback for downloads added through the Nginx direct RPC route. It force-removes matching single-file non-torrent tasks, deletes the partial file and `.aria2` control file only inside `scan_path`, and removes the download result.
- Tests live in `tests/test_discard_downloads.py`.

## Installer And Nginx

- `app.py` reads `SCANNER_PORT` with a default of `5000`. The installer writes the chosen port to the installed project's `scanner.env`; systemd reads it through `EnvironmentFile`, and `trigger.sh` reads the same file for its local callback URL.
- On a new database, `app.py` creates a random initial username and password. It writes them once to a root-only `.initial_admin_credentials` file; the installer displays the values after Scanner starts and deletes the file. Existing user accounts are never reset during reinstall.
- The installer installs P3TERX Aria2-Pro-Core to `/usr/local/bin/aria2c` when no custom binary is already present; it does not install the APT `aria2` package. On a new installation, it extracts `install/aria2-config/default.tar` to `/root/.aria2c` and generates a new `rpc-secret`; it leaves every other template setting unchanged. If `/root/.aria2c` lacks its `.scanner-managed` marker, the installer prompts for an alternate absolute configuration directory rather than overwriting it. To enable automatic Scanner callbacks, the user must add `on-download-complete=<project>/trigger.sh` themselves.
- Installer menu option `2. 更新` downloads the latest project archive, updates dependencies and restarts Scanner. It preserves the installed project path, database, `scanner.env`, Aria2 config/`rpc-secret`, Nginx, model files and existing AriaNg data; it does not rerun interactive network or Nginx configuration.
- For public mode, the installer checks whether TCP `5000` is listening with `ss` (or `netstat`). If occupied, it chooses an unused random port in `10000-42767` and prints the actual direct-access URL.
- If no `aria2.service` or `aria2c.service` exists, the installer registers `aria2.service` with the selected config path and `--daemon=false`. If an existing unit points to a missing executable, it replaces that broken unit with the current Aria2 binary. It never kills an already manual Aria2 process just to adopt it under systemd.
- Nginx HTTPS is opt-in. The installer prompts for a domain or IP and accepts either existing fullchain/key file paths or pasted PEM content terminated by a line containing `EOF`. Pasted key files use mode `0600`; certificate and key syntax are checked before use.
- The generated Nginx site proxies Scanner to HTTPS port `5001`; if `5001` is occupied, the installer chooses an unused random port in `10000-42767`. HTTP redirects and the printed Scanner address use the selected Scanner port. When Aria2 RPC is enabled with a secret, a separate HTTPS/WSS virtual host on `443` exposes `/jsonrpc` and retains `/aria2/jsonrpc` for compatibility; both forward to the configured local `rpc-listen-port` (default `6802`). The installer stops if `443` is held by a non-Nginx service. It validates with `nginx -t` and restores the preceding Scanner site configuration if validation fails. Do not overwrite a non-symlink `/etc/nginx/sites-enabled/scanner` file.
- During uninstall, Scanner reads its project directory from `scanner.service` and its Aria2 configuration directory from `scanner.env`; do not prompt for installation paths again. It removes the recorded Aria2 configuration directory only when it contains the `.scanner-managed` marker, and also removes a Scanner-registered Aria2 service. It leaves a non-Scanner Aria2 configuration and all other Aria2 runtime files untouched. Removal of the Scanner Nginx site and pasted certificate files is optional. Purging the Nginx package is offered only when Scanner installed it and no other enabled Nginx sites are detected; retain Nginx in every other case.

Important statuses:

- `pending`: waiting for detection.
- `processing`: detection is running.
- `pending_upload`: waiting for upload.
- `uploading`: upload is running.
- `uploaded`: complete.
- `dirty`: blocked by audio keyword match.
- `error`: failed after retry policy.
- `cancelled`: manually stopped.

Server info and file sizes:

- `GET /api/server_info` (login required) returns the masked server IP and `shutil.disk_usage()` of `scan_path` (falls back to the project directory when `scan_path` is missing). The disk badge turns red below 10GB or 5% free.
- The public IP is looked up from `PUBLIC_IP_SERVICES` in a background thread and cached for 6h (retry after 5min on failure), so page requests never wait on an external service. Until it is known, the outbound local IP is shown. `mask_ip()` keeps the first two IPv4 octets (`1.2.x.x`). Telegram notifications still use `get_server_ip()`.
- `/api/tasks` returns `file_size` (bytes or `null`) from `get_task_file_size()`: live size of the file when it still exists, otherwise the `_file_size` override. `_file_size` is recorded when the task is created and refreshed for single-file tasks when an upload starts, so uploaded tasks keep their size. Directory tasks prefer the stored total because uploads move their files out; live directory sizes are cached for 30s. Tests live in `tests/test_server_info.py`.

Status probe endpoint:

- `GET /api/status` is a read-only busy/idle probe for external orchestration
  (multi-server automation, media organizers, dashboards). It authenticates with
  the same `X-API-Token` header as `/api/trigger` and requires no login session.
- It never mutates state. It reports `detect_pending`, `upload_pending`,
  `in_memory_active`, `queue_depth`, Aria2 `numActive`/`numWaiting`,
  `aria2_paused` and `aria2_waiting_runnable`, plus a `busy`/`idle` verdict.
- Aria2 includes paused downloads in `numWaiting`; the probe pages through
  `aria2.tellWaiting` and treats only runnable waiting and active downloads as busy.
- The verdict is fail-safe: when the database or Aria2 RPC cannot be read, or
  waiting jobs cannot all be classified, it reports `busy`. A caller must never
  treat an unknown state as idle.

## Settings And Defaults

Global settings are stored in the `Config` table and merged by `get_final_config()` in `app.py`.

Common settings:

- `check_audio`: enable ASR audio checks.
- `check_subtitles`: enable subtitle checks and subtitle-track removal.
- `sanitize_metadata`: enable metadata cleanup.
- `enable_cloud_asr`: enable cloud ASR requests. When disabled, audio checks skip cloud requests and go directly to local GGUF ASR if `enable_local_model` is enabled.
- `enable_local_model`: allow local GGUF model fallback. When cloud ASR is enabled, fallback is only allowed after cloud retries are exhausted. When cloud ASR is disabled, fallback is allowed immediately.
- `local_model_concurrency`: maximum number of simultaneous local GGUF inference subprocesses. Default is `2`; raising it increases CPU and memory pressure.
- `detailed_mode`: log full recognized text even for successful checks.
- `concurrency_detect`: detection worker count. Requires restart to take effect.
- `concurrency_upload`: upload worker count. Requires restart to take effect.
- `audio_segment_len`: unified ASR segment length in seconds. Default is `360`.
- `audio_max_segments`: maximum number of dynamic ASR samples. Default is `8`.
- `audio_threshold_multi`, `audio_threshold_long`, `audio_len_head`, `audio_len_mid`, `audio_len_tail`, `audio_len_tail_long`: legacy keys kept for old DB/config compatibility. Do not expose them again unless explicitly requested.
- `api_url`, `api_key`, `api_model`: cloud ASR config.
- `cloud_asr_api_keys`: newline-separated cloud ASR API key pool. The settings page edits it as one key per row and keeps `api_key` as the first key for compatibility.
- `cloud_asr_concurrency`: maximum simultaneous cloud ASR requests in the current Flask process. Default is `3`.
- `cloud_asr_per_key_concurrency`: maximum simultaneous cloud ASR requests per individual API key. Default is `3`. Key pool capacity is `key count x this value`; when that capacity is below `cloud_asr_concurrency`, the effective global limit is degraded to the capacity and the task log records it.
- `cloud_asr_max_duration`: maximum cloud chunk duration to send to cloud ASR. Default is `60`; longer ASR samples are split into multiple cloud chunks. Set to `0` to disable cloud chunking.
- `cloud_asr_upload_timeout`: cloud ASR audio upload/connect timeout. Default is `20`.
- `cloud_asr_read_timeout`: cloud ASR normal recognition read timeout. Default is `120`.
- `cloud_asr_long_read_timeout`: cloud ASR recognition read timeout for samples with duration `>= 450s`. Default is `180`.
- `cloud_asr_proxy_enabled`: enable the optional proxy for cloud ASR audio upload requests.
- `cloud_asr_proxy`: optional proxy URL used only for cloud ASR audio upload requests when `cloud_asr_proxy_enabled` is true. Supports `http://user:pass@host:port` and `socks5h://user:pass@host:port` formats when SOCKS support is installed.
- Cloud ASR proxy changes do not require a service restart. New tasks use the saved setting immediately, and running audio tasks refresh only the proxy fields before each cloud upload request. A request that is already in flight keeps the proxy it started with.
- `scan_path`, `rclone_remote`: local download root and default remote.
- `cleanup_detect_dirty`, `cleanup_detect_error`, `cleanup_detect_cancelled`: independently control removal of intercepted, errored, and cancelled records from the detection queue. All default to `true`.
- `cleanup_upload_uploaded`, `cleanup_upload_error`, `cleanup_upload_cancelled`: independently control removal of completed, errored, and cancelled records from the upload queue. All default to `true`.
- `cleanup_scanner_uploaded`, `cleanup_scanner_dirty`, `cleanup_scanner_error`, `cleanup_scanner_cancelled`: legacy global cleanup keys retained only to migrate existing saved settings into the queue-specific choices.
- `cleanup_aria2_completed`: controls whether Dashboard `清空历史` removes Aria2 `complete` result records. Default is `true`; failed and removed Aria2 records are always retained.
- `cleanup_scanner_history`: legacy compatibility key. When present without any legacy per-status or queue-specific key, it supplies the initial value for all Scanner cleanup statuses.
- `清空历史` never deletes local media or `.aria2` resume files. When no cleanup items are enabled, it makes no changes. Aria2 cleanup failures are reported without silently treating them as success.

Sensitive values include `api_key`, `cloud_asr_proxy`, `tg_bot_token`, `tg_chat_id`, and `api_token`. Do not print or commit real secrets.

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
- The remux clears all stream tags, then `get_track_label_restore_args()` re-applies audio and subtitle track labels so players (Jellyfin) can still tell tracks apart: `language` codes are restored as-is, and `title` goes through `clean_track_title()`, which removes only the metadata-keyword hits plus adjacent separators (`..`, `@`, `|`, spaces) and keeps the rest (`GyWEB..国语` -> `国语`, `双语特效@KKYY` -> `双语特效`). Titles with no hit are kept verbatim; a title left empty is dropped while the language stays. Audio output indexes follow the mapped (copyable) audio streams.
- Do not hardcode ad markers or language-name allowlists in code. New markers belong in the metadata keyword table; a keyword that is really a language descriptor (e.g. `Mandarin`, removed from the defaults) must be removed from the table instead.

Important: unknown audio streams such as `av3a` may make MP4 remux fail if mapped blindly. Keep `get_safe_audio_map_args()` in metadata/subtitle-remux paths.

### Subtitle Cleanup

Implemented in `ScannerCore.check_subtitles()`.

Current optimized flow:

1. `get_subtitle_streams()` uses one `ffprobe` JSON call to collect subtitle `index`, `codec`, `language`, `title`, and `handler_name`.
2. Track labels (`language`, `title`, `handler_name`) are never used to remove a subtitle track. A keyword hit in a label is not evidence the dialogue is dirty, and labels are what players use to tell languages apart. Ad text in titles is handled by metadata cleanup (see above).
3. Text subtitle tracks are batch-exported with one `ffmpeg` command into temporary files in their own format (`ass`/`ssa` -> `.ass`, `webvtt` -> `.vtt`, others such as `subrip`/`mov_text` -> `.srt`).
4. Only cue/dialogue text (override tags stripped) is scanned for subtitle keywords after zero-width normalization.
5. Image subtitle tracks are not OCR-scanned and not modified.
6. A text track whose content hits a keyword is modified, not removed: `clean_subtitle_content()` drops every timed cue / ASS `Dialogue` event in which any line hits, including the other lines of that event (e.g. a 3-line ad event `永裴资源君\N更多实时同步更新优品资源\Nhttps://link3.cc/...` is removed as a whole). Events at other times stay. Losing a normal line that shares an event with an ad line is an accepted cost. If no dialogue remains or the cleaned text still hits, the whole track is removed.
7. One remux writes video, safe audio streams, untouched subtitle tracks, and the cleaned files in the original track order. Cleaned tracks keep language/title via `-map_metadata` and `default`/`forced` disposition; MP4/MOV outputs encode them as `mov_text`. Logs report `✂️ 字幕轨 #N 剔除命中事件（共 X 行文本），保留该轨` and the remux time in `✅ 字幕清洗完成，用时 Xs`.

Image subtitle codecs currently treated as non-text:

- `hdmv_pgs_subtitle`
- `dvd_subtitle`
- `dvb_subtitle`
- `xsub`

Important behavior for PGS/image subtitles:

- Track titles are not used to remove image tracks; image tracks cannot be edited and are kept.
- Subtitle image content is not checked.
- OCR is not implemented and should not be added casually because full OCR can be very slow.

Known Task-3430 example:

- File had 6 `hdmv_pgs_subtitle` tracks.
- Log showed `字幕轨 6 条，待扫文本轨 0 条，图片轨 6 条`.
- The system scanned metadata only and retained tracks because no keyword matched.

### Audio ASR Detection

Implemented in `ScannerCore.scan_audio_cloud_fallback_local()`.

Segment planning:

- Dynamic mode uses `audio_segment_len` as `L` and `audio_max_segments` as the cap. With defaults, `L = 360s` and max samples is `8`.
- If duration is `<= 3L`, dynamic mode scans one full segment named `01全片`.
- If duration is `> 3L`, dynamic mode scans evenly spaced samples in this order: `01片头`, `02片尾`, then `03抽样` onward.
- Dynamic sample count is `4` for `3L-4L`, `5` for `4L-5L`, `6` for `5L-7L`, then about `80%` coverage capped by `audio_max_segments`.
- Non-dynamic mode uses the same `audio_segment_len`: scan tail first, then middle and head when duration exceeds `3L`.
- Terminology matters in logs and UI: video/audio sampling units are `段` or `抽样段`; cloud upload splits are `块` or `切块`. Do not call 60s cloud chunks `段`.
- The dashboard audio indicator should reflect actual planned sample count from task logs such as `动态抽样开启: 5段...` or `动态抽样开启: 全片...`, not the maximum cap `audio_max_segments`.

Segment scheduling:

- Metadata and subtitle checks remain synchronous because they are fast and can rename/remux the file before audio starts.
- Pending audio segments for the same task run concurrently up to available ASR capacity. With cloud ASR enabled, same-task segment workers are capped by the effective cloud limit (`min(cloud_asr_concurrency, key count x cloud_asr_per_key_concurrency)`); with cloud disabled, they are capped by `local_model_concurrency`.
- Local GGUF slots use the same task-priority semantics as cloud slots: earlier audio tasks with waiting local fallback requests get priority for newly freed local inference slots.
- Cloud ASR slots are task-prioritized. A task that entered audio detection earlier gets priority for newly freed API slots while it has waiting segment requests; later video tasks only use slots that are not currently needed by earlier audio tasks.
- Segment completion can be out of order. Each passed segment is checkpointed by name and logged as a green light; the task only succeeds after every planned segment is green.
- A failed segment should not interrupt other already scheduled segments from the same task. Let siblings finish and checkpoint green segments, then retry the failed segment on the next task retry. Only dirty keyword hits should stop remaining segments immediately.
- Child segment workers must not write DB logs or checkpoints directly. They queue logs in memory; the parent detection worker flushes logs, saves `_passed` and `_audio_segments`, and updates progress from the Flask app context.

Cloud timeout policy:

- Upload/connect timeout: `cloud_asr_upload_timeout`, default `20s`.
- Read timeout: `cloud_asr_read_timeout`, default `120s`.
- Long-sample read timeout: `cloud_asr_long_read_timeout`, default `180s`, used when the current audio segment duration is `>= 450s`.

The log includes the timeout value:

```text
☁️ 云端识别中... (timeout=上传20s/识别120s)
```

Cloud failure policy:

- Cloud ASR requests are gated by a process-local global limiter. `cloud_asr_concurrency` defaults to `3`. Each individual key is additionally capped by `cloud_asr_per_key_concurrency` (default `3`); requests choose keys from `cloud_asr_api_keys` round-robin while skipping keys already at their per-key cap, falling back to legacy `api_key` when the key pool is empty. When `key count x per-key cap` is below the global limit, the effective limit degrades to that capacity so workers do not park on slots that can never open.
- Historical SiliconFlow behavior on netcup: real speech FLAC segments above `60s` once returned empty-body `500`, while `60s` returned `200`. A later direct endpoint test on 2026-07-06 showed `61s`, `90s`, `120s`, and `180s` real samples all returned `200`; keep chunking as a stability fallback unless deliberately retuning.
- `cloud_asr_max_duration` defaults to `60`. Longer ASR samples are split into overlapping cloud chunks of at most this duration; the current overlap is `2s`, implemented by moving the next chunk start earlier, not by exceeding the max chunk duration. Chinese logs should use `云端切块识别`, `块`, and `每块≤60s`. The sample passes cloud detection only after every chunk succeeds and no chunk hits an audio keyword.
- `detect_retry_limit` is the number of automatic retries after the first attempt, not total attempts. For example, `detect_retry_limit = 1` means two total attempts and logs should show `第1/2次` then `第2/2次`.
- For retry attempts before the retry limit, local model fallback is forced off.
- After cloud retries are exhausted, local fallback follows the user's `enable_local_model` setting.
- If `enable_cloud_asr` is false, cloud upload is skipped and local GGUF ASR runs immediately when `enable_local_model` is true.
- If `enable_cloud_asr` is false and `enable_local_model` is also false, the task fails with a configuration error instead of being requeued.

Checkpoint behavior:

- Successful segments are stored in task overrides as `_passed` and `_audio_segments[name].status = passed`.
- Failed segments are stored as `_audio_segments[name].status = failed` and shown as red audio dots in the dashboard.
- On retry, already successful segments are skipped; failed or missing segments are retried.

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

- `local_model_concurrency` defaults to `2` and is exposed on the settings page under `识别模型` -> `本地模型` -> `本地推理并发数`.
- The limiter is process-local. It gates simultaneous GGUF subprocesses inside the current Flask process, not across multiple independent service processes.
- Logs should show slot accounting, for example `等待本地模型资源槽... (并发上限 2)`, `获得本地模型资源槽 (1/2)`, and `本地 GGUF 推理资源已释放 (运行中 0/2)`.
- On netcup, old logs before this setting showed the old single lock worked correctly: multiple tasks waited, but only one task held GGUF inference at a time. After deployment with default `1`, logs showed Task 7127 held `(1/1)` while Task 7128/7129 waited. The current default is `2`.
- Raising this above `2` can improve throughput only if CPU and memory headroom exist. Watch `systemctl status scanner` memory and CPU before increasing further.

Important Linux runtime behavior:

- On `hd东京绕`, the official prebuilt Linux ARM64 runtime started with `--help` but crashed during inference with `code=-4` / `SIGILL`; the cause was CPU instruction incompatibility on `aarch64` `Neoverse-N1`.
- On `hd东京绕` x64/Debian 12, the official prebuilt Linux x64 runtime failed with `GLIBC_2.38 not found` because the server has glibc 2.36.
- The downloader now builds runtime from source on Linux x64 and ARM64 with `GGML_NATIVE=OFF` and writes an architecture-specific marker such as `models/sensevoice-gguf/runtime-source-build-linux-x64.txt` or `runtime-source-build-linux-arm64.txt`.
- Non-Linux platforms still use the matching prebuilt runtime package when available.
- If local GGUF inference fails with `code=-4`, `SIGILL`, or `GLIBC_*. not found`, rebuild the runtime on that server instead of redownloading the prebuilt binary.

Manual rebuild on Linux x64 or ARM64:

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

Run the unit tests (install `requirements.txt` first; the subtitle remux test is skipped when `ffmpeg`/`ffprobe` are missing):

```powershell
python -m unittest discover -s tests
```

Each test file also runs standalone, for example `python tests/test_subtitle_line_cleaning.py`. Importing `app` generates `.flask_secret`; delete it if a test run created it and never commit it.

For targeted behavior tests, prefer small synthetic media files created in the system temp directory, not in the repository.

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
journalctl -t arup --since "10 minutes ago" --no-pager | grep -E "本地模型资源槽|本地 GGUF 推理资源已释放|本地 GGUF 推理中" | tail -n 80
systemctl restart scanner
systemctl is-active scanner
```

### Where Scanner Logs Go

Scanner writes to two separate sinks. Pick by message source, not by habit.

- `ScannerCore.log()` sends every task-level message to syslog under the tag `arup`
  (`core_logic.py`), so it reaches journald. Both `journalctl -t arup` and
  `journalctl -u scanner` show these; `-t arup` is the precise filter and is what the
  dashboard's 系统日志 tab uses through `/api/system_logs`.
- Plain `print()` from `app.py` — worker startup banners, queue recovery, the image/NFO
  discard notices — goes to stdout, which the installer's unit redirects to
  `<project>/scanner.log` (`StandardError` to `scanner_error.log`). **These never reach
  journald at all.** Grepping any `journalctl` invocation for them returns nothing, which
  is not evidence that the code did not run; confirm in `scanner.log` instead.
- Task logs embed recognized ASR text, so a keyword grep over journald can match speech
  content rather than a real log line. Grep for the full emoji-prefixed message when you
  need certainty.
- `scanner.log` and `scanner_error.log` grow without rotation; check size before dumping them whole.

```bash
cd /www/wwwroot/scanner_web
journalctl -t arup -n 100 --no-pager           # task/detection/ASR logs
tail -n 100 scanner.log                         # app.py prints, e.g. discard notices
grep -F "🚫 已丢弃下载任务" scanner.log | tail -n 20
```

Do not commit:

- `.idea/`
- `__pycache__/`
- generated `scanner.env`
- secrets such as `.token_secret`, `.flask_secret`, or `.initial_admin_credentials`
- runtime DB files
- runtime logs `scanner.log` and `scanner_error.log`
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

Deployment servers are reached through SSH aliases defined in the maintainer's local `~/.ssh/config` (for example `netcup` and `hd东京绕`). Every server uses path `/www/wwwroot/scanner_web` and service `scanner`.

Never commit server IP addresses, SSH ports, usernames, private key paths, or local machine paths to this repository. Keep them only in the local SSH config; if an alias is awkward on the command line (such as a Unicode alias in PowerShell `scp` target syntax), add an ASCII alias to the local SSH config instead of writing the IP here.

Sync files after code changes (`<host>` is the SSH alias, run from the repository root):

```powershell
scp app.py core_logic.py <host>:/www/wwwroot/scanner_web/
scp templates/settings.html <host>:/www/wwwroot/scanner_web/templates/settings.html
ssh <host> 'cd /www/wwwroot/scanner_web && python3 -m py_compile app.py core_logic.py database.py && systemctl restart scanner && systemctl is-active scanner'
```

For installer-only changes, sync and syntax-check it without restarting Scanner:

```powershell
scp install/install.sh <host>:/www/wwwroot/scanner_web/install/install.sh
ssh <host> 'bash -n /www/wwwroot/scanner_web/install/install.sh'
```

After deployment, verify key behavior:

```bash
cd /www/wwwroot/scanner_web
python3 -c "import app; ctx=app.app.app_context(); ctx.push(); print(app.get_final_config(None).get('enable_cloud_asr')); ctx.pop()"
python3 -c "from core_logic import ScannerCore; c=ScannerCore(logger_callback=lambda m: None); print(c.get_retry_attempt_label({'current_retry':1,'retry_limit':1})); print(c.get_retry_attempt_label({'current_retry':2,'retry_limit':1}))"
python3 -c "import app; ctx=app.app.app_context(); ctx.push(); print(app.get_final_config(None).get('local_model_concurrency')); ctx.pop()"
systemctl status scanner --no-pager -l | sed -n '1,10p'
```

### Netcup Aria2 Next Test

`netcup` was used to test `aria2-next v2.5.2` on ARM64. The binary, HTTPS download, JSON-RPC, Scanner RPC, global-option writes, and configured completion-hook setting all worked. It was restored to the existing `aria2 1.36.0` afterward; do not treat aria2-next as an installer default yet.

- Only switch when `aria2.getGlobalStat` reports zero active and waiting tasks.
- Back up both `/usr/local/bin/aria2c` and `/root/.aria2c/aria2.conf` before replacing anything. Keep the timestamped paths for rollback.
- Download the official `aria2-next-<version>-linux-aarch64` release and verify it against the matching `checksums.sha256` before installation.
- The current P3TERX configuration needs these aria2-next compatibility changes: set `console-log-level=info`; comment out unsupported `bt-detach-seed-only` and `retry-on-400`, `retry-on-403`, `retry-on-406`, `retry-on-unknown` keys. These changes alter retry behavior, so they are test-only until deliberately adopted.
- Keep `/usr/local/bin/aria2-next` as the downloaded binary and replace `/usr/local/bin/aria2c` only for the systemd unit. Then run `systemctl restart aria2c.service` and verify `aria2.getVersion`, `aria2.getGlobalOption`, and `aria2.getGlobalStat` through `app.call_aria2_rpc()`.
- Roll back by restoring the saved binary and config, then run `systemctl restart aria2c.service`. Never print `rpc-secret` while testing.

## Versioning

Version naming currently uses compact dates:

```text
vYYYYMMDD
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
- Broader test coverage for `ScannerCore` media helpers.
- More robust API retry/backoff policy.
- ASR segment cache retention cleanup on process startup.
