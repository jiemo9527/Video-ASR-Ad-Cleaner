# Video-ASR-Ad-Cleaner — 项目约定

Flask + SQLAlchemy 媒体检测、清理与上传面板，连接 Aria2、ffmpeg/ffprobe、云/本地 ASR 和 rclone。

## 文档边界
- 本文件保存必须常驻上下文的核心约定，目标 ≤20,000 字符，不累积发布流水账。
- 详细行为见 [docs/agents-reference.md](docs/agents-reference.md)：Runtime Architecture（队列/下载器）、Installer And Nginx（安装/监控）、Settings And Defaults、Media Processing Details（字幕/ASR/GGUF）、Development Workflow（日志/测试）。修改对应功能前查阅对应段落，行为变化同步更新核心和细节，按代码/测试核对文档。
- 主机、账号、私有地址留本地运维 skill，不进入仓库。不要把机密移入参考文档。

## 入口
- `app.py`：路由、队列、设置、worker、恢复，APP_VERSION 为可见版本；`core_logic.py`：媒体/ASR/上传；`database.py`：Task/Config/Keyword/User。
- `templates/index.html`、`settings.html`、`downloader.html`：主面板/设置/独立下载器；`static/downloader.js` / `.css` 的 Vue scanner-downloader 是唯一下载 UI，不恢复 AriaNg/iframe/downloader_ui。
- `monitor.py`：监控；`trigger.sh`：下载完成回调；`install/install.sh`：systemd 安装更新；`install-nosystemd.sh` / `supervise.sh`：非 systemd 主机。

## 队列和上传
- `/api/trigger` → detect_queue → ScannerCore.process_file → pending_upload → upload_queue → rclone moveto。
- 检测重试进队首，新任务进队尾。插队只移动 pending 检测任务，保留选择顺序，不改状态/重试次数/覆盖项；批量直传跳过 processing 和上传任务。
- 运行中删除先停，待 worker 释放后由 finish_pending_delete 删除记录和本地文件。停止后重提保留新的 pending/pending_upload，不被旧 worker 写回 cancelled，并在释放占用后重新入队。
- 默认 remote 取 scan_path 下首层目录，散文件用 rclone_remote；单文件丢弃更深目录，多文件任务保留任务目录及内部结构。任务 upload_remote 优先但不搬本地文件。
- remote 劫持只影响新入上传队列及重试，不改变等待/运行上传；保留候选即时保存和服务端状态轮询，不恢复一次性批量修改 remote。
- 额度失败按候选顺序切换，remote 暂记耗尽 24h，切换不消耗 retry_count，无候选报错；慢速重启保留开关/上限。重启按启动恢复逻辑排队在途任务，上线前检查实际状态。

## 媒体和 ASR
- 命令优先用 ScannerCore.run_cmd，保留超时/停止杀子进程与跨平台处理。remux 必用 get_safe_audio_map_args，避免未知音轨 av3a 失败。
- 关键词用 find_keywords/normalize_scan_text 处理零宽字符；广告词来自关键词表，不硬编码或加语言白名单。轨 language 保留，title 仅清理命中及相邻分隔符。
- 字幕一次批量导出，不恢复逐轨全文件扫描。仅按正文的 cue/Dialogue 事件匹配、删完整命中事件；事件内拼多行，不跨事件拼。检测/清理共用 subtitle_event_scan_text。
- 清理命中事件而非整轨，清空正文才删轨；图像字幕不 OCR/不修改，轨标题不是删轨证据。remux 保留轨序、所有流显式 metadata 映射、default/forced；MP4/MOV 清理字幕为 mov_text。
- 视频抽样叫“段”，云上传拆分叫“块”。默认 audio_segment_len=360s、audio_max_segments=8；动态规划和非动态尾/中/头规则见参考文档，不恢复 legacy 分段 UI。
- 云 ASR 有全局和每 key 限额，有效容量取较小值；云/本地槽优先较早音频任务。同任务分段可并发、乱序；普通失败不打断已排兄弟段，dirty 才立即停余段。
- 子线程仅排内存日志，父 worker 在 Flask context 写日志/checkpoint/进度。通过段 _passed/_audio_segments 保存后重试跳过，失败段保留状态。
- detect_retry_limit 是首次之后的重试数；耗尽云重试前禁本地 fallback，之后尊重 enable_local_model。云关时可直接本地，两者皆关明确失败不循环排队。
- 云默认每块≤60s、重叠2s（提前下一块起点而非超长）；全块成功且无关键词才通过。代理变化新请求即时读，已发请求保留旧配置。
- 失败音频缓存重用前校验源路径/大小/mtime、段位置/长度/audio map；成功/dirty/最终失败按原策略清理。
- 本地用 SenseVoice GGUF/llama.cpp，不恢复 PyTorch/FunASR；Linux x64/ARM64 用 GGML_NATIVE=OFF 源码构建。SIGILL/GLIBC 不兼容在目标机重建，不反复下载同一预编译包；--help 不是实际推理验证。

