/* Scanner 内置下载器：直接通过 /api/aria2/jsonrpc 代理管理 Aria2，不依赖 AriaNg。 */
(function () {
    'use strict';

    var PREFS_KEY = 'scanner.downloaderPrefs';
    var DEFAULT_PREFS = {
        refreshInterval: 2,      // 秒
        defaultList: 'active',   // active | waiting | stopped
        sort: 'default',         // default | name | size | progress | speed | remain
        density: 'auto',         // auto（电脑宽松、手机紧凑）| comfortable | compact
        newTaskSplit: 0,         // 0 = 使用 Aria2 全局设置
        newTaskConnections: 0    // 单服务器连接数，0 = 使用 Aria2 全局设置
    };
    var MAX_CONN = 128;  // P3TERX Aria2-Pro-Core 允许超过官方 16 的连接数
    var PREFS_VERSION = 2;
    var MOBILE_QUERY = '(max-width: 767.98px)';
    var LISTS = [
        {id: 'active', label: '下载中', icon: 'bi-arrow-down-circle'},
        {id: 'waiting', label: '等待中', icon: 'bi-hourglass-split'},
        {id: 'stopped', label: '已结束', icon: 'bi-check2-circle'}
    ];
    var SORTS = [
        {id: 'default', label: '默认顺序'},
        {id: 'name', label: '文件名'},
        {id: 'size', label: '大小'},
        {id: 'progress', label: '进度'},
        {id: 'speed', label: '下载速度'},
        {id: 'remain', label: '剩余时间'}
    ];
    var STATUS_TEXT = {
        active: '下载中', waiting: '等待中', paused: '已暂停',
        complete: '已完成', error: '出错', removed: '已移除'
    };

    function sanitizePrefs(raw) {
        var p = Object.assign({}, DEFAULT_PREFS);
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return p;
        var interval = Number(raw.refreshInterval);
        if (Number.isFinite(interval)) p.refreshInterval = Math.min(60, Math.max(1, Math.round(interval)));
        if (LISTS.some(function (l) { return l.id === raw.defaultList; })) p.defaultList = raw.defaultList;
        if (SORTS.some(function (s) { return s.id === raw.sort; })) p.sort = raw.sort;
        if (['auto', 'compact', 'comfortable'].indexOf(raw.density) >= 0) p.density = raw.density;
        var split = Number(raw.newTaskSplit);
        if (Number.isFinite(split)) p.newTaskSplit = Math.min(128, Math.max(0, Math.round(split)));
        var conns = Number(raw.newTaskConnections);
        if (Number.isFinite(conns)) p.newTaskConnections = Math.min(128, Math.max(0, Math.round(conns)));
        return p;
    }

    function loadPrefs() {
        var raw;
        try { raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'); } catch (_) { raw = {}; }
        // 旧版本把「舒适」当默认值存了下来；升级时改为自动，让手机默认紧凑
        if (raw && typeof raw === 'object' && raw.v !== PREFS_VERSION && raw.density === 'comfortable') raw.density = 'auto';
        return sanitizePrefs(raw);
    }

    function formatBytes(value, suffix) {
        var size = Number(value);
        if (!Number.isFinite(size) || size < 0) return '-';
        var units = ['B', 'KB', 'MB', 'GB', 'TB'];
        var unit = 0;
        while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
        var digits = unit === 0 || size >= 100 ? 0 : (size >= 10 ? 1 : 2);
        return size.toFixed(digits) + ' ' + units[unit] + (suffix || '');
    }

    function formatDuration(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) return '-';
        seconds = Math.round(seconds);
        if (seconds >= 86400 * 30) return '> 30 天';
        var d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600);
        var m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
        if (d) return d + '天' + h + '时';
        if (h) return h + '时' + m + '分';
        if (m) return m + '分' + s + '秒';
        return s + '秒';
    }

    function baseName(path) {
        var parts = String(path || '').replace(/\\/g, '/').split('/');
        return parts[parts.length - 1] || '';
    }

    // 解析 "5M" / "500K" / "0" 一类 Aria2 限速值为字节/秒
    function parseLimit(value) {
        var m = String(value == null ? '' : value).trim().match(/^(\d+(?:\.\d+)?)\s*([KMG]?)/i);
        if (!m) return 0;
        return Math.round(Number(m[1]) * ({'': 1, K: 1024, M: 1048576, G: 1073741824}[m[2].toUpperCase()]));
    }

    function readFileAsBase64(file) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () {
                var text = String(reader.result || '');
                resolve(text.slice(text.indexOf(',') + 1));
            };
            reader.onerror = function () { reject(reader.error); };
            reader.readAsDataURL(file);
        });
    }

    var TEMPLATE = `
<div class="sdl" :class="'sdl-' + effectiveDensity">
  <div class="sdl-bar">
    <div class="sdl-lists" role="tablist">
      <button v-for="l in lists" :key="l.id" type="button" class="sdl-list-btn" :class="{active: list===l.id}" @click="switchList(l.id)" role="tab" :aria-selected="list===l.id">
        <i class="bi" :class="l.icon"></i><span class="sdl-list-label">{{ l.label }}</span><span class="sdl-count">{{ listCount(l.id) }}</span>
      </button>
    </div>
    <div class="sdl-speed d-none d-md-flex" title="全局速度">
      <span class="text-success"><i class="bi bi-arrow-down"></i>{{ fmtSpeed(stat.downloadSpeed) }}</span>
      <span class="text-primary"><i class="bi bi-arrow-up"></i>{{ fmtSpeed(stat.uploadSpeed) }}</span>
    </div>
  </div>

  <div class="sdl-toolbar">
    <button type="button" class="btn btn-primary btn-sm sdl-new" @click="openNew"><i class="bi bi-plus-lg"></i><span>新建</span></button>
    <div class="sdl-search">
      <i class="bi bi-search"></i>
      <input type="search" class="form-control form-control-sm" v-model.trim="search" placeholder="搜索文件名" aria-label="搜索下载任务">
    </div>
    <select class="form-select form-select-sm sdl-sort" v-model="prefs.sort" @change="savePrefs" aria-label="排序">
      <option v-for="s in sorts" :key="s.id" :value="s.id">{{ s.label }}</option>
    </select>
    <div class="dropdown">
      <button type="button" class="btn btn-light border btn-sm" data-bs-toggle="dropdown" aria-expanded="false" title="更多"><i class="bi bi-three-dots"></i></button>
      <ul class="dropdown-menu dropdown-menu-end shadow-sm">
        <li><button type="button" class="dropdown-item" @click="openSettings"><i class="bi bi-sliders me-2"></i>下载设置<span class="text-muted small ms-1" v-if="limitSummary">（{{ limitSummary }}）</span></button></li>
        <li><button type="button" class="dropdown-item" @click="openPrefs"><i class="bi bi-gear me-2"></i>偏好设置</button></li>
      </ul>
    </div>
  </div>

  <div class="sdl-selbar">
    <label class="sdl-check" v-if="visible.length"><input type="checkbox" class="form-check-input" :checked="allSelected" :indeterminate.prop="someSelected && !allSelected" @change="toggleAll($event.target.checked)"> <span>{{ selectedTasks.length ? '已选 ' + selectedTasks.length : '全选' }}</span></label>
    <div class="sdl-batch" v-if="selectedTasks.length">
      <button type="button" class="btn btn-sm btn-light border" v-if="canReorder" @click="batchTop" title="批量置顶"><i class="bi bi-chevron-bar-up"></i><span class="sdl-b-label"> 置顶</span></button>
      <button type="button" class="btn btn-sm btn-light border" v-if="batchCan('pause')" @click="batch('pause')" title="暂停"><i class="bi bi-pause-fill"></i><span class="sdl-b-label"> 暂停</span></button>
      <button type="button" class="btn btn-sm btn-light border" v-if="batchCan('resume')" @click="batch('resume')" title="开始"><i class="bi bi-play-fill"></i><span class="sdl-b-label"> 开始</span></button>
      <button type="button" class="btn btn-sm btn-light border" v-if="batchCan('retry')" @click="batch('retry')" title="重试"><i class="bi bi-arrow-repeat"></i><span class="sdl-b-label"> 重试</span></button>
      <button type="button" class="btn btn-sm btn-outline-danger" @click="batch('remove')" title="删除"><i class="bi bi-trash"></i><span class="sdl-b-label"> 删除</span></button>
    </div>
    <div class="sdl-quick" v-else>
      <template v-if="list !== 'stopped'">
      <button type="button" class="sdl-q sdl-q-start" title="全部开始" @click="globalAction('aria2.unpauseAll','已开始全部任务')"><i class="bi bi-play-fill"></i><span class="sdl-q-full">全部开始</span><span class="sdl-q-short">全开</span></button>
      <button type="button" class="sdl-q sdl-q-pause" title="全部暂停" @click="globalAction('aria2.pauseAll','已暂停全部任务')"><i class="bi bi-pause-fill"></i><span class="sdl-q-full">全部暂停</span><span class="sdl-q-short">全停</span></button>
      </template>
      <button type="button" class="sdl-q sdl-q-start" v-else title="重试全部失败任务" :disabled="retryingAll" @click="retryAllFailed"><i class="bi bi-arrow-repeat" :class="{'sdl-spin': retryingAll}"></i><span class="sdl-q-full">重试失败</span><span class="sdl-q-short">重试</span></button>
      <button type="button" class="sdl-q sdl-q-clean" title="清理已完成" @click="purge('complete')"><i class="bi bi-check2-all"></i><span class="sdl-q-full">清理已完成</span><span class="sdl-q-short">清完成</span></button>
      <button type="button" class="sdl-q sdl-q-purge" title="清理全部已结束" @click="purge('all')"><i class="bi bi-trash3"></i><span class="sdl-q-full">清理全部已结束</span><span class="sdl-q-short">清全部</span></button>
    </div>
  </div>

  <div class="sdl-error" v-if="error"><i class="bi bi-exclamation-triangle me-1"></i>{{ error }}</div>

  <div class="sdl-items">
    <div v-for="t in visible" :key="t.gid" class="sdl-item" :class="['is-' + t.status, {selected: selected[t.gid]}]" @click="openDetail(t)">
      <div class="sdl-fill" :class="barClass(t)" :style="{width: percent(t) + '%'}" role="progressbar" :aria-valuenow="Math.round(percent(t))" aria-valuemin="0" aria-valuemax="100"></div>
      <input type="checkbox" class="form-check-input sdl-item-check" :checked="!!selected[t.gid]" @click.stop @change="toggle(t.gid, $event.target.checked)" :aria-label="'选择 ' + taskName(t)">
      <div class="sdl-item-main">
        <div class="sdl-name" :title="taskName(t)">
          <i class="bi me-1" :class="t.bittorrent ? 'bi-magnet' : 'bi-file-earmark-arrow-down'"></i>{{ taskName(t) }}
        </div>
        <div class="sdl-meta">
          <span class="sdl-status" :class="'st-' + t.status">{{ statusText(t) }}</span>
          <span>{{ percent(t).toFixed(1) }}%</span>
          <span class="sdl-m-size" title="文件总大小">{{ Number(t.totalLength) > 0 ? fmtBytes(t.totalLength) : '大小未知' }}</span>
          <template v-if="t.status==='active'">
            <span class="text-success"><i class="bi bi-arrow-down"></i>{{ fmtSpeed(t.downloadSpeed) }}</span>
            <span class="d-none d-sm-inline">剩余 {{ remain(t) }}</span>
            <span class="d-none d-md-inline" title="连接数"><i class="bi bi-diagram-3"></i> {{ t.connections || 0 }}</span>
          </template>
          <span v-if="t.status==='error'" class="text-danger sdl-errmsg" :title="t.errorMessage">{{ t.errorMessage || ('错误代码 ' + t.errorCode) }}</span>
        </div>
      </div>
      <div class="sdl-actions" @click.stop>
        <button type="button" class="btn btn-sm btn-light" v-if="canReorder" @click="move(t)" title="置顶"><i class="bi bi-chevron-bar-up text-primary"></i></button>
        <button type="button" class="btn btn-sm btn-light" v-if="t.status==='active' || t.status==='waiting'" @click="act(t,'pause')" title="暂停"><i class="bi bi-pause-fill text-warning"></i></button>
        <button type="button" class="btn btn-sm btn-light" v-if="t.status==='paused'" @click="act(t,'resume')" title="开始"><i class="bi bi-play-fill text-success"></i></button>
        <button type="button" class="btn btn-sm btn-light" v-if="canRetry(t)" @click="act(t,'retry')" title="重试"><i class="bi bi-arrow-repeat"></i></button>
        <button type="button" class="btn btn-sm btn-light" @click="act(t,'remove')" title="删除"><i class="bi bi-trash text-danger"></i></button>
      </div>
    </div>
    <div v-if="!visible.length" class="sdl-empty">
      <i class="bi" :class="loading ? 'bi-arrow-repeat sdl-spin' : (search ? 'bi-search' : 'bi-inbox')"></i>
      <div>{{ loading ? '加载中…' : (search ? '没有匹配“' + search + '”的任务' : '暂无' + currentListLabel + '的任务') }}</div>
      <button v-if="!loading && !search && list!=='stopped'" type="button" class="btn btn-sm btn-primary mt-2" @click="openNew"><i class="bi bi-plus-lg me-1"></i>新建下载</button>
    </div>
  </div>
  <div class="sdl-foot">
    <span class="sdl-dot" :class="error ? 'is-off' : 'is-on'"></span>
    <span class="sdl-version">{{ aria2Version ? 'Aria2 v' + aria2Version : (error ? 'Aria2 未连接' : 'Aria2') }}</span>
    <a v-if="standaloneUrl" :href="standaloneUrl" target="_blank" rel="noopener" class="sdl-open" title="在新标签页单独打开下载器"><i class="bi bi-box-arrow-up-right"></i><span>单独打开</span></a>
  </div>

  <!-- 新建任务 -->
  <div class="modal fade" ref="newModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered modal-fullscreen-sm-down"><div class="modal-content">
      <div class="modal-header"><h5 class="modal-title fs-6"><i class="bi bi-plus-circle me-2"></i>新建下载</h5><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button></div>
      <div class="modal-body">
        <ul class="nav nav-pills nav-fill mb-3 sdl-pills">
          <li class="nav-item"><button type="button" class="nav-link" :class="{active: form.mode==='uri'}" @click="form.mode='uri'"><i class="bi bi-link-45deg me-1"></i>链接 / 磁力</button></li>
          <li class="nav-item"><button type="button" class="nav-link" :class="{active: form.mode==='file'}" @click="form.mode='file'"><i class="bi bi-file-earmark-arrow-up me-1"></i>种子文件</button></li>
        </ul>
        <div v-if="form.mode==='uri'">
          <textarea class="form-control font-monospace sdl-uris" v-model="form.uris" rows="5" placeholder="每行一个链接，支持 HTTP / FTP / 磁力链接 magnet:?"></textarea>
          <div class="form-text">{{ uriLines.length ? '共 ' + uriLines.length + ' 个任务' : '每行会创建一个下载任务' }}</div>
        </div>
        <div v-else>
          <input type="file" class="form-control" ref="torrentInput" accept=".torrent,.metalink,.meta4,application/x-bittorrent" multiple @change="pickFiles">
          <div class="form-text">支持 .torrent 与 .metalink，可多选。</div>
          <ul class="list-unstyled small mt-2 mb-0" v-if="form.files.length"><li v-for="f in form.files" :key="f.name" class="text-truncate"><i class="bi bi-file-earmark me-1"></i>{{ f.name }} <span class="text-muted">{{ fmtBytes(f.size) }}</span></li></ul>
        </div>
        <details class="mt-3 sdl-adv">
          <summary class="small fw-bold">高级选项</summary>
          <div class="row g-2 mt-1">
            <div class="col-12"><label class="form-label small mb-1">下载目录</label><input class="form-control form-control-sm" v-model.trim="form.dir" :placeholder="'留空使用 Aria2 默认目录' + (globalDir ? '（' + globalDir + '）' : '')"><div class="form-text" v-if="defaultDir">默认是 Scanner 设置中的下载根目录，可临时修改。</div></div>
            <div class="col-6"><label class="form-label small mb-1">分片数</label><input type="number" min="0" max="128" class="form-control form-control-sm" v-model.number="form.split" placeholder="0 = 默认"></div>
            <div class="col-6"><label class="form-label small mb-1">单服务器连接数</label><input type="number" min="0" max="128" class="form-control form-control-sm" v-model.number="form.connections" placeholder="0 = 默认"></div>
            <div class="col-12" v-if="form.mode==='uri'"><label class="form-label small mb-1">文件名</label><input class="form-control form-control-sm" v-model.trim="form.out" :disabled="uriLines.length > 1" placeholder="仅单个链接时可填"></div>
            <div class="col-12"><label class="form-label small mb-1">User-Agent</label><input class="form-control form-control-sm font-monospace" v-model.trim="form.userAgent" :placeholder="globalUserAgent ? '留空使用全局：' + globalUserAgent : '留空使用全局设置'"></div>
            <div class="col-12"><label class="form-label small mb-1">Referer</label><input class="form-control form-control-sm font-monospace" v-model.trim="form.referer" placeholder="例如 https://example.com/page"></div>
            <div class="col-12"><label class="form-label small mb-1">Cookie</label><input class="form-control form-control-sm font-monospace" v-model.trim="form.cookie" placeholder="例如 name=value; token=abc"></div>
            <div class="col-12"><label class="form-label small mb-1">自定义请求头</label><textarea class="form-control form-control-sm font-monospace sdl-headers" rows="2" v-model="form.headers" placeholder="每行一个，例如 Authorization: Bearer xxx"></textarea></div>
          </div>
        </details>
      </div>
      <div class="modal-footer"><button type="button" class="btn btn-light" data-bs-dismiss="modal">取消</button><button type="button" class="btn btn-primary" @click="submitNew" :disabled="submitting || !canSubmit"><span v-if="submitting" class="spinner-border spinner-border-sm me-1"></span>开始下载</button></div>
    </div></div>
  </div>

  <!-- 任务详情 -->
  <div class="modal fade" ref="detailModal" tabindex="-1">
    <div class="modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable modal-fullscreen-sm-down"><div class="modal-content" v-if="detail">
      <div class="modal-header"><h5 class="modal-title fs-6 text-truncate pe-1 sdl-selectable" :title="taskName(detail)">{{ taskName(detail) }}</h5><button type="button" class="sdl-copy me-auto" title="复制名称" @click="copy(taskName(detail), '已复制名称')"><i class="bi bi-clipboard"></i></button><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button></div>
      <div class="modal-body">
        <div class="progress mb-2" style="height:10px"><div class="progress-bar" :class="barClass(detail)" :style="{width: percent(detail) + '%'}"></div></div>
        <div class="sdl-kv">
          <div><span>状态</span><b :class="'st-' + detail.status">{{ statusText(detail) }}</b></div>
          <div><span>进度</span><b>{{ percent(detail).toFixed(2) }}%</b></div>
          <div><span>大小</span><b>{{ fmtBytes(detail.completedLength) }} / {{ fmtBytes(detail.totalLength) }}</b></div>
          <div><span>下载速度</span><b>{{ fmtSpeed(detail.downloadSpeed) }}</b></div>
          <div><span>上传速度</span><b>{{ fmtSpeed(detail.uploadSpeed) }}</b></div>
          <div><span>剩余时间</span><b>{{ detail.status==='active' ? remain(detail) : '-' }}</b></div>
          <div><span>连接数</span><b>{{ detail.connections || 0 }}<template v-if="detail.bittorrent"> / 做种者 {{ detail.numSeeders || 0 }}</template></b></div>
          <div class="sdl-kv-wide"><span>保存目录</span><b class="font-monospace">{{ detail.dir || '-' }}</b></div>
          <div class="sdl-kv-wide" v-if="detail.infoHash"><span>InfoHash</span><b class="font-monospace">{{ detail.infoHash }}</b></div>
          <div class="sdl-kv-wide"><span>GID</span><b class="font-monospace">{{ detail.gid }}</b></div>
          <div class="sdl-kv-wide text-danger" v-if="detail.status==='error'"><span>错误</span><b>{{ detail.errorMessage || ('错误代码 ' + detail.errorCode) }}</b></div>
        </div>
        <h6 class="small fw-bold mt-3 mb-2">文件（{{ (detail.files || []).length }}）</h6>
        <div class="sdl-files">
          <div v-for="f in (detail.files || [])" :key="f.index" class="sdl-file" :class="{'text-muted': f.selected==='false'}">
            <div class="d-flex align-items-center gap-2"><span class="text-truncate sdl-selectable me-auto" :title="f.path">{{ baseName(f.path) || '(未知)' }}</span><span class="text-nowrap small">{{ fmtBytes(f.length) }}</span><button type="button" class="sdl-copy" v-if="f.path" title="复制文件名" @click="copy(baseName(f.path), '已复制文件名')"><i class="bi bi-clipboard"></i></button></div>
            <div class="progress" style="height:4px"><div class="progress-bar" :style="{width: filePercent(f) + '%'}"></div></div>
          </div>
        </div>
        <template v-if="detailUris.length">
          <div class="d-flex align-items-center mt-3 mb-2">
            <h6 class="small fw-bold mb-0 me-auto">下载链接（{{ allDetailUris.length }}）</h6>
            <button type="button" class="btn btn-sm btn-link p-0 text-decoration-none small" v-if="allDetailUris.length > 1" @click="copyAllUris"><i class="bi bi-clipboard me-1"></i>复制全部</button>
          </div>
          <div class="sdl-uri" v-for="u in detailUris" :key="u"><span class="font-monospace small sdl-selectable">{{ u }}</span><button type="button" class="sdl-copy" title="复制链接" @click="copy(u, '已复制链接')"><i class="bi bi-clipboard"></i></button></div>
          <div class="form-text" v-if="allDetailUris.length > detailUris.length">仅显示前 {{ detailUris.length }} 个，「复制全部」包含全部链接。</div>
        </template>
        <div class="sdl-queue mt-3" v-if="detailInQueue">
          <span class="small fw-bold me-auto">队列位置</span>
          <button type="button" class="btn btn-sm btn-light border" @click="move(detail)" title="置顶"><i class="bi bi-chevron-bar-up"></i> 置顶</button>
        </div>
        <details class="mt-3 sdl-adv sdl-taskopt" v-if="detailEditable" @toggle="$event.target.open && loadTaskOptions()">
          <summary class="small fw-bold">任务设置</summary>
          <div class="row g-2 mt-1">
            <div class="col-6"><label class="form-label small mb-1">下载限速</label><div class="input-group input-group-sm"><input type="number" min="0" step="0.1" class="form-control" v-model.number="taskOpt.down"><span class="input-group-text">MB/s</span></div></div>
            <div class="col-6"><label class="form-label small mb-1">上传限速</label><div class="input-group input-group-sm"><input type="number" min="0" step="0.1" class="form-control" v-model.number="taskOpt.up"><span class="input-group-text">MB/s</span></div></div>
            <div class="col-6"><label class="form-label small mb-1">单服务器连接数</label><input type="number" min="1" max="128" class="form-control form-control-sm" v-model.number="taskOpt.connections"></div>
            <div class="col-6"><label class="form-label small mb-1">分片数</label><input type="number" min="1" max="128" class="form-control form-control-sm" v-model.number="taskOpt.split"></div>
          </div>
          <div class="d-flex align-items-center gap-2 mt-2">
            <div class="form-text m-0 me-auto">限速填 0 表示不限，立即生效。改连接数或分片数时，下载中的任务会由 Aria2 自动重新开始，已下载部分保留。</div>
            <button type="button" class="btn btn-sm btn-primary text-nowrap" :disabled="!taskOptLoaded || savingTaskOpt" @click="saveTaskOptions">应用</button>
          </div>
        </details>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-sm btn-light border me-auto" @click="copy(detail.gid)"><i class="bi bi-clipboard me-1"></i>复制 GID</button>
        <button type="button" class="btn btn-sm btn-light border" v-if="detail.status==='active' || detail.status==='waiting'" @click="act(detail,'pause')"><i class="bi bi-pause-fill"></i> 暂停</button>
        <button type="button" class="btn btn-sm btn-light border" v-if="detail.status==='paused'" @click="act(detail,'resume')"><i class="bi bi-play-fill"></i> 开始</button>
        <button type="button" class="btn btn-sm btn-light border" v-if="canRetry(detail)" @click="act(detail,'retry')"><i class="bi bi-arrow-repeat"></i> 重试</button>
        <button type="button" class="btn btn-sm btn-outline-danger" @click="act(detail,'remove')"><i class="bi bi-trash"></i> 删除</button>
      </div>
    </div></div>
  </div>

  <!-- 下载设置（写入 aria2.conf） -->
  <div class="modal fade" ref="settingsModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered modal-fullscreen-sm-down"><div class="modal-content">
      <div class="modal-header"><h5 class="modal-title fs-6"><i class="bi bi-sliders me-2"></i>下载设置</h5><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button></div>
      <div class="modal-body">
        <div class="row g-3">
          <div class="col-12"><label class="form-label small fw-bold mb-1">最大同时下载数</label><input type="number" min="1" max="999" class="form-control form-control-sm" v-model.number="settingsForm.concurrent"><div class="form-text">超出的任务在「等待中」排队。</div></div>
          <div class="col-6"><label class="form-label small fw-bold mb-1">下载限速</label><div class="input-group input-group-sm"><input type="number" min="0" step="0.1" class="form-control" v-model.number="settingsForm.down"><span class="input-group-text">MB/s</span></div></div>
          <div class="col-6"><label class="form-label small fw-bold mb-1">上传限速</label><div class="input-group input-group-sm"><input type="number" min="0" step="0.1" class="form-control" v-model.number="settingsForm.up"><span class="input-group-text">MB/s</span></div></div>
          <div class="col-12"><label class="form-label small fw-bold mb-1">User-Agent</label><textarea class="form-control form-control-sm font-monospace" rows="2" v-model.trim="settingsForm.userAgent" placeholder="留空则保持当前值"></textarea><div class="form-text">只影响之后开始的任务；新建任务时可单独覆盖。</div></div>
        </div>
        <div class="form-text mt-3">限速填 0 表示不限。保存后立即生效，并写入 aria2.conf，Aria2 重启后仍保留。</div>
        <div class="form-text" v-if="aria2Version">当前 Aria2 版本：v{{ aria2Version }}</div>
      </div>
      <div class="modal-footer"><button type="button" class="btn btn-light btn-sm" data-bs-dismiss="modal">取消</button><button type="button" class="btn btn-primary btn-sm" @click="saveSettings" :disabled="savingSettings"><span v-if="savingSettings" class="spinner-border spinner-border-sm me-1"></span>保存</button></div>
    </div></div>
  </div>

  <!-- 偏好设置 -->
  <div class="modal fade" ref="prefsModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered modal-fullscreen-sm-down"><div class="modal-content">
      <div class="modal-header"><h5 class="modal-title fs-6"><i class="bi bi-gear me-2"></i>下载器偏好设置</h5><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button></div>
      <div class="modal-body">
        <div class="row g-2">
          <div class="col-6"><label class="form-label small fw-bold mb-1">刷新间隔</label><div class="input-group input-group-sm"><input type="number" min="1" max="60" class="form-control" v-model.number="prefsForm.refreshInterval"><span class="input-group-text">秒</span></div></div>
          <div class="col-6"><label class="form-label small fw-bold mb-1">默认打开</label><select class="form-select form-select-sm" v-model="prefsForm.defaultList"><option v-for="l in lists" :key="l.id" :value="l.id">{{ l.label }}</option></select></div>
          <div class="col-6"><label class="form-label small fw-bold mb-1">排序方式</label><select class="form-select form-select-sm" v-model="prefsForm.sort"><option v-for="s in sorts" :key="s.id" :value="s.id">{{ s.label }}</option></select></div>
          <div class="col-6"><label class="form-label small fw-bold mb-1">列表密度</label><select class="form-select form-select-sm" v-model="prefsForm.density"><option value="auto">自动（电脑宽松，手机紧凑）</option><option value="comfortable">宽松</option><option value="compact">紧凑</option></select></div>
          <div class="col-6"><label class="form-label small fw-bold mb-1">新建任务默认分片数</label><input type="number" min="0" max="128" class="form-control form-control-sm" v-model.number="prefsForm.newTaskSplit"><div class="form-text">0 = 使用 Aria2 设置</div></div>
          <div class="col-6"><label class="form-label small fw-bold mb-1">新建任务默认连接数</label><input type="number" min="0" max="128" class="form-control form-control-sm" v-model.number="prefsForm.newTaskConnections"><div class="form-text">单服务器，0 = 使用 Aria2 设置</div></div>
        </div>
        <hr>
        <div class="d-flex flex-wrap gap-2 align-items-center mb-2">
          <span class="small fw-bold me-auto">导入 / 导出</span>
          <button type="button" class="btn btn-sm btn-light border" @click="exportPrefs"><i class="bi bi-clipboard me-1"></i>复制到剪贴板</button>
          <button type="button" class="btn btn-sm btn-outline-primary" :disabled="!prefsImport.trim()" @click="importPrefs"><i class="bi bi-box-arrow-in-down me-1"></i>导入</button>
        </div>
        <textarea class="form-control form-control-sm font-monospace" rows="3" v-model="prefsImport" placeholder='粘贴导出的偏好 JSON，例如 {"density":"compact"}'></textarea>
        <div class="form-text">偏好只保存在当前浏览器；新旧界面切换在「设置 → 界面」，所有设备共用。</div>
      </div>
      <div class="modal-footer"><button type="button" class="btn btn-light btn-sm me-auto" @click="resetPrefs">恢复默认</button><button type="button" class="btn btn-light btn-sm" data-bs-dismiss="modal">取消</button><button type="button" class="btn btn-primary btn-sm" @click="applyPrefsForm">保存</button></div>
    </div></div>
  </div>
</div>`;

    window.ScannerDownloader = {
        name: 'ScannerDownloader',
        props: {
            active: {type: Boolean, default: true},
            notify: {type: Function, default: function () {}},
            defaultDir: {type: String, default: ''},
            standaloneUrl: {type: String, default: ''},
            confirm: {type: Function, default: function (msg, fn) { if (window.confirm(msg)) fn(); }}
        },
        template: TEMPLATE,
        data: function () {
            var prefs = loadPrefs();
            return {
                lists: LISTS, sorts: SORTS, prefs: prefs, list: prefs.defaultList,
                tasks: [], stat: {numActive: 0, numWaiting: 0, numStopped: 0, downloadSpeed: 0, uploadSpeed: 0},
                search: '', selected: {}, loading: true, error: '', timer: null, seq: 0,
                detail: null, globalDir: '', globalUserAgent: '', globalConcurrent: 5, limits: {down: 0, up: 0},
                form: {mode: 'uri', uris: '', files: [], dir: '', split: 0, connections: 0, out: '', userAgent: '', referer: '', cookie: '', headers: ''}, submitting: false,
                taskOpt: {}, taskOptOrig: {}, taskOptLoaded: false, savingTaskOpt: false, aria2Version: '', retryingAll: false,
                isMobile: !!(window.matchMedia && window.matchMedia(MOBILE_QUERY).matches),
                settingsForm: {concurrent: 5, down: 0, up: 0, userAgent: ''}, savingSettings: false,
                prefsForm: Object.assign({}, prefs), prefsImport: '',
                modals: {}
            };
        },
        computed: {
            currentListLabel: function () { var self = this; return (LISTS.find(function (l) { return l.id === self.list; }) || {}).label || ''; },
            visible: function () {
                var q = this.search.toLowerCase(), self = this;
                var rows = this.tasks.filter(function (t) { return !q || self.taskName(t).toLowerCase().indexOf(q) >= 0; });
                var key = this.prefs.sort;
                if (key === 'default') return rows;
                var val = {
                    name: function (t) { return self.taskName(t).toLowerCase(); },
                    size: function (t) { return -Number(t.totalLength || 0); },
                    progress: function (t) { return -self.percent(t); },
                    speed: function (t) { return -Number(t.downloadSpeed || 0); },
                    remain: function (t) { var r = self.remainSeconds(t); return Number.isFinite(r) ? r : Infinity; }
                }[key];
                return rows.slice().sort(function (a, b) {
                    var x = val(a), y = val(b);
                    return x < y ? -1 : (x > y ? 1 : 0);
                });
            },
            selectedTasks: function () { var s = this.selected; return this.visible.filter(function (t) { return s[t.gid]; }); },
            allSelected: function () { return this.visible.length > 0 && this.selectedTasks.length === this.visible.length; },
            someSelected: function () { return this.selectedTasks.length > 0; },
            uriLines: function () { return this.form.uris.split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean); },
            canSubmit: function () { return this.form.mode === 'uri' ? this.uriLines.length > 0 : this.form.files.length > 0; },
            allDetailUris: function () {
                var out = [];
                ((this.detail && this.detail.files) || []).forEach(function (f) {
                    (f.uris || []).forEach(function (u) { if (u.uri && out.indexOf(u.uri) < 0) out.push(u.uri); });
                });
                return out;
            },
            detailUris: function () { return this.allDetailUris.slice(0, 20); },
            detailEditable: function () { return !!this.detail && ['active', 'waiting', 'paused'].indexOf(this.detail.status) >= 0; },
            detailInQueue: function () { return !!this.detail && (this.detail.status === 'waiting' || this.detail.status === 'paused'); },
            // 队列顺序只在等待列表、默认排序且未搜索时有意义
            effectiveDensity: function () {
                if (this.prefs.density !== 'auto') return this.prefs.density;
                return this.isMobile ? 'compact' : 'comfortable';
            },
            canReorder: function () { return this.list === 'waiting' && this.prefs.sort === 'default' && !this.search; },
            headerLines: function () {
                var lines = this.form.headers.split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
                var cookie = this.form.cookie.replace(/^cookie\s*:\s*/i, '').trim();
                if (cookie) lines.push('Cookie: ' + cookie);
                return lines;
            },
            limitSummary: function () {
                var d = this.limits.down, u = this.limits.up;
                if (!d && !u) return '';
                return '↓' + (d ? formatBytes(d, '/s') : '不限') + ' ↑' + (u ? formatBytes(u, '/s') : '不限');
            }
        },
        watch: {
            active: function (on) { if (on) this.refresh(); this.schedule(); }
        },
        mounted: function () {
            var self = this;
            ['newModal', 'detailModal', 'settingsModal', 'prefsModal'].forEach(function (name) {
                // 挂到 body，避免被标签页容器的 overflow/层级截断
                document.body.appendChild(self.$refs[name]);
                self.modals[name] = new bootstrap.Modal(self.$refs[name]);
            });
            this.$refs.detailModal.addEventListener('hidden.bs.modal', function () { self.detail = null; });
            if (window.matchMedia) {
                this.mobileMql = window.matchMedia(MOBILE_QUERY);
                this.onMobileChange = function (e) { self.isMobile = e.matches; };
                if (this.mobileMql.addEventListener) this.mobileMql.addEventListener('change', this.onMobileChange);
                else this.mobileMql.addListener(this.onMobileChange);
            }
            this.refresh();
            this.loadGlobalOptions();
            this.loadVersion();
            this.schedule();
        },
        beforeUnmount: function () {
            clearTimeout(this.timer);
            if (this.mobileMql) {
                if (this.mobileMql.removeEventListener) this.mobileMql.removeEventListener('change', this.onMobileChange);
                else this.mobileMql.removeListener(this.onMobileChange);
            }
            var self = this;
            Object.keys(this.modals).forEach(function (name) { self.modals[name].dispose(); self.$refs[name] && self.$refs[name].remove(); });
        },
        methods: {
            fmtBytes: function (v) { return formatBytes(v); },
            fmtSpeed: function (v) { return formatBytes(v, '/s'); },
            baseName: baseName,
            rpc: function (method, params) {
                return axios.post('/api/aria2/jsonrpc', {jsonrpc: '2.0', id: 'sdl-' + (++this.seq), method: method, params: params || []})
                    .then(function (r) {
                        if (r.data && r.data.error) throw new Error(r.data.error.message || 'Aria2 返回错误');
                        return r.data ? r.data.result : null;
                    });
            },
            listCount: function (id) {
                return Number({active: this.stat.numActive, waiting: this.stat.numWaiting, stopped: this.stat.numStopped}[id] || 0);
            },
            schedule: function () {
                clearTimeout(this.timer);
                if (!this.active) return;
                var self = this;
                this.timer = setTimeout(function () { self.refresh().finally(function () { self.schedule(); }); }, this.prefs.refreshInterval * 1000);
            },
            refresh: function () {
                var self = this, list = this.list;
                var jobs = [axios.post('/api/aria2/task_list', {list: list}).then(function (r) { return r.data; })];
                if (this.detail) jobs.push(this.rpc('aria2.tellStatus', [this.detail.gid]).catch(function () { return null; }));
                return Promise.all(jobs).then(function (res) {
                    var data = res[0] || {};
                    if (data.stat) self.stat = data.stat;
                    if (list === self.list && Array.isArray(data.tasks)) {
                        self.tasks = data.tasks;
                        var gids = {};
                        self.tasks.forEach(function (t) { gids[t.gid] = true; });
                        Object.keys(self.selected).forEach(function (g) { if (!gids[g]) delete self.selected[g]; });
                    }
                    if (self.detail && res[1] && res[1].gid === self.detail.gid) self.detail = res[1];
                    self.error = '';
                }).catch(function (e) {
                    self.error = (e.response && e.response.data && e.response.data.msg) || e.message || '无法连接 Aria2';
                }).finally(function () { self.loading = false; });
            },
            switchList: function (id) {
                if (this.list === id) return;
                this.list = id; this.tasks = []; this.selected = {}; this.loading = true;
                this.refresh();
            },
            loadGlobalOptions: function () {
                var self = this;
                return this.rpc('aria2.getGlobalOption').then(function (o) {
                    o = o || {};
                    self.globalDir = o.dir || '';
                    self.globalUserAgent = o['user-agent'] || '';
                    self.globalConcurrent = Number(o['max-concurrent-downloads'] || 5);
                    self.limits = {down: parseLimit(o['max-overall-download-limit']), up: parseLimit(o['max-overall-upload-limit'])};
                }).catch(function () {});
            },
            taskName: function (t) {
                if (!t) return '';
                if (t.bittorrent && t.bittorrent.info && t.bittorrent.info.name) return t.bittorrent.info.name;
                var files = t.files || [];
                if (files.length && files[0].path) return baseName(files[0].path);
                if (files.length && files[0].uris && files[0].uris.length) return baseName(files[0].uris[0].uri.split('?')[0]) || files[0].uris[0].uri;
                if (t.infoHash) return '[元数据] ' + t.infoHash;
                return t.gid;
            },
            percent: function (t) {
                var total = Number(t.totalLength || 0);
                return total > 0 ? Math.min(100, Number(t.completedLength || 0) / total * 100) : (t.status === 'complete' ? 100 : 0);
            },
            filePercent: function (f) {
                var total = Number(f.length || 0);
                return total > 0 ? Math.min(100, Number(f.completedLength || 0) / total * 100) : 0;
            },
            remainSeconds: function (t) {
                var speed = Number(t.downloadSpeed || 0);
                if (speed <= 0) return NaN;
                return (Number(t.totalLength || 0) - Number(t.completedLength || 0)) / speed;
            },
            remain: function (t) { return formatDuration(this.remainSeconds(t)); },
            statusText: function (t) {
                if (t.status === 'complete' && t.followedBy && t.followedBy.length) return '元数据已获取';
                return STATUS_TEXT[t.status] || t.status;
            },
            barClass: function (t) {
                return {complete: 'bg-success', error: 'bg-danger', removed: 'bg-secondary', paused: 'bg-warning', waiting: 'bg-secondary'}[t.status] || 'bg-primary';
            },
            canRetry: function (t) {
                // 与 AriaNg 一致：仅出错的非 BT 任务可用原链接重新创建
                return t.status === 'error' && !t.bittorrent && (t.files || []).some(function (f) { return (f.uris || []).length; });
            },
            toggle: function (gid, on) { if (on) this.selected[gid] = true; else delete this.selected[gid]; },
            toggleAll: function (on) {
                var self = this;
                this.visible.forEach(function (t) { self.toggle(t.gid, on); });
            },
            batchCan: function (action) {
                var self = this;
                return this.selectedTasks.some(function (t) {
                    if (action === 'pause') return t.status === 'active' || t.status === 'waiting';
                    if (action === 'resume') return t.status === 'paused';
                    if (action === 'retry') return self.canRetry(t);
                    return true;
                });
            },
            retryTask: function (t) {
                var self = this;
                // 列表行只带首个 URI，重试前取完整任务以保留全部镜像
                return Promise.all([
                    this.rpc('aria2.tellStatus', [t.gid, ['files']]).catch(function () { return t; }),
                    this.rpc('aria2.getOption', [t.gid]).catch(function () { return {}; })
                ]).then(function (res) {
                    var uris = [];
                    ((res[0] && res[0].files) || []).forEach(function (f) { (f.uris || []).forEach(function (u) { if (uris.indexOf(u.uri) < 0) uris.push(u.uri); }); });
                    if (!uris.length) throw new Error('任务没有可用的下载链接');
                    return self.rpc('aria2.addUri', [uris, res[1] || {}]);
                }).then(function () {
                    return self.rpc('aria2.removeDownloadResult', [t.gid]).catch(function () {});
                });
            },
            removeTask: function (t) {
                var self = this;
                if (['active', 'waiting', 'paused'].indexOf(t.status) >= 0) {
                    return this.rpc('aria2.forceRemove', [t.gid]).then(function () {
                        return self.rpc('aria2.removeDownloadResult', [t.gid]).catch(function () {});
                    });
                }
                return this.rpc('aria2.removeDownloadResult', [t.gid]);
            },
            runOne: function (t, action) {
                if (action === 'pause') return this.rpc('aria2.forcePause', [t.gid]);
                if (action === 'resume') return this.rpc('aria2.unpause', [t.gid]);
                if (action === 'retry') return this.retryTask(t);
                return this.removeTask(t);
            },
            act: function (t, action) {
                var self = this;
                var go = function () {
                    self.runOne(t, action).then(function () {
                        self.notify({pause: '已暂停', resume: '已开始', retry: '已重新创建任务', remove: '已删除任务'}[action]);
                        if (action === 'remove' && self.detail && self.detail.gid === t.gid) self.modals.detailModal.hide();
                        delete self.selected[t.gid];
                        self.refresh();
                    }).catch(function (e) { self.notify('操作失败：' + e.message, 'error'); });
                };
                if (action === 'remove') this.confirm('确定删除任务「' + this.taskName(t) + '」吗？\n只删除下载记录，已下载的文件保留。', go);
                else go();
            },
            batch: function (action) {
                var self = this;
                var targets = this.selectedTasks.filter(function (t) {
                    if (action === 'pause') return t.status === 'active' || t.status === 'waiting';
                    if (action === 'resume') return t.status === 'paused';
                    if (action === 'retry') return self.canRetry(t);
                    return true;
                });
                if (!targets.length) return;
                var go = function () {
                    Promise.allSettled(targets.map(function (t) { return self.runOne(t, action); })).then(function (results) {
                        var failed = results.filter(function (r) { return r.status === 'rejected'; }).length;
                        var label = {pause: '暂停', resume: '开始', retry: '重试', remove: '删除'}[action];
                        if (failed) self.notify(label + '完成，' + failed + ' 项失败', 'error');
                        else self.notify('已' + label + ' ' + targets.length + ' 项');
                        self.selected = {};
                        self.refresh();
                    });
                };
                if (action === 'remove') this.confirm('确定删除选中的 ' + targets.length + ' 个任务吗？\n只删除下载记录，已下载的文件保留。', go);
                else go();
            },
            globalAction: function (method, msg) {
                var self = this;
                this.rpc(method).then(function () { self.notify(msg); self.refresh(); })
                    .catch(function (e) { self.notify('操作失败：' + e.message, 'error'); });
            },
            purge: function (kind) {
                var self = this;
                var msg = kind === 'complete'
                    ? '确定清理所有「已完成」的下载记录吗？\n出错和已移除的记录保留，已下载的文件保留。'
                    : '确定清理全部已结束记录（已完成、出错、已移除）吗？\n已下载的文件保留。';
                this.confirm(msg, function () {
                    var job = kind === 'complete'
                        ? axios.post('/api/aria2/task_list', {list: 'stopped'}).then(function (r) {
                            var gids = ((r.data && r.data.tasks) || []).filter(function (t) { return t.status === 'complete'; }).map(function (t) { return t.gid; });
                            return Promise.allSettled(gids.map(function (g) { return self.rpc('aria2.removeDownloadResult', [g]); }))
                                .then(function (res) { return res.filter(function (x) { return x.status === 'fulfilled'; }).length; });
                        })
                        : self.rpc('aria2.purgeDownloadResult').then(function () { return null; });
                    job.then(function (n) {
                        self.notify(n == null ? '已清理全部已结束记录' : '已清理 ' + n + ' 条已完成记录');
                        self.refresh();
                    }).catch(function (e) { self.notify('清理失败：' + e.message, 'error'); });
                });
            },
            // 批量置顶：从队列靠后的开始逐个移到最前，选中任务之间的相对顺序保持不变
            batchTop: function () {
                var self = this;
                var sel = this.visible.map(function (t) { return t.gid; }).filter(function (g) { return self.selected[g]; });
                if (!sel.length) return;
                sel.slice().reverse().reduce(function (chain, g) {
                    return chain.then(function () { return self.rpc('aria2.changePosition', [g, 0, 'POS_SET']); });
                }, Promise.resolve()).then(function () {
                    self.notify('已置顶 ' + sel.length + ' 项');
                    self.selected = {};
                }).catch(function (e) { self.notify('置顶失败：' + e.message, 'error'); })
                    .finally(function () { self.refresh(); });
            },
            // 已结束列表：自动选出所有可重试的失败任务，逐个用原链接重建，成功后移除原失败记录
            retryAllFailed: function () {
                var self = this;
                this.retryingAll = true;
                axios.post('/api/aria2/task_list', {list: 'stopped'}).then(function (r) {
                    var failed = ((r.data && r.data.tasks) || []).filter(function (t) { return t.status === 'error'; });
                    var targets = failed.filter(function (t) { return self.canRetry(t); });
                    var skipped = failed.length - targets.length;
                    if (!targets.length) {
                        self.notify(failed.length ? '有 ' + failed.length + ' 个失败任务，但都是 BT 任务或没有下载链接，无法重试' : '没有失败的任务');
                        return;
                    }
                    var ok = 0, bad = 0;
                    return targets.reduce(function (chain, t) {
                        return chain.then(function () {
                            return self.retryTask(t).then(function () { ok++; }, function () { bad++; });
                        });
                    }, Promise.resolve()).then(function () {
                        var msg = '已重试 ' + ok + ' 个失败任务';
                        if (bad) msg += '，' + bad + ' 个失败';
                        if (skipped) msg += '，' + skipped + ' 个无法重试（BT 或无链接）';
                        self.notify(msg, bad ? 'error' : undefined);
                    });
                }).catch(function (e) { self.notify('重试失败：' + e.message, 'error'); })
                    .finally(function () { self.retryingAll = false; self.selected = {}; self.refresh(); });
            },
            copyAllUris: function () { this.copy(this.allDetailUris.join(String.fromCharCode(10)), '已复制 ' + this.allDetailUris.length + ' 个链接'); },
            loadVersion: function () {
                var self = this;
                return this.rpc('aria2.getVersion').then(function (v) { self.aria2Version = (v && v.version) || ''; }).catch(function () {});
            },
            move: function (t) {
                var self = this;
                return this.rpc('aria2.changePosition', [t.gid, 0, 'POS_SET']).then(function () {
                    self.notify('已置顶');
                    self.refresh();
                }).catch(function (e) { self.notify('置顶失败：' + e.message, 'error'); });
            },
            loadTaskOptions: function () {
                var self = this, gid = this.detail && this.detail.gid;
                if (!gid) return Promise.resolve();
                this.taskOptLoaded = false;
                return this.rpc('aria2.getOption', [gid]).then(function (o) {
                    if (!self.detail || self.detail.gid !== gid) return;
                    o = o || {};
                    var mb = function (v) { return +(parseLimit(v) / 1048576).toFixed(2); };
                    self.taskOpt = {down: mb(o['max-download-limit']), up: mb(o['max-upload-limit']),
                        connections: Number(o['max-connection-per-server'] || 1), split: Number(o.split || 1)};
                    self.taskOptOrig = Object.assign({}, self.taskOpt);
                    self.taskOptLoaded = true;
                }).catch(function (e) { self.notify('读取任务设置失败：' + e.message, 'error'); });
            },
            saveTaskOptions: function () {
                var self = this, f = this.taskOpt, o = this.taskOptOrig, gid = this.detail.gid, changes = {};
                var toValue = function (mb) { var n = Math.max(0, Number(mb) || 0); return n ? Math.round(n * 1024) + 'K' : '0'; };
                var conns = Math.round(Number(f.connections)), split = Math.round(Number(f.split));
                if (!(conns >= 1 && conns <= MAX_CONN)) { this.notify('单服务器连接数需在 1–' + MAX_CONN + ' 之间', 'error'); return; }
                if (!(split >= 1 && split <= MAX_CONN)) { this.notify('分片数需在 1–' + MAX_CONN + ' 之间', 'error'); return; }
                if (f.down !== o.down) changes['max-download-limit'] = toValue(f.down);
                if (f.up !== o.up) changes['max-upload-limit'] = toValue(f.up);
                if (conns !== o.connections) changes['max-connection-per-server'] = String(conns);
                if (split !== o.split) changes.split = String(split);
                if (!Object.keys(changes).length) { this.notify('没有修改'); return; }
                this.savingTaskOpt = true;
                this.rpc('aria2.changeOption', [gid, changes]).then(function () {
                    self.notify('任务设置已应用');
                    self.taskOptOrig = Object.assign({}, self.taskOpt, {connections: conns, split: split});
                    self.refresh();
                }).catch(function (e) { self.notify('应用失败：' + e.message, 'error'); })
                    .finally(function () { self.savingTaskOpt = false; });
            },
            openDetail: function (t) {
                var self = this;
                this.taskOptLoaded = false; this.taskOpt = {}; this.taskOptOrig = {};
                this.detail = t;
                this.modals.detailModal.show();
                this.rpc('aria2.tellStatus', [t.gid]).then(function (full) {
                    if (self.detail && full && full.gid === self.detail.gid) self.detail = full;
                }).catch(function () {});
            },
            openNew: function () {
                this.form = {mode: 'uri', uris: '', files: [], dir: this.defaultDir, split: this.prefs.newTaskSplit,
                    connections: this.prefs.newTaskConnections, out: '', userAgent: '', referer: '', cookie: '', headers: ''};
                if (this.$refs.torrentInput) this.$refs.torrentInput.value = '';
                this.loadGlobalOptions();
                this.modals.newModal.show();
            },
            pickFiles: function (e) { this.form.files = Array.prototype.slice.call(e.target.files || []); },
            newTaskOptions: function (single) {
                var o = {};
                if (this.form.dir) o.dir = this.form.dir;
                var split = Number(this.form.split);
                if (split > 0) o.split = String(Math.min(MAX_CONN, split));
                var conns = Number(this.form.connections);
                if (conns > 0) o['max-connection-per-server'] = String(Math.min(MAX_CONN, conns));
                if (this.form.userAgent) o['user-agent'] = this.form.userAgent;
                if (this.form.referer) o.referer = this.form.referer;
                var headers = this.headerLines;
                if (headers.length) o.header = headers;
                if (single && this.form.out && this.form.mode === 'uri') o.out = this.form.out;
                return o;
            },
            submitNew: function () {
                var self = this;
                var bad = this.form.headers.split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean)
                    .filter(function (h) { return !/^[A-Za-z0-9!#$%&'*+.^_`|~-]+\s*:/.test(h); });
                if (bad.length) { this.notify('请求头格式应为「名称: 值」：' + bad[0], 'error'); return; }
                this.submitting = true;
                var jobs;
                if (this.form.mode === 'uri') {
                    var lines = this.uriLines, single = lines.length === 1;
                    jobs = lines.map(function (u) { return function () { return self.rpc('aria2.addUri', [[u], self.newTaskOptions(single)]); }; });
                } else {
                    jobs = this.form.files.map(function (f) {
                        return function () {
                            return readFileAsBase64(f).then(function (data) {
                                return /\.(metalink|meta4)$/i.test(f.name)
                                    ? self.rpc('aria2.addMetalink', [data, self.newTaskOptions(false)])
                                    : self.rpc('aria2.addTorrent', [data, [], self.newTaskOptions(false)]);
                            });
                        };
                    });
                }
                // 逐个添加，保证任务在队列中的顺序与输入顺序一致
                var results = [];
                jobs.reduce(function (chain, job) {
                    return chain.then(function () {
                        return job().then(function (v) { results.push({status: 'fulfilled', value: v}); },
                            function (e) { results.push({status: 'rejected', reason: e}); });
                    });
                }, Promise.resolve()).then(function () {
                    var failed = results.filter(function (r) { return r.status === 'rejected'; });
                    var ok = results.length - failed.length;
                    if (failed.length) self.notify('已添加 ' + ok + ' 个，' + failed.length + ' 个失败：' + failed[0].reason.message, 'error');
                    else self.notify('已添加 ' + ok + ' 个下载任务');
                    if (ok) {
                        self.modals.newModal.hide();
                        if (self.list === 'stopped') self.switchList('active'); else self.refresh();
                    }
                }).finally(function () { self.submitting = false; });
            },
            openSettings: function () {
                var self = this;
                this.loadGlobalOptions().then(function () {
                    self.settingsForm = {
                        concurrent: self.globalConcurrent,
                        down: +(self.limits.down / 1048576).toFixed(2),
                        up: +(self.limits.up / 1048576).toFixed(2),
                        userAgent: self.globalUserAgent
                    };
                    self.modals.settingsModal.show();
                });
            },
            saveSettings: function () {
                var self = this, f = this.settingsForm;
                var concurrent = Math.round(Number(f.concurrent));
                if (!(concurrent >= 1 && concurrent <= 999)) { this.notify('最大同时下载数需在 1–999 之间', 'error'); return; }
                if (/[\r\n]/.test(f.userAgent || '')) { this.notify('User-Agent 不能包含换行', 'error'); return; }
                var toValue = function (mb) { var n = Math.max(0, Number(mb) || 0); return n ? Math.round(n * 1024) + 'K' : '0'; };
                var payload = {
                    'max-concurrent-downloads': String(concurrent),
                    'max-overall-download-limit': toValue(f.down),
                    'max-overall-upload-limit': toValue(f.up)
                };
                // 留空表示保持当前 User-Agent，避免把它写成空值
                if (f.userAgent) payload['user-agent'] = f.userAgent;
                this.savingSettings = true;
                axios.post('/api/aria2/global_options', payload).then(function (r) {
                    self.modals.settingsModal.hide();
                    self.notify(r.data.msg || '设置已保存');
                    self.loadGlobalOptions();
                    self.refresh();
                }).catch(function (e) {
                    var d = e.response && e.response.data;
                    self.notify((d && d.msg) || '保存失败：' + e.message, 'error');
                    if (d && d.applied) { self.modals.settingsModal.hide(); self.loadGlobalOptions(); }
                }).finally(function () { self.savingSettings = false; });
            },
            savePrefs: function () {
                this.prefs = sanitizePrefs(this.prefs);
                try { localStorage.setItem(PREFS_KEY, JSON.stringify(Object.assign({v: PREFS_VERSION}, this.prefs))); } catch (_) {}
            },
            openPrefs: function () { this.prefsForm = Object.assign({}, this.prefs); this.prefsImport = ''; this.modals.prefsModal.show(); },
            applyPrefsForm: function () {
                this.prefs = sanitizePrefs(this.prefsForm);
                this.savePrefs();
                this.modals.prefsModal.hide();
                this.notify('偏好设置已保存');
                this.schedule();
            },
            resetPrefs: function () { this.prefsForm = Object.assign({}, DEFAULT_PREFS); },
            importPrefs: function () {
                var data;
                try {
                    data = JSON.parse(this.prefsImport);
                    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error();
                } catch (_) { this.notify('偏好设置必须是有效的 JSON 对象', 'error'); return; }
                this.prefsForm = sanitizePrefs(Object.assign({}, this.prefs, data));
                this.applyPrefsForm();
            },
            exportPrefs: function () { this.copy(JSON.stringify(this.prefs, null, 2), '偏好设置已复制到剪贴板'); },
            copy: function (text, msg) {
                var self = this;
                var done = function () { self.notify(msg || '已复制'); };
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(text).then(done, function () { self.notify('无法写入剪贴板', 'error'); });
                    return;
                }
                var field = document.createElement('textarea');
                field.value = text;
                field.setAttribute('readonly', '');
                field.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
                // 放进当前弹窗，避免 Bootstrap 弹窗的焦点锁把焦点抢走导致复制失败
                (document.querySelector('.modal.show .modal-content') || document.body).appendChild(field);
                field.focus();
                field.select();
                field.setSelectionRange(0, text.length);
                var ok = document.execCommand('copy');
                field.remove();
                if (ok) done(); else this.notify('无法写入剪贴板', 'error');
            }
        }
    };
    window.ScannerDownloader.sanitizePrefs = sanitizePrefs;
}());