## 下载、安全和持久化
- `/api/aria2/jsonrpc` 是登录保护的同源代理，RPC secret 留服务端。外部 `/jsonrpc`（兼容 `/aria2/jsonrpc`）由 Nginx 直连 Aria2、用原 secret，不依赖 Scanner 登录/Trigger token。
- 列表不拉完整 files，详情再读。新建目录默认 scan_path。下载 UI 删除不删文件；清已完成/全部结果需确认。
- 重命名运行中单文件不能只改 out：暂停→验证并改媒体/.aria2 名→changeOption→恢复；失败回滚，原暂停状态和队列槽顺序保留。
- 无进度自动暂停释放槽、不自动恢复；完成/做种排除。磁盘保护始终启用，不加禁用开关：不足停 active/waiting、阻止恢复，空间恢复仅恢复它停的任务；sys_disk_guard 持久化，拒绝设置写入且不进备份恢复。
- 丢弃后缀只针对单文件非 BT，torrent/metalink/多文件不拦；不允许 .mp4/.mkv 全局丢弃。清文件仅限 scan_path 内。
- Config 经 get_final_config 合并，worker 并发改动需重启。“清空历史”按独立队列/状态设置只删记录，默认仅已完成，不删媒体/.aria2，失败不伪报成功；不要混用运行任务删除语义。
- `/api/status` 为 X-API-Token 只读探针，waiting 包含 paused；未知/读取失败均 busy，不判 idle。
- 跨站监控仅 origin-bound 只读 grant，精确 allowlist 和 popup window/origin/state 校验；CORS 不全局开，不拷贝登录 Cookie/密码，grant 不进 URL；401 清 grant，网络错误保留。
- 监控仅白名单，不含 transcript/日志/设置/路径/URI/Cookie/secret；未知不当0、离线不显示旧值。详细授权/指标/测试见参考文档。

## 安装、UI 和日志
- 更新保留路径、DB、scanner.env、模型、Aria2 配置/secret、Nginx；只卸载 .scanner-managed 配置，不重置已有账号。初始随机凭据仅展示一次后删临时文件。
- SCANNER_HOST/PORT 从 scanner.env 读，不改代码写死；无 systemd 部署保留 supervisor 锁/fd 关闭、持久化和 shell 恢复钩子。Nginx 改动先校验，失败回滚，不影响其他站点。
- 设置分区/统一保存栏保留，关键词即时保存，账户单独保存。移动端保持紧凑单行头部、过滤范围选择、两行文件名、统一 taskActions、整行半透明进度，不恢复细进度条。
- UI 音频进度用实际计划段数，不用上限；日志先 HTML 转义，高亮按 Task ID 隔离；双空格清历史忽略输入/弹窗/修饰键。Vue/axios 三模板固定版本一起更新并跑浏览器测试。
- ScannerCore.log 走 journald arup，无 systemd 则 system.log（20MB轮转）；app.py print 走 scanner.log/scanner_error.log，不能只查 journalctl。ASR 文本可能命中关键词，诊断用完整日志标记。

## 验证和卫生
```sh
python -m py_compile app.py core_logic.py database.py
python -m unittest discover -s tests
git diff --check
```
- 对应 tests/test_*.py 必跑；真实字幕 remux 需要 ffmpeg/ffprobe，下载器/监控需要浏览器验证，skip 不算通过。测试样本/探针放 scratch；仅清本次测试产生的 .flask_secret/system.log，不碰原有运行文件。
- 不提交 env、secret、DB、日志、.supervisor、媒体、模型、缓存、IDE 文件，不输出 API key/代理凭据/TG token/RPC secret。保留既有未提交改动，不擅自提交/推送/部署。
- 可见版本按需求更新 app.py APP_VERSION（vYYYYMMDD）。未来改进建议见参考文档，不因有建议就实现。
