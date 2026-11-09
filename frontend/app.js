﻿﻿﻿﻿﻿﻿﻿﻿﻿// ==========================================================================
// Spire Mod Manager — 前端 Vue3 应用
// 版本号约定: 语义化 SEMVER（Alpha/Beta 阶段用 0.x.y，稳定后 1.0.0）
//   MAJOR = 不兼容的 API/架构改动
//   MINOR = 新功能（依赖线、实时异步、NexusMods 来源识别等）
//   PATCH = 修 bug + 小优化（图标、文案、竞态修复等）
// 当前版本: 0.4.0（依赖线实时 + 异步 IO + 来源图标 + 内容标签）
// ==========================================================================

// ========== 全局 JS 错误捕获（必须在最顶部，Vue 导入之前注册）==========
window.onerror = function(msg, url, line, col, err) {
  console.error('[JS ERR]', msg, url, line, col, err);
  try {
    const api = window.pywebview && window.pywebview.api;
    if (api && api.log_js_error) {
      api.log_js_error(String(msg||''), String(url||''), line||0, col||0, err && err.stack || '');
    }
  } catch(_) {}
};
window.addEventListener('unhandledrejection', function(e) {
  console.error('[PROMISE ERR]', e.reason);
  try {
    const api = window.pywebview && window.pywebview.api;
    if (api && api.log_js_error) {
      const r = e.reason || {};
      api.log_js_error(
        String(r.message || r || 'Promise rejection'),
        String(r.fileName || ''),
        r.lineNumber || 0,
        r.columnNumber || 0,
        r.stack || ''
      );
    }
  } catch(_) {}
});

const { createApp, ref, reactive, computed, watch, onMounted, nextTick } = Vue;

const app = createApp({
  setup() {
    // ========================================================================
    // setup() 总览
    // ------------------------------------------------------------------------
    // 模块划分 (按代码顺序):
    //   1. API 包装层        (L906 附近) — pywebview vs mock 兜底
    //   2. i18n 翻译字典     (L920 附近) — 中英双语 + tag 映射
    //   3. 响应式数据        (L770 附近) — state/selected/menus/drag/depLayer
    //   4. 计算属性          (L1024 附近) — filteredEnabled, displayList, selectedTags...
    //   5. 核心操作函数      (L1213 附近) — refresh, applyState, select, toggle
    //   6. Workshop 爬取     (L1405 附近) — loadWorkshop 异步抓取详情
    //   7. 列表扁平化        (L2337 附近) — expandModList + displayList (flat 多副本)
    //   8. 拖拽系统          (L2173 附近) — onDragStart/Move/Up, placeholder 插入
    //   9. 依赖线 SVG        (L2722 附近) — computeGroups + drawDeps
    //  10. 设置/备份/皮肤...  (散布各处)
    //  11. return 导出       (L2854 附近)
    // ========================================================================

    // === 响应式数据 ===
    const state = reactive({ enabled: [], disabled: [], packs: [], backups: [], backup_stats: {}, game_root: null, errors: [], steam_id: null, subscribed_known: true, breakdown: null });
    const selected = ref(null);
    // 2026-11-05 Ctrl/Shift 多选集合：ref([]) 而非 Set，Vue 能追踪 .includes() 变化
    const multiSelected = ref([]);
    const multiSelectedCount = computed(() => multiSelected.value.length);
    const _multiHas = (vk) => multiSelected.value.includes(vk);
    const _multiAdd = (vk) => { if (!_multiHas(vk)) multiSelected.value.push(vk); };
    const _multiDel = (vk) => { multiSelected.value = multiSelected.value.filter(x => x !== vk); };
    const _multiClear = () => { multiSelected.value = []; };
    // shift 多选的锚点（第一次点击的副本 version_key）
    const _rangeAnchorKey = ref(null);

    // ====== 延迟保存：dirty flag ======
    const hasUnsavedChanges = ref(false);
    const markDirty = () => { hasUnsavedChanges.value = true; };
    const clearDirty = () => { hasUnsavedChanges.value = false; };

    // ====== 快照比对法：自动检测净变化 ======
    let savedStateSnapshot = '';
    const _snapshot = () => JSON.stringify({
      en: state.enabled.map(m => m.id.toLowerCase()),  // 保留顺序，不 sort（sort 会让拖拽排序变化检测失效）
      dis: state.disabled.map(m => m.id.toLowerCase()),
    });
    const updateSnapshot = () => {
      savedStateSnapshot = _snapshot();
      hasUnsavedChanges.value = false;
      touchedKeys.clear();
    };
    const checkDirtyStatus = () => {
      const cur = _snapshot();
      if (cur === savedStateSnapshot) {
        hasUnsavedChanges.value = false;
        touchedKeys.clear();
      } else {
        hasUnsavedChanges.value = true;
      }
    };

    // 2026-11-07 Step 3: 前端脏数据标记
    // 三元组 [mod_id_lower, workshop_id_or_null, version_key]
    // workshop_id 统一转 null（漏洞 3）
    // Set 存 JSON string，save() 时反解成 just_touched_list 数组传给后端
    const touchedKeys = new Set();
    const enabledSearch = ref('');
    const disabledSearch = ref('');
    // 界面语言（zh / en）与标签中英文对照表
    const lang = ref('zh');
    const tagI18n = reactive({});        // 中文 -> 英文
    const tagI18nEn2zh = reactive({});   // 英文(lower) -> 中文
    const launchState = ref('idle');  // idle / launching / launched
    const launchText = computed(() => {
      if (launchState.value === 'launching') return t('launching');
      if (launchState.value === 'launched') return t('launched');
      return t('launch');
    });
    const depLayer = ref(null);
    const depHeight = ref(0);
    const depScrollTop = ref(0);
    // 自实现拖拽状态（Pointer Events，flat 数组渲染但 canonical 级重排）
    // 跨列表拖拽：disabled→enabled=启用，enabled→disabled=停用；同列表=排序
    const drag = reactive({
      active: false,         // 是否正在拖拽中
      fromList: null,        // 起始列表：'enabled' 或 'disabled'
      targetList: null,      // 光标当前在哪个列表（实时更新）
      fromIdx: -1,           // flat 数组起始索引（仅 enabled 内部排序用）
      targetIdx: -1,         // flat 数组目标索引（仅 enabled 内部排序用）
      fromModId: null,       // 被拖 canonical id（延迟保存架构后只改前端 state，不再提交后端 set_enabled / reorder_enabled）
      fromMod: null,         // 被拖 flat 副本对象（ghost 显示用）
      pointerX: 0,
      pointerY: 0,
      itemHeight: ITEM_HEIGHT,
      _pointerDownX: 0,
      _pointerDownY: 0,
      _hasMoved: false,
    });

    // 全局缓存：state.enabled/disabled 变动时才重新 expandModList（700+ 项拉平很贵）
    const _cachedAllFlat = computed(() => {
      return expandModList([...state.enabled, ...state.disabled]);
    });

    // 拖拽期间渲染数组：直接返回完整列表（不挤开任何元素）
    // 用一条绝对定位的 .drag-indicator 线显示 drop 位置，而不是 placeholder 挤开
    // 这样列表布局完全不动，用户看到的就是 RimSort 风格：原处半透明虚影 + 一条指示线 + 幽灵卡牌
    const dragRenderList = computed(() => {
      return displayList(filteredEnabled.value, 'enabled', enabledSearch.value);
    });

    // 拖拽指示线（不挤开列表，只画一条横线指示 drop 位置）
    // 只在 targetList === 'enabled' 时显示（内部排序 + disabled→enabled）
    // targetList === 'disabled' 时不画线（停用列表不排序）
    const indicatorStyle = computed(() => {
      if (!drag.active || !drag._hasMoved) return null;
      if (drag.targetList !== 'enabled') return null;
      const list = _getListEl('enabled');
      if (!list) return null;
      const targetIdx = drag.targetIdx;
      const items = list.querySelectorAll('.mod-item:not(.drag-placeholder)');
      const listRect = list.getBoundingClientRect();
      let top;
      if (targetIdx <= 0 || items.length === 0) {
        top = 0;
      } else if (targetIdx >= items.length) {
        const lastRect = items[items.length - 1].getBoundingClientRect();
        top = lastRect.bottom - listRect.top;
      } else {
        const r = items[targetIdx].getBoundingClientRect();
        top = r.top - listRect.top;
      }
      return { top: top + 'px' };
    });
    const menu = reactive({ show: false, x: 0, y: 0, mod: null, versions: [], toggleText: '' });
    // 备份：右键菜单 / 还原弹窗 / 设置
    const backupMenu = reactive({ show: false, x: 0, y: 0, backup: null });
    const backupMenuEl = ref(null);
    const restoreDlg = reactive({ show: false, backup: null, busy: false });
    // 备份/还原/校验任务状态
    const backupBusy = reactive({ show: false, text: '', mode: 'indeterminate', done: 0, total: 0 });
    const backupSettings = reactive({ auto_backup: true, keep_versions: 3, backup_dir: '', effective_dir: '' });
    const packMenu = reactive({ show: false, x: 0, y: 0, pack: null });
    const menuEl = ref(null);
    const packMenuEl = ref(null);
    // 管理器标签弹窗
    const tagMenu = reactive({ show: false, mod: null, tags: [], allTags: [], newTag: '' });
    const packTab = ref('packs');
    const skin = reactive({ available: false, roles: [], config_found: false, manager: 'native' });
    const card = reactive({ available: false, roles: [], config_found: false, manager: 'native' });
    const settings = reactive({ show: false, form: { manual_game_root: '', manual_mods_dirs: '', extra_mods_list: [], manual_workshop_dir: '', manual_mods_dir: '', manual_save_dir: '', language: 'zh', workshop_html_fill: true } });
    const previewCollapse = reactive({ siblings: false, status: true });
    const settingsTab = ref('paths');
    // 工坊更新检查
    const workshopUpdates = reactive({ checking: false, checked: 0, errors: 0, updates: [], lastChecked: '' });
    // 替代 mod 建议
    const replacementSuggestions = reactive({ suggestions: [], scanned_count: 0, idle_count: 0, note: '', lastChecked: '' });
    const replacementChecking = ref(false);
    // steamDB 导出
    const steamdbExport = reactive({ path: '', entries: 0, with_deps: 0, exporting: false });
    // 多人联机兼容性
    const mpCompat = reactive({ checking: false, results: {}, stats: {}, lastChecked: '' });
    // 应用信息（关于页）
    const appInfo = reactive({ version: '' });
    // 前端版本号（cache-busting 真源，由 index.html head 里的 __FE_VERSION__ 注入）
    const feVer = window.__FE_VERSION__ || 'unknown';
    // DLL 分析进度（中下方进度条）
    const dllProgress = reactive({ show: false, current: '', done: 0, total: 0 });
    // 扫描进度（中下方进度条）
    const scanProgress = reactive({ show: false, stage: '', current: '', done: 0, total: 0 });

    // 游戏版本 & Beta 分支信息
    const gameInfo = reactive({
      version: '',
      version_detected: false,
      beta_key: '',
      beta_label: '未知分支',
      root: null,
      exe: null,
    });

    // 存档管理面板
    const savesManager = reactive({
      show: false,
      profiles: [],   // [{ profile: 1, official: {...}, modded: {...} }]
      backups: [],    // [{ profile, official: [...], modded: [...] }]
      selectedSlot: null,
      busy: false,
    });
    // 批量重新下载预览进度（右上角进度条）
    const bulkProgress = reactive({ show: false, done: 0, total: 0 });
    // 游戏日志查看器
    const logDiag = reactive({
      show: false, loading: false, available: false, path: '',
      files: [], file: '', level: 'all', keyword: '', useRegex: false,
      lines: [], counts: { info: 0, warn: 0, error: 0, other: 0 },
      total: 0, shown: 0, truncated: false,
    });
    // 标签诊断面板
    const tagDiag = reactive({ show: false, loading: false, items: [], filter: '', selftest: [], selftestLoading: false });
    // 右上角悬浮提示（仅提示类信息，带手动关闭与悬停暂停）
    const toasts = reactive([]);
    let toastSeq = 0;
    const TOAST_DURATION = { success: 3000, info: 3000, warning: 4000, error: 5000 };
    const TOAST_MAX = 5;
    const TOAST_DEDUPE_MS = 2000;
    const toastTimers = new Map();   // id -> { timer, started, remaining }
    const toastRecent = new Map();   // "type|text" -> 最近出现时间
    const toastClosed = new Set();   // 用户手动关闭过的 key

    function _clearToastTimer(id) {
      const st = toastTimers.get(id);
      if (st && st.timer) clearTimeout(st.timer);
      toastTimers.delete(id);
    }

    function _evictToastForRoom() {
      // 错误优先保留：先淘汰最早的非错误提示
      const idx = toasts.findIndex(x => x.type !== 'error');
      dismissToast(idx >= 0 ? toasts[idx].id : toasts[0].id);
    }

    function showToast(text, type = 'info', opts = {}) {
      const content = String(text == null ? '' : text);
      const kind = TOAST_DURATION[type] ? type : 'info';
      const key = opts.key || null;
      // opts.duration 允许调用方自定义倒计时（进度条同步走完），未传则用类型默认时长
      const dur = Number(opts.duration) > 0 ? Number(opts.duration) : null;
      const now = Date.now();
      for (const [k, ts] of toastRecent) {
        if (now - ts > 5000) toastRecent.delete(k);
      }

      // 同 key 原地更新，不新增第二条
      if (key) {
        const found = toasts.find(x => x.key === key);
        if (found) {
          found.text = content;
          if (kind === 'error') found.type = 'error';
          else if (found.type !== 'error' && kind !== 'info') found.type = kind;
          if (opts.sticky) {
            _clearToastTimer(found.id);
            found.hasBar = false;   // sticky 开始提示不显示倒计时进度条
          } else armToastTimer(found.id, dur || TOAST_DURATION[found.type]);
          return found.id;
        }
        // 手动关闭过的 key 不复活；错误升级除外
        if (toastClosed.has(key) && kind !== 'error') return null;
      }

      // 同类事件合并计数（不同 wid 的同类事件，2 秒窗口内累加）
      if (opts.mergeKey) {
        const merged = toasts.find(x => x.mergeKey === opts.mergeKey && !x.key);
        if (merged) {
          merged.count = (merged.count || 1) + 1;
          merged.text = opts.mergeText ? opts.mergeText(merged.count) : content;
          armToastTimer(merged.id, TOAST_DURATION[merged.type]);
          return merged.id;
        }
      }

      // 无 key 的重复文案去重
      const dedupeKey = kind + '|' + content;
      const last = toastRecent.get(dedupeKey);
      if (last && now - last < TOAST_DEDUPE_MS) return null;
      toastRecent.set(dedupeKey, now);

      while (toasts.length >= TOAST_MAX) _evictToastForRoom();
      const id = ++toastSeq;
      toasts.push({ id, key, text: content, type: kind, mergeKey: opts.mergeKey || null, count: 1, hasBar: false, ratio: 1 });
      if (opts.sticky) _clearToastTimer(id);
      else armToastTimer(id, dur || TOAST_DURATION[kind]);
      return id;
    }

    function armToastTimer(id, duration, total) {
      _clearToastTimer(id);
      const st = { started: Date.now(), remaining: duration, duration: total || duration, timer: setTimeout(() => dismissToast(id), duration) };
      toastTimers.set(id, st);
      const item = toasts.find(x => x.id === id);
      if (item) {
        item.hasBar = true;
        // 不再写 item.ratio —— 进度条完全由 _refreshToastBars DOM 直写
      }
      // 初始拉满（100%）：直接 DOM 写，不等 rAF 首帧
      requestAnimationFrame(() => {
        const el = document.querySelector(`[data-toast-bar="${id}"]`);
        if (el) el.style.width = '100%';
      });
      _startToastBarLoop();
    }

    // 进度条跟随倒计时：rAF 每帧直接写 DOM style.width —— 完全绕开 Vue 响应式
    // 这样 5 个 toast 同时倒计时也不会触发模板重渲染，CSS transition 丝滑生效
    // _refreshToastBars 返回 true 表示还有活跃 timer，rAF 继续循环；false 则退出
    let toastBarRaf = null;
    function _refreshToastBars() {
      let anyActive = false;
      const now = Date.now();
      for (const item of toasts) {
        const st = toastTimers.get(item.id);
        if (!st || !st.timer) continue;
        anyActive = true;
        const pct = st.duration > 0
          ? Math.max(0, st.remaining - (now - st.started)) / st.duration * 100
          : 0;
        const el = document.querySelector(`[data-toast-bar="${item.id}"]`);
        if (el) el.style.width = pct + '%';
      }
      return anyActive;
    }
    function _startToastBarLoop() {
      if (toastBarRaf) return;
      const step = () => {
        toastBarRaf = null;
        if (_refreshToastBars()) toastBarRaf = requestAnimationFrame(step);
      };
      toastBarRaf = requestAnimationFrame(step);
    }

    function pauseToast(item) {
      const st = item && toastTimers.get(item.id);
      if (!st || !st.timer) return;
      clearTimeout(st.timer);
      st.timer = null;
      st.remaining = Math.max(0, st.remaining - (Date.now() - st.started));
      // 立即 DOM 写冻结位置（_refreshToastBars 下帧会因 st.timer=null 跳过）
      if (item) {
        const pct = st.duration > 0 ? st.remaining / st.duration * 100 : 0;
        const el = document.querySelector(`[data-toast-bar="${item.id}"]`);
        if (el) el.style.width = pct + '%';
      }
    }

    function resumeToast(item) {
      const st = item && toastTimers.get(item.id);
      if (!st) return;
      if (st.remaining <= 0) { dismissToast(item.id); return; }
      // 带上原始总时长，进度条从当前比例继续而不是重新拉满
      armToastTimer(item.id, st.remaining, st.duration);
    }

    function dismissToast(id, manual = false) {
      const idx = toasts.findIndex(x => x.id === id);
      const item = idx >= 0 ? toasts[idx] : null;
      if (manual && item && item.key) toastClosed.add(item.key);
      _clearToastTimer(id);
      if (idx >= 0) toasts.splice(idx, 1);
    }

    function dismissToastKey(key) {
      const item = toasts.find(x => x.key === key);
      if (item) dismissToast(item.id);
    }

    // 后端异步派发支持：pywebview API 返回 {"ok":true, "async_id":"xxx"} 时，
    // 前端等 evaluate_js 回调 resolve/reject，让调用方正常用 .then/.catch
    const __asyncPending = new Map();
    window.__onBackendDone = function(asyncId, ok, result) {
      const p = __asyncPending.get(asyncId);
      if (p) {
        __asyncPending.delete(asyncId);
        if (ok) p.resolve(result);
        else p.reject(new Error(result && result.error ? result.error : 'backend async failed'));
      }
    };

    // 后端检测到 mod 被 Steam 更新并自动补备份后回调前端（弹提示 + 刷新备份列表）
    window.__onAutoBackup = function(names) {
      try {
        api().list_backups().then(r => {
          if (r && r.backups) { state.backups = r.backups; state.backup_stats = r.stats || state.backup_stats; }
        }).catch(() => {});
        const names = Array.isArray(names) ? names : [];
        if (names.length === 1) {
          showToast(t('auto_backup_done', { name: names[0] }), 'success');
        } else if (names.length > 1) {
          const shown = names.slice(0, 3).join(', ') + (names.length > 3 ? '…' : '');
          showToast(t('auto_backup_done_many', { n: names.length, names: shown }), 'success');
        }
      } catch (_) {}
    };

    // 包装 pywebview.api：所有方法自动把 async_id 返回值转成 Promise
    function _wrapApi(rawApi) {
      if (!rawApi || rawApi.__wrapped) return rawApi;
      const wrapped = {};
      const seen = new Set();
      const collectKeys = (obj) => {
        if (!obj) return;
        for (const k of Object.getOwnPropertyNames(obj)) {
          if (k !== 'constructor' && !seen.has(k)) { seen.add(k); collectKeys(obj.__proto__); }
        }
      };
      const keys = new Set();
      let cur = rawApi;
      while (cur) {
        for (const k of Object.getOwnPropertyNames(cur)) {
          if (k !== 'constructor' && typeof cur[k] === 'function') keys.add(k);
        }
        cur = Object.getPrototypeOf(cur);
        if (cur === Object.prototype) break;
      }
      for (const key of keys) {
        const fn = rawApi[key].bind(rawApi);
        wrapped[key] = async (...args) => {
          // pywebview API 本身返回 Promise，先 await 拿到真实返回值
          const ret = await fn(...args);
          if (ret && ret.async_id) {
            // 后端走了 _dispatch_async → 等 evaluate_js 回调
            return new Promise((resolve, reject) => {
              __asyncPending.set(ret.async_id, { resolve, reject });
            });
          }
          return ret;
        };
      }
      wrapped.__wrapped = true;
      return wrapped;
    }

    const api = () => {
      if (hasPywebviewApi()) return _wrapApi(window.pywebview.api);
      return mockApi;
    };

    // Steam Workshop 原始标签 → 中文（每个标签独立翻译，不合并为笼统分类）
    // 设计原则：
    //   - 工坊标签是 Steam 官方/社区定义的具体分类名 → 直译/正规翻译（Tools & APIs → 工具与接口）
    //   - 自定义标签是我们内部的笼统分类（依赖/工具/外观） → 不在这张表里，由后端 tagI18n 提供
    //   - 同一中文只能对应一个英文（MANIFEST_TAG_EN 反向表要求唯一）
    const MANIFEST_TAG_ZH = {
      // ── 核心玩法内容 ──
      'card': '卡牌', 'cards': '卡牌',
      'power': '能力', 'powers': '能力',
      'relic': '遗物', 'relics': '遗物',
      'event': '事件', 'events': '事件',
      'enemy': '敌人', 'enemies': '敌人', 'monster': '怪物', 'monsters': '怪物',
      'potion': '药水', 'potions': '药水',
      'map': '地图', 'maps': '地图',
      'character': '角色', 'characters': '角色', 'hero': '英雄', 'heroes': '英雄',
      'boss': 'Boss', 'bosses': 'Boss',
      'minion': '仆从', 'minions': '仆从',
      'gameplay': '玩法', 'content': '内容',
      'modifiers': '修饰器', 'multiplayer': '多人',
      'balance': '平衡', 'rebalance': '平衡性调整',
      // ── 外观 / 美术 ──
      'skin': '皮肤', 'skins': '皮肤',
      'cosmetic': '装饰', 'cosmetics': '装饰',
      'appearance': '外观',
      'art': '美术', 'animation': '动画',
      'voice': '语音', 'sfx': '音效', 'audio': '音效', 'music': '音乐',
      'portrait': '立绘', 'card portraits': '卡面', 'card portrait': '卡面',
      // ── 工具 / QoL ──
      'tools & apis': '工具与接口', 'tools & api': '工具与接口',
      'utility': '实用工具', 'tool': '工具', 'tools': '工具',
      'qol': '便捷功能', 'QoL': '便捷功能', 'QOL': '便捷功能',
      'quality of life': '便捷功能',
      'enhancement': '增强', 'expansion': '扩展',
      'ui': '界面', 'interface': '界面', 'hud': '界面',
      'config': '配置',
      // ── 依赖 / 库 ──
      'library': '资料库', 'lib': '库', 'api': 'API',
      'framework': '框架', 'dependency': '依赖',
      // ── 修复 / 兼容 ──
      'silent': '静默模式',     // STS2 workshop tag = 静默修复，不是 STS1 角色名！
      'fix': '修复', 'compat': '兼容',
      // ── 特定 mod 名（旧 STS1 角色等，在 STS2 tags 里还偶尔出现）──
      'regent': '储君', 'defectskin': '机器人皮肤', 'defect_skin': '机器人皮肤',
      'watcherskin': '观察者皮肤', 'reskin': '换肤',
      // ── 其它 ──
      'misc': '杂项', 'Misc': '杂项', 'mod': '模组', 'other': '其它',
      'enchantment': '附魔', 'humor': '搞笑', 'ancients': '远古',
      'pack': '整合包',
      'base-game': '基础游戏', 'base game': '基础游戏',
      'downfall': '陨落', 'colorless': '无色',
      'campaign': '战役', 'act': '章节',
      'deck': '卡组',
      'gold': '金色', 'Gold': '金色',
      'texture': '贴图', 'Textures': '贴图',
      // ── 语言标签（Steam 标注的支持语言）──
      'simplified chinese': '简体中文', 'schinese': '简体中文',
      'traditional chinese': '繁体中文', 'tchinese': '繁体中文',
      'english': '英语', 'japanese': '日语', 'koreana': '韩语', 'korean': '韩语',
      'russian': '俄语', 'french': '法语', 'german': '德语', 'spanish': '西班牙语',
      'italian': '意大利语', 'polish': '波兰语', 'portuguese': '葡萄牙语',
      'turkish': '土耳其语', 'dutch': '荷兰语', 'ukrainian': '乌克兰语',
      'chinese': '中文',
    };
    const MANIFEST_TAG_EN = {};
    for (const [k, v] of Object.entries(MANIFEST_TAG_ZH)) MANIFEST_TAG_EN[v] = k;

    // 标签双向翻译：先查内置 manifest 标准标签，再查后端用户自定义 tagI18n
    function tr(tag) {
      if (!tag) return tag;
      const low = String(tag).toLowerCase();
      if (lang.value === 'en') {
        return MANIFEST_TAG_EN[tag] || tagI18n[tag] || tag;
      }
      return MANIFEST_TAG_ZH[low] || tagI18nEn2zh[low] || tag;
    }

    // 界面文案翻译：按当前语言取 I18N 文案，支持 {key} 占位符替换
    function t(key, params) {
      const dict = I18N[lang.value] || I18N.zh;
      let s = dict[key];
      if (s === undefined) s = I18N.zh[key];
      if (s === undefined) s = key;
      if (params) {
        Object.keys(params).forEach(k => {
          s = s.split('{' + k + '}').join(params[k]);
        });
      }
      return s;
    }

    // mod 来源显示文案（本地 / 创意工坊）
    function sourceText(source) {
      return source === 'workshop' ? t('src_workshop') : t('src_local');
    }

    // 读取界面语言 + 标签对照表（不依赖扫描，可单独加载）
    async function loadLanguageSettings() {
      try {
        const s = await api().get_settings();
        if (s && (s.language === 'zh' || s.language === 'en')) lang.value = s.language;
        if (s && s.tag_i18n) {
          Object.keys(tagI18n).forEach(k => delete tagI18n[k]);
          Object.assign(tagI18n, s.tag_i18n);
        }
        if (s && s.tag_i18n_en2zh) {
          Object.keys(tagI18nEn2zh).forEach(k => delete tagI18nEn2zh[k]);
          Object.assign(tagI18nEn2zh, s.tag_i18n_en2zh);
        }
        if (s && s.backup) loadBackupSettings(s);
      } catch (e) {
        // 忽略：语言设置读取失败时保持默认 zh
      }
    }

    // === 计算属性 ===
    const filteredEnabled = computed(() => {
      const q = enabledSearch.value.trim().toLowerCase();
      if (!q) return state.enabled;
      return state.enabled.filter(m => matches(m, q));
    });

    const filteredDisabled = computed(() => {
      const q = disabledSearch.value.trim().toLowerCase();
      return q ? state.disabled.filter(m => matches(m, q)) : state.disabled;
    });

    function matches(m, q) {
      return (m.name || '').toLowerCase().includes(q)
        || (m.author || '').toLowerCase().includes(q)
        || (m.id || '').toLowerCase().includes(q);
    }

    const selectedMeta = computed(() => {
      if (!selected.value) return '';
      const m = selected.value;
      const parts = [];
      if (m.author) parts.push(t('author_prefix') + m.author);
      if (m.version) parts.push(fmtVersion(m.version));
      // source_label 已在徽章上显示，meta 里不再重复
      if (m.workshop_id) parts.push(t('workshop_prefix') + m.workshop_id);
      return parts.join(' · ');
    });

    // 本地 manifest 自带的标签（非工坊 mod 也有）
    const selectedLocalTags = computed(() => {
      if (!selected.value) return [];
      return (selected.value.tags || []).filter(Boolean);
    });

    // 管理器标签（用户自定义 / 工坊映射 / 本地分析）
    // 返回 [{tag, source}] 数组，source: "manual" / "workshop" / "local"
    const selectedManagerTags = computed(() => {
      if (!selected.value) return [];
      const tags = (selected.value.manager_tags || []).filter(Boolean);
      const src = selected.value.tag_source || "local";
      return tags.map(t => ({ tag: t, source: src }));
    });

    // ====== 同文件夹兄弟 mod 映射 ======
    // 找路径中最后一个纯数字段（Workshop ID），parent 截到那一级（含）
    // 例：.../content/2868840/3748583469/AliceDefectSkin → parent = .../2868840/3748583469
    // 例：.../content/2868840/3747602295 (无子目录) → parent = .../2868840/3747602295（正确，RituLib 自己一个）
    const _getParentDir = (path) => {
      if (!path) return null;
      const parts = path.replace(/\\/g, '/').split('/').filter(Boolean);
      if (parts.length < 3) return null;
      // 找最后一个纯数字段（Workshop ID）
      let lastNumIdx = -1;
      for (let i = 0; i < parts.length; i++) {
        if (/^\d+$/.test(parts[i])) lastNumIdx = i;
      }
      if (lastNumIdx < 0) return null;  // 没找到数字段（本地 mods/ 目录下）
      // 如果数字段已经是最后一段（无子目录），parent 就截到那级（含）
      // 如果数字段后面还有子目录，也截到那级（含）
      return parts.slice(0, lastNumIdx + 1).join('/');
    };
    const siblingModsMap = computed(() => {
      const map = {};
      const all = [...state.enabled, ...state.disabled];
      for (const m of all) {
        const parent = _getParentDir(m.path);
        if (!parent) continue;
        if (!map[parent]) map[parent] = [];
        map[parent].push(m);
      }
      return map;
    });
    const selectedSiblings = computed(() => {
      const s = selected.value;
      if (!s || !s.path) return [];
      const parent = _getParentDir(s.path);
      if (!parent) return [];
      const all = siblingModsMap.value[parent] || [];
      // 用 entryKey 过滤（不是 id）——同一物理副本才排除
      const _ek = (m) => {
        const wid = m && m.workshop_id ? String(m.workshop_id) : '';
        const mid = (m && m.id || '').toLowerCase();
        return wid + '|' + mid;
      };
      const myEk = _ek(s);
      return all.filter(m => _ek(m) !== myEk);
    });

    // 命中的关键词（作为 tag 徽标直观展示）
    const selectedTagHits = computed(() => {
      if (!selected.value) return [];
      return (selected.value.tag_hits || []).filter(Boolean);
    });

    // 管理器标签 + 命中关键词 合并去重：翻译后相同只保留一个（先管理器标签，再命中词）。
    // 只隐藏重复项，不改动底层数据。
    const selectedTags = computed(() => {
      const seen = new Set();
      const items = [];
      for (const tag of selectedManagerTags.value) {
        const text = tr(tag);
        if (!text || seen.has(text)) continue;
        seen.add(text);
        items.push({ text, type: 'manager' });
      }
      for (const h of selectedTagHits.value) {
        const text = tr(h);
        if (!text || seen.has(text)) continue;
        seen.add(text);
        items.push({ text, type: 'hit' });
      }
      return items;
    });

    // ---- Per-field fallback：workshop 数据可能不完整（被限流、正则截断），需要逐项验证有效性 ----
    function _validDescription(d) {
      // workshop description 不能只是标题级别的短文本（< 15 字符）
      return d && d.length >= 15;
    }
    function _validPreview(p) {
      return !!p;
    }

    // 实际生效的描述（workshop 有效优先 → 本地 manifest → 带依赖信息的）
    const effectiveDescription = computed(() => {
      if (!selected.value) return '';
      const m = selected.value;
      // 1) workshop 数据的描述（有效才用）
      if (workshop.data && _validDescription(workshop.data.description)) {
        return workshop.data.description;
      }
      // 2) 本地 manifest description（有效才用）
      const localDesc = (m.description || '').trim();
      if (_validDescription(localDesc)) {
        return localDesc;
      }
      // 3) 退回 selectedDesc（带依赖信息的版本）
      return selectedDesc.value;
    });

    // 实际生效的预览图 URL（RimCrow 设计：优先用 CDN URL，让浏览器直接拉图）
    const effectivePreview = computed(() => {
      if (!selected.value) return null;
      const m = selected.value;
      // 1) preview：后端 _load_cached 返回的本地 file:// URL（preview.jpg/png 存磁盘）
      //    优先本地图——pywebview 的 WebView2 缓存不持久化，CDN URL 每次重启都要重新拉
      if (workshop.data && _validPreview(workshop.data.preview)) {
        return workshop.data.preview;
      }
      // 2) preview_url：Steam CDN URL（images.steamusercontent.com）——本地图没有时的 fallback
      if (workshop.data && workshop.data.preview_url) {
        return workshop.data.preview_url;
      }
      // 3) 本地 mod 文件夹里的 icon/preview/banner
      if (_validPreview(m.local_preview)) {
        return m.local_preview;
      }
      return null;
    });

    // workshop 标题有效才覆盖本地名（本地名从 manifest，总是更可靠）
    const effectiveTitle = computed(() => {
      if (workshop.data && workshop.data.title && selected.value
          && workshop.data.title.length > selected.value.name.length + 2) {
        return workshop.data.title;
      }
      return selected.value ? selected.value.name : '';
    });

    // workshop 作者有效才覆盖本地 author
    const effectiveAuthor = computed(() => {
      if (workshop.data && _validDescription(workshop.data.description)) {
        if (workshop.data.author) return workshop.data.author;
      }
      return selected.value ? selected.value.author : '';
    });

    const selectedDesc = computed(() => {
      if (!selected.value) return t('select_hint');
      const m = selected.value;
      let desc = m.description || t('no_desc');
      if (m.dependencies && m.dependencies.length) {
        let head = t('deps_label') + m.dependencies.join(', ');
        if (m.missing_deps && m.missing_deps.length) {
          head += '\n' + t('missing_deps_label') + m.missing_deps.join(', ');
        }
        desc = head + '\n\n' + desc;
      }
      return desc;
    });

    // 提示/按钮只在双语档（15%~30%）出现；<15% 由后台自动抓，≥30% 已有中文
    // 阈值与后端 workshop.py 的 _ZH_RATIO_WEAK / _ZH_RATIO_ENOUGH 保持一致
    const ZH_RATIO_WEAK = 0.15;
    const ZH_RATIO_ENOUGH = 0.30;
    const translationBusy = ref(false);
    const translationPolling = ref(false);
    let translationPollToken = 0;
    const _descRatio = () => {
      const d = workshop.data;
      return d && typeof d.zh_ratio === 'number' ? d.zh_ratio : null;
    };
    const _isBilingual = (d, ratio) => ratio === null
      ? !!d.is_bilingual
      : (ratio >= ZH_RATIO_WEAK && ratio < ZH_RATIO_ENOUGH);
    const canFetchTranslation = computed(() => {
      const d = workshop.data;
      if (!d || !selected.value || !selected.value.workshop_id) return false;
      if (translationPolling.value) return false;  // 抓取中只留 Toast
      if (!_isBilingual(d, _descRatio())) return false;
      // 当前版本已检查过（无论是否找到中文）→ 收起，避免反复提示
      const checkedCurrent = !!(d.html_checked
        && Number(d.html_time_updated || 0) === Number(d.time_updated || 0));
      return !checkedCurrent;
    });
    const translationHint = computed(() => {
      const d = workshop.data;
      if (!d) return '';
      if (_isBilingual(d, _descRatio())) return t('desc_bilingual');
      return t('desc_original_lang', { lang: t('lang_' + (d.desc_lang || 'unknown')) });
    });

    async function fetchCommunityTranslation() {
      const m = selected.value;
      if (!m) return;
      translationBusy.value = true;
      try {
        const r = await api().request_workshop_translation(m.id, m.workshop_id);
        if (!r || !r.ok) {
          showToast(t('translation_failed'), 'warning');
          return;
        }
        showToast(t('translation_queued'), 'info', { key: 'html:' + (m.workshop_id || ''), sticky: true });
        _pollHtmlFill(m, workshopSeq);
      } catch (e) {
        showToast(t('translation_failed'), 'warning');
      } finally {
        translationBusy.value = false;
      }
    }

    // _relTime 已提升到全局作用域（行 41），避免缓存版本 return 里缺失导致模板崩

    function subText(m) {
      const parts = [];
      if (m.author) parts.push(m.author);
      if (m.version) parts.push(fmtVersion(m.version));
      return parts.join(' · ');
    }

    function tooltip(m) {
      const lines = [m.name];
      if (m.author) lines.push(t('author_full') + m.author);
      if (m.version) lines.push(t('version_full') + fmtVersion(m.version));
      // 直接用后端算好的 source_label（综合了 external_source + source），不再前端自己算
      lines.push(t('source_full') + (m.source_label || m.source));
      if (m.dependencies && m.dependencies.length) {
        lines.push(t('deps_label') + m.dependencies.join(', '));
        if (m.missing_deps && m.missing_deps.length) lines.push(t('missing_deps_label') + m.missing_deps.join(', '));
      }
      if (m.has_dep_warning && m.dep_flags && m.dep_flags.length) {
        lines.push('');
        lines.push('⚠ 依赖警告：');
        const flagMap = {
          missing_install: '  • 有前置依赖未安装',
          missing_enable: '  • 有前置依赖未启用',
          order_warn: '  • 依赖顺序可能错误（前置排在子 mod 后面）',
          cycle: '  • 检测到循环依赖',
        };
        m.dep_flags.forEach(f => { if (flagMap[f]) lines.push(flagMap[f]); });
      }
      return lines.join('\n');
    }

    function itemWarnTitle(m) {
      if (!m) return '';
      const redLines = [];
      if (m.dep_flags && m.dep_flags.length) {
        const flagMap = {
          missing_install: '依赖的 mod 未安装',
          missing_enable: '依赖的 mod 未启用',
          order_warn: '依赖顺序可能错误',
          cycle: '检测到循环依赖',
          dup_warn: '同一 mod_id 存在多个物理副本（游戏仅加载活动版本）',
        };
        redLines.push('依赖问题：');
        m.dep_flags.forEach(f => { if (flagMap[f]) redLines.push('  • ' + flagMap[f]); });
      }
      if (m.game_incompat) redLines.push('游戏版本不兼容：此 mod 要求的版本超出当前游戏版本范围');
      if (m.has_dup_copies) redLines.push('同模组有多个副本同时启用（游戏可能只加载其中一个，请确认要保留哪个）');

      const yellowLines = [];
      if (m.is_bundle) {
        yellowLines.push(t('bundle_warn_title', { n: m.bundle_size, names: (m.sibling_names || []).join('、') }));
      } else if (m.has_siblings) {
        yellowLines.push(t('sibling_warn_title', { names: (m.sibling_names || []).join('、') }));
      }

      if (redLines.length) return redLines.join('\n');
      if (yellowLines.length) return yellowLines.join('\n');
      return '';
    }

    function versionLabel(v) {
      return (v.version ? fmtVersion(v.version) : t('no_version')) + ' · ' + sourceText(v.source);
    }

    // === 核心刷新 & 状态同步 ===
    async function refresh(showProgress = false) {
      const keepId = selected.value ? selected.value.id : null;
      const startResp = await api().refresh();
      const alreadyRunning = !startResp.running && startResp.error === 'scan_already_running';
      if (!startResp.running && startResp.ok === false && !alreadyRunning) {
        showToast(t('scan_failed'), 'error');
        return null;
      }

      if (showProgress) {
        scanProgress.show = true;
        scanProgress.stage = '';
        scanProgress.current = '';
        scanProgress.done = 0;
        scanProgress.total = 0;
      }

      const result = await new Promise((resolve) => {
        const started = Date.now();
        const timeout = 300000; // 5 分钟（含首次 Steam 依赖爬取）
        const timer = setInterval(async () => {
          try {
            const p = await api().get_scan_progress();
            if (showProgress) {
              scanProgress.stage = p.stage || '';
              scanProgress.current = p.current || '';
              scanProgress.done = p.done || 0;
              scanProgress.total = p.total || 0;
            }
            if (!p.running) {
              clearInterval(timer);
              resolve(p);
              return;
            }
          } catch (e) {
            clearInterval(timer);
            resolve({ error: String(e) });
            return;
          }
          if (Date.now() - started > timeout) {
            clearInterval(timer);
            resolve({ error: 'timeout' });
          }
        }, 150);
      });

      if (showProgress) scanProgress.show = false;

      if (result.error) {
        if (showProgress) showToast(t('scan_failed') + ': ' + result.error, 'error');
        return null;
      }
      const s = result.result;
      if (!s) return null;

      applyState(s);
      if (keepId && !selected.value) {
        // 优先从展开后的 flat 副本里找（有 __version_key 才能让 selected 判断生效）
        const allFlat = expandModList([...state.enabled, ...state.disabled]);
        selected.value = allFlat.find(m => m.id === keepId)
                      || [...state.enabled, ...state.disabled].find(m => m.id === keepId)
                      || null;
      }
      return s;
    }

    async function scan() {
      const s = await refresh(true);
      if (!s) return;
      loadSkinState();
      loadCardState();
      const r = s.scan_report || {};
      // B2：只列非零项 + 独立"未发现变化"分支（定论方案）
      const n = r.new || 0, u = r.updated || 0, d = r.deleted || 0;
      if (!n && !u && !d) {
        showToast(t('scan.done.no_change'), 'success');
      } else {
        const parts = [];
        if (n) parts.push(t('scan.done.new', { n }));
        if (u) parts.push(t('scan.done.updated', { n: u }));
        if (d) parts.push(t('scan.done.removed', { n: d }));
        showToast(t('scan.done.mixed') + '：' + parts.join('，'), 'success');
      }
    }

    async function analyzeDlls() {
      const r = await api().analyze_dlls();
      const total = r.total || 0;
      const skipped = r.skipped || 0;
      if (skipped) {
        showToast(t('skipped_manual', { n: skipped }), 'warning');
      }
      if (!total) return;
      dllProgress.show = true;
      dllProgress.current = '';
      dllProgress.done = 0;
      dllProgress.total = total;
      await new Promise((resolve) => {
        const started = Date.now();
        const timeout = 60000; // 最长等 60 秒，避免后端卡住时进度条永远不消失
        const timer = setInterval(async () => {
          try {
            const p = await api().get_dll_progress();
            dllProgress.current = p.current || '';
            dllProgress.done = p.done || 0;
            dllProgress.total = p.total || total;
            if (p.total && p.done >= p.total) {
              clearInterval(timer);
              resolve();
              return;
            }
          } catch (e) {
            clearInterval(timer);
            resolve();
            return;
          }
          if (Date.now() - started > timeout) {
            clearInterval(timer);
            resolve();
          }
        }, 120);
      });
      dllProgress.show = false;
    }

    function applyState(s) {
      // 统一条目唯一键：workshop_id + mod_id
      // 不能只按 mod_id 去重——不同 workshop 的 mod 可能共享同一个 manifest mod_id（Watcher bug）
      // 本地 mod（无 workshop_id）退化成空串 + mod_id，等价于旧行为
      const entryKey = (m) => {
        const wid = m && m.workshop_id ? String(m.workshop_id) : '';
        const mid = (m && m.id || '').toLowerCase();
        return wid + '|' + mid;
      };
      // 防御性去重：后端应该保证无重复，但前端加保险
      // 2026-11-05 改：按 entryKey 去重，兼容不同 workshop 同 mod_id 场景
      const _dedup = (arr) => {
        const seen = new Set();
        return (arr || []).filter(m => {
          const k = entryKey(m);
          if (!k || seen.has(k)) return false;
          seen.add(k); return true;
        });
      };
      // 2026-11-08：给每个 mod 挂 has_siblings（同文件夹兄弟）、game_incompat、is_bundle（合集）
      const _tagMods = (arr) => {
        // 找最后一个纯数字段（Workshop ID），parent 截到那级（含）
        const _getParentDir2 = (path) => {
          if (!path) return null;
          const parts = path.replace(/\\/g, '/').split('/').filter(Boolean);
          if (parts.length < 3) return null;
          let lastNumIdx = -1;
          for (let i = 0; i < parts.length; i++) {
            if (/^\d+$/.test(parts[i])) lastNumIdx = i;
          }
          if (lastNumIdx < 0) return null;
          return parts.slice(0, lastNumIdx + 1).join('/');
        };
        const flat = [...(s.enabled || []), ...(s.disabled || [])];
        // 同副本多启用检测：数每个 mod_id 有多少个启用的物理副本
        const dupCountMap = {};
        for (const f of flat) {
          if (!f.is_enabled) continue;
          const mid = (f.id || '').toLowerCase();
          if (!mid) continue;
          dupCountMap[mid] = (dupCountMap[mid] || 0) + 1;
        }
        // 构造 entryKey 过滤（同 entryKey = 同一个物理 mod，不算兄弟）
        const _ek = (m) => {
          const wid = m && m.workshop_id ? String(m.workshop_id) : '';
          const mid = (m && m.id || '').toLowerCase();
          return wid + '|' + mid;
        };
        const sibMap = {};
        for (const m of flat) {
          const p = _getParentDir2(m.path);
          if (!p) continue;
          if (!sibMap[p]) sibMap[p] = [];
          sibMap[p].push(m);
        }
        for (const m of arr) {
          const pp = _getParentDir2(m.path);
          const sibs = pp && sibMap[pp] ? sibMap[pp] : [];
          const myEk = _ek(m);
          // 兄弟：排除自己（用 entryKey 对比，不是 id——不同物理副本可能同 id）
          const realSibs = sibs.filter(x => _ek(x) !== myEk);
          m.has_siblings = realSibs.length >= 1;  // 有至少 1 个真实兄弟就算
          m.sibling_count = realSibs.length;
          m.sibling_names = realSibs.map(x => x.name || x.id || '').filter(Boolean).slice(0, 5);
          m.is_bundle = realSibs.length >= 3;  // ≥4 个物理 mod（自己+3 兄弟）判定为合集
          m.bundle_size = realSibs.length + 1;
          const vc = m.version_compat;
          m.game_incompat = !!(vc && (vc.status === 'needs_update' || vc.status === 'too_new'));
          m.has_dup_copies = !!dupCountMap[(m.id || '').toLowerCase()] && dupCountMap[(m.id || '').toLowerCase()] >= 2;
        }
        return arr;
      };
      // 全局挂载 entryKey，给后续所有 canonical 查找 / 排序用
      window.__entryKey = entryKey;
      state.enabled = _dedup(_tagMods(s.enabled || []));
      state.disabled = _dedup(_tagMods(s.disabled || []));
      state.packs = s.packs || [];
      state.backups = s.backups || [];
      state.backup_stats = s.backup_stats || {};
      state.game_root = s.game_root;
      state.errors = s.errors || [];
      state.steam_id = s.steam_id || null;
      state.subscribed_known = s.subscribed_known !== false;
      state.breakdown = s.mod_count_breakdown || null;

      // Steam 工坊更新检测结果：后端 refresh 里自动触发 check_workshop_updates()
      const wsUpdates = s.workshop_updates;
      if (wsUpdates) {
        workshopUpdates.checked = wsUpdates.checked || 0;
        workshopUpdates.errors = wsUpdates.errors || 0;
        workshopUpdates.updates = wsUpdates.updates || [];
        workshopUpdates.lastChecked = new Date().toLocaleTimeString();
        // 有更新 → 自动弹窗（仅首次，避免反复打扰）
        if (workshopUpdates.updates.length > 0 && !workshopUpdates._dialogShown) {
          workshopUpdates._dialogShown = true;
          nextTick(() => openUpdateDialog());
        } else if (workshopUpdates.updates.length === 0) {
          workshopUpdates._dialogShown = false; // 无更新 → 重置标记（下次有更新还能弹）
        }
      }

      // 同步游戏版本 & Beta 信息（每次 get_state 都会刷新）
      if (s.game_info) {
        Object.assign(gameInfo, s.game_info);
      }

      // 后端返回的 mod 对象没有 dep_flags / has_dep_warning，
      // 必须在这里统一重算一次（含 dup_warn），否则所有依赖警告在后端回灌后丢失
      _localRecheckDeps();
      // 历史状态：同 mod 多副本同时启用只提示，不自动改
      _checkDuplicateEnabledCopies();

      // 选中保留：先按 __version_key（install_dir hash）精确匹配；
      // 找不到再按 id 找；还找不到 → 保留旧 selected（如果它仍在 state 里）。
      // 只有 selected 真被删了（不在新 enabled+disabled 里）才清掉。
      const prevKey = selected.value ? (selected.value.__row_key || selected.value.__version_key) : null;
      const prevId = selected.value ? selected.value.id : null;
      let newSelected = null;
      if (prevKey && prevId) {
        const allFlat = expandModList([...state.enabled, ...state.disabled]);
        newSelected = allFlat.find(x => x.__row_key === prevKey);
      }
      if (!newSelected && prevId) {
        // 也从 flat 副本找（确保有 __version_key）
        newSelected = expandModList([...state.enabled, ...state.disabled]).find(m => m.id === prevId);
      }
      if (!newSelected && prevId) {
        // 最后兜底：旧 selected 还在原始 enabled/disabled 里 → 保留它
        const allRaw = [...state.enabled, ...state.disabled];
        const stillThere = allRaw.some(m => m.id === prevId);
        if (stillThere) newSelected = selected.value;
      }
      selected.value = newSelected || null;

      // 不清空 workshop.data —— 改语言/刷新/重扫时 selected 可能短暂丢失，
      // 但用户还在看预览。留着旧数据比闪白强。
      // 只有用户主动 select(null) 时才由 select() 流程控制。
      nextTick(drawDeps);
      _maybePollTitles(s.titles_pending);
      _maybePollBgProgress();
      _maybePollHtmlTasks();
      updateSnapshot();  // 每次 applyState 后重建快照（含首次加载）
    }

    // ---------------------------------------------------------------- 标题补全轮询
    // 后端会后台补全工坊本地化标题（没打开过预览的 mod 也能显示工坊原名）。
    // 只要后端还在跑或有待补全的就轻量轮询，补全结束后重载一次 state 让列表名刷新。
    let _titlesPollTimer = null;
    function _maybePollTitles(pending) {
      if (!pending || _titlesPollTimer) return;
      let ticks = 0;
      _titlesPollTimer = setInterval(async () => {
        ticks++;
        try {
          const r = await api().get_workshop_titles_pending();
          const left = (r && r.pending) || 0;
          const busy = !!(r && r.running);
          if ((!busy && left === 0) || ticks > 400) {   // 400 次 ≈ 66 分钟（安全兜底）
            clearInterval(_titlesPollTimer);
            _titlesPollTimer = null;
            if (left === 0) applyState(await api().get_state());
          }
        } catch (e) { /* ignore */ }
      }, 10000);
    }

    // ---------------------------------------------------------------- 后台两阶段补全进度轮询
    // refresh() 启动后后台线程并行跑：阶段1 Web API 快速预览图 → 阶段2 慢速 HTML 补中文
    let _bgPollTimer = null;
    function _maybePollBgProgress() {
      if (_bgPollTimer) return;
      let ticks = 0;
      _bgPollTimer = setInterval(async () => {
        ticks++;
        try {
          const r = await api().get_workshop_bg_progress();
          const prevRunning = bgProgress.running;
          bgProgress.phase1 = r.phase1 || bgProgress.phase1;
          bgProgress.phase2 = r.phase2 || bgProgress.phase2;
          bgProgress.running = !!r.running;
          // show 条件：只认正在跑或有进度差（done < total）
          // 之前加了 || p2Initialized 导致 phase2 初始化过一次后永不消失——永久开关 bug
          const p1Active = bgProgress.phase1.total >= 0 && bgProgress.phase1.done < bgProgress.phase1.total;
          const p2Active = bgProgress.phase2.total >= 0 && bgProgress.phase2.done < bgProgress.phase2.total;
          bgProgress.show = bgProgress.running || p1Active || p2Active;
          // 阶段 1 刚完成（从 running 变不 running）→ 重载 state 让预览图立即出来
          if (prevRunning && !bgProgress.running && bgProgress.phase1.total > 0 && bgProgress.phase1.done >= bgProgress.phase1.total) {
            applyState(await api().get_state());
          }
          // show 变 false 或超时 → 停轮询
          if (!bgProgress.show || ticks > 2000) {
            clearInterval(_bgPollTimer);
            _bgPollTimer = null;
          }
        } catch (e) { /* ignore */ }
      }, 5000);
    }
    // 初始启动一次（refresh 时已经有后台线程了）
    _maybePollBgProgress();
    // 每次 applyState 也触发（如果之前 timer 被清了，refresh 完后台又起来了）

    // ---------------------------------------------------------------- 统一 HTML 队列轮询
    // 启用/预览只把任务入队，后端单消费者串行抓取。
    // 这里轻量轮询任务状态，有任务完成时静默拉一次 state，让标题/标签/依赖刷新。
    let _htmlTasksPollTimer = null;
    let _htmlTasksDoneCount = 0;
    function _maybePollHtmlTasks() {
      if (_htmlTasksPollTimer) return;
      let ticks = 0;
      _htmlTasksPollTimer = setInterval(async () => {
        ticks++;
        try {
          const r = await api().get_html_tasks();
          const items = (r && r.items) || {};
          const list = Object.values(items);
          const doneCount = list.filter(x => x && (x.status === 'done' || x.status === 'failed')).length;
          const busy = ((r && r.pending && r.pending.length) || 0) > 0
            || list.some(x => x && x.status === 'running');
          if (doneCount !== _htmlTasksDoneCount) {
            _htmlTasksDoneCount = doneCount;
            applyState(await api().get_state());
          }
          if (!busy || ticks > 2000) {
            clearInterval(_htmlTasksPollTimer);
            _htmlTasksPollTimer = null;
          }
        } catch (e) { /* ignore */ }
      }, 3000);
    }

    // 滚动到指定 flat 行（供保存后聚焦用）
    function scrollToFlatRow(rowKey) {
      if (!rowKey) return;
      // 遍历所有 li 匹配 data-ver-key（避免 CSS.escape 对 Windows 反斜杠问题）
      const allItems = document.querySelectorAll('li.mod-item, li.disabled-item');
      let target = null;
      for (const li of allItems) {
        const vk = li.getAttribute('data-ver-key');
        if (vk === rowKey) { target = li; break; }
      }
      if (!target) return;
      requestAnimationFrame(() => {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    }

    function select(m, side, event) {
      const rk = m.__row_key || m.__version_key;
      // Ctrl/Meta（Mac）点击：toggle 单个
      if (event && (event.ctrlKey || event.metaKey)) {
        if (_multiHas(rk)) {
          _multiDel(rk);
        } else {
          _multiAdd(rk);
        }
        selected.value = m;
        _rangeAnchorKey.value = { row_key: rk, side };
        nextTick(drawDeps);
        loadWorkshop(m);
        return;
      }
      // Shift 点击：范围选择（按当前显示侧做范围，不合并两侧）
      if (event && event.shiftKey && _rangeAnchorKey.value) {
        const anchor = _rangeAnchorKey.value;
        // 锚点和当前侧不同 → 重置锚点为当前行，只选当前行
        if (anchor.side !== side) {
          _multiClear();
          _rangeAnchorKey.value = { row_key: rk, side };
          selected.value = m;
          nextTick(drawDeps);
          loadWorkshop(m);
          return;
        }
        // 同侧：用 displayList 拿当前侧（含搜索过滤）的 flat 行做范围
        const search = side === 'enabled' ? enabledSearch.value : disabledSearch.value;
        const sideFlat = displayList(side === 'enabled' ? filteredEnabled.value : filteredDisabled.value, side, search);
        const anchorIdx = sideFlat.findIndex(x => (x.__row_key || x.__version_key) === anchor.row_key);
        const curIdx = sideFlat.findIndex(x => (x.__row_key || x.__version_key) === rk);
        if (anchorIdx >= 0 && curIdx >= 0) {
          _multiClear();
          const [lo, hi] = anchorIdx < curIdx ? [anchorIdx, curIdx] : [curIdx, anchorIdx];
          for (let i = lo; i <= hi; i++) {
            _multiAdd(sideFlat[i].__row_key || sideFlat[i].__version_key);
          }
        }
        selected.value = m;
        _rangeAnchorKey.value = { row_key: rk, side };
        nextTick(drawDeps);
        loadWorkshop(m);
        return;
      }
      // 普通点击：清空多选 + 设置主选中 + 重置锚点
      _multiClear();
      selected.value = m;
      _rangeAnchorKey.value = { row_key: rk, side };
      nextTick(drawDeps);
      loadWorkshop(m);
    }

    // 复制指定文本（模组名称 / 标签等单击复制）
    async function copyText(text) {
      if (!text) { showToast(t('no_desc_copy'), 'warning'); return; }
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement('textarea');
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        showToast(t('copied'), 'success');
      } catch (e) {
        showToast(t('copy_failed') + (e && e.message ? e.message : e), 'error');
      }
    }

    // 工坊详情：只在来源为创意工坊时异步拉取
    const workshop = reactive({ loading: false, data: null, error: '', html_just_filled: false });
    let workshopSeq = 0;
    // 后台两阶段补全进度（阶段1=快速预览图，阶段2=慢速HTML补中文）
    const bgProgress = reactive({
      phase1: { done: 0, total: 0, running: false, ok: false },
      phase2: { done: 0, total: 0, running: false, ok: false },
      running: false,
      show: false,
    });
    // 正下方任务进度卡片：汇总所有带进度的任务
    const progressCards = computed(() => {
      const cards = [];
      if (backupBusy.show) {
        cards.push({
          id: 'backup', title: backupBusy.text,
          indeterminate: backupBusy.mode === 'indeterminate',
          done: backupBusy.done, total: backupBusy.total,
        });
      }
      if (bulkProgress.show) {
        cards.push({
          id: 'bulk', title: t('bulk_downloading_soft'),
          indeterminate: false, done: bulkProgress.done, total: bulkProgress.total,
          fillClass: 'fill-blue',
        });
      }
      if (dllProgress.show) {
        cards.push({
          id: 'dll',
          title: dllProgress.current ? dllProgress.current + ' ' + t('analyzing') : t('analyzing_dll'),
          indeterminate: false, done: dllProgress.done, total: dllProgress.total,
        });
      }
      if (scanProgress.show) {
        const stageLabel = t({ fetch_deps: 'scan_stage_deps', finalize: 'scan_stage_finalize' }[scanProgress.stage] || 'scanning');
        cards.push({
          id: 'scan',
          title: (stageLabel + ' ' + (scanProgress.current || '')).trim(),
          indeterminate: false, done: scanProgress.done, total: scanProgress.total,
          fillClass: 'fill-green',
        });
      }
      if (bgProgress.show) {
        // phase2（预览图下载）正在跑 → 优先显示；否则显示 phase1（工坊信息获取）
        const p2 = bgProgress.phase2;
        const p1 = bgProgress.phase1;
        const useP2 = (p2.total > 0 && (p2.running || (p2.done || 0) < (p2.total || 0))) ||
                      (p2.running && p2.total < 0);
        const p = useP2 ? p2 : p1;
        // 文案：preparing=total<0，running，done
        const title = p.total < 0
          ? t('bg_fill_preparing')
          : (p.running ? t(useP2 ? 'bg_fill_phase2' : 'bg_fill_phase1') : t('bg_fill_done'));
        cards.push({
          id: 'bg',
          title,
          // indeterminate 只在 total < 0（preparing）时晃；total <= 0 直接隐藏这个卡片
          indeterminate: p.total < 0,
          done: Math.max(0, p.done || 0),
          total: Math.max(0, p.total || 0),
          fillClass: p.total < 0 ? 'fill-purple' : 'fill-green',
        });
      }
      return cards.map(c => {
        const total = Math.max(0, c.total || 0);
        const done = Math.max(0, c.done || 0);
        const indeterminate = c.indeterminate || total <= 0;
        return {
          ...c,
          indeterminate,
          percent: indeterminate ? 0 : Math.min(100, done / total * 100),
          countText: indeterminate ? '' : `${Math.round(done / total * 100)}%`,
        };
      });
    });

    // 正下方只展示优先级最高的一条，其余用右上角角标计数
    const PROGRESS_PRIORITY = { backup: 1, scan: 2, bulk: 3, dll: 4, bg: 5 };
    const currentProgressCard = computed(() => {
      const cards = progressCards.value;
      if (!cards.length) return null;
      return cards.slice().sort(
        (a, b) => (PROGRESS_PRIORITY[a.id] || 99) - (PROGRESS_PRIORITY[b.id] || 99)
      )[0];
    });
    const progressTaskCount = computed(() => progressCards.value.length);

    // 「打开详情」菜单（类似右键菜单）
    const wsMenu = reactive({ show: false, x: 0, y: 0 });

    // === Workshop 工坊详情爬取 ===
    async function loadWorkshop(m) {
      // 防重复：已加载同一个 mod 的工坊数据 → 直接跳过（不触发 loading/清空）
      // 场景：applyState 保留了 selected，旧 workshop.data 还在；或切语言时不该重加载预览
      if (m && m.workshop_id
          && workshop.data && workshop.data.workshop_id === m.workshop_id
          && !workshop.loading) {
        console.warn('[workshop] loadWorkshop SKIP — same wid already loaded:', m.workshop_id);
        return;
      }
      console.warn('[workshop] loadWorkshop TRIGGERED wid=', m && m.workshop_id,
                   'stack:', new Error().stack.split('\n').slice(2, 5).join(' | '));
      workshopSeq += 1;
      const seq = workshopSeq;
      workshop.loading = false;
      workshop.data = null;
      workshop.error = '';
      workshop.html_just_filled = false;
      wsMenu.show = false;
      translationPollToken++;
      translationPolling.value = false;
      if (!m || !m.workshop_id) return;

      // 只有完全无缓存才显示进度条；已有缓存（无论完整/不完整）都秒开
      let cached = true;
      try {
        cached = await api().is_workshop_cached(m.id, m.workshop_id);
      } catch (e) {
        cached = true;
      }
      if (seq !== workshopSeq) { workshop.loading = false; return; }

      workshop.loading = true;
      try {
        const d = await api().get_workshop_details(m.id, m.workshop_id);
        if (seq !== workshopSeq) { workshop.loading = false; return; }
        workshop.data = (d && d.workshop_id && !d.error) ? d : null;
        workshop.error = d && d.error ? d.error : '';
      } catch (e) {
        if (seq !== workshopSeq) { workshop.loading = false; return; }
        workshop.data = null;
        workshop.error = String(e && e.message || e);
      } finally {
        // 无条件关闭 loading —— 不管 seq 对不对，过时请求不能永远卡着 loading
        workshop.loading = false;
        // 后台已启动 HTML 补中文 desc → 前端 5s 轮询自动刷新（desc_src 变成 "html" 时停）
        if (seq === workshopSeq && workshop.data && workshop.data.workshop_id
            && !workshop.data.html_checked
            && workshop.data.desc_src !== 'html') {
          _pollHtmlFill(m, seq);
        }
      }
    }

    // 用户点选 → 后端统一 HTML 队列补中文 desc → 轮询任务状态，完成后再拉一次详情
    function _pollHtmlFill(m, seq) {
      let ticks = 0;
      let busy = false;
      let shownFetching = false;  // 开始提示只弹一次（带倒计时进度条），后续 tick 不重置
      const modName = (m && (m.name || m.title)) || '';
      // toast key 必须在轮询开始时固定：用户切换选中后 workshop.data 已换成新 mod，
      // 动态取 wid 会 dismiss 错目标，旧 sticky Toast 就一直留在右上角不消失。
      const pollWid = (m && m.workshop_id) || (workshop.data && workshop.data.workshop_id) || '';
      const tk = pollWid ? 'html:' + pollWid : '';
      const token = ++translationPollToken;
      translationPolling.value = true;
      const stop = (keepToast) => {
        if (token === translationPollToken) translationPolling.value = false;
        if (tk && !keepToast) dismissToastKey(tk);
        clearInterval(timer);
      };
      const tick = async () => {
        if (busy) return;
        busy = true;
        try {
          ticks++;
          if (seq !== workshopSeq) { stop(); return; }
          if (ticks > 60) { stop(); return; }  // 5 分钟兜底
          // 先看统一队列里该 wid 的状态，避免每 5s 重复拉完整详情
          const tasks = await api().get_html_tasks();
          if (seq !== workshopSeq) { stop(); return; }
          // 熔断感知：后端 HTML 熔断冷却中 → 直接停，不要永远 poll
          if (tasks && tasks.circuit_open) {
            console.warn('[pollHtmlFill] HTML 熔断开启，停止轮询并 dismiss toast');
            stop();
            return;
          }
          const wid = workshop.data && workshop.data.workshop_id;
          const st = wid && tasks && tasks.items ? tasks.items[wid] : null;
          if (st && (st.status === 'queued' || st.status === 'running')) {
            if (tk && !shownFetching) {
              shownFetching = true;
              // 抓取开始提示：3s 自动消失（走 TOAST_DURATION.info 默认值）
              showToast(t('preview_fetching', { name: modName }), 'info', { key: tk });
            }
            return;
          }
          if (st && st.status === 'failed') {
            if (tk) showToast(t('preview_fetch_failed', { name: modName }), 'warning', { key: tk });
            stop(true);  // 保留结果提示，让它按 4s 自动消失
            return;
          }

          const d = await api().get_workshop_details(m.id, m.workshop_id);
          if (seq !== workshopSeq) { stop(); return; }
          if (d && d.html_desc_state === 'updated') {
            // 后台 HTML 补完 desc！刷新前端显示
            workshop.data = d;
            workshop.error = '';
            workshop.html_just_filled = true;
            if (tk) showToast(t('preview_fetched', { name: modName }), 'success', { key: tk });
            stop(true);  // 保留结果提示，让它按 3s 自动消失
            setTimeout(() => {
              if (seq === workshopSeq) workshop.html_just_filled = false;
            }, 3000);
          } else if (d && d.html_checked) {
            // 抓过了：内容一致或无中文，都只提示一次，按钮按版本检查状态收起
            workshop.data = d;
            if (tk) {
              const key = d.html_desc_state === 'same'
                ? 'preview_same_translation' : 'preview_no_translation';
              showToast(t(key, { name: modName }), 'info', { key: tk });
            }
            stop(true);  // 保留结果提示，让它按 3s 自动消失
          } else if (!st) {
            // 未入队且未检查过（例如开关关闭/退避中）→ 停止轮询
            stop();
          } else {
            // st 存在但既没 updated 也没 html_checked（理论上不该发生）→ 停止
            stop();
          }
        } catch (e) { /* 网络波动，下次再试 */ }
        finally { busy = false; }
      };
      const timer = setInterval(tick, 5000);
      tick();
    }

    function openWsMenu(e) {
      // 菜单显示在按钮正上方，避免超出窗口底部
      const rect = e.currentTarget.getBoundingClientRect();
      wsMenu.x = rect.left;
      wsMenu.y = rect.top - 4;
      wsMenu.show = true;
    }
    function closeWsMenu() { wsMenu.show = false; }
    async function openWorkshop(mode) {
      wsMenu.show = false;
      if (!selected.value) return;
      await api().open_workshop(selected.value.id, mode, selected.value.workshop_id);
    }

    /** 双击路由：所有副本统一启/停（活动/非活动副本选择逻辑已于 2026-10 删除） */
    function dblClickHandler(m) {
      if (!m || m.__ph) return;
      // workshop 副本可以精确启停（改 save 文件不动 manifest），不拦截
      toggle(m);  // 所有副本直接启停，按 source 粒度独立
    }

    // selectVersion 已于 2026-10 删除（活动/非活动副本选择逻辑废弃）
    // 多副本改由 source_states 多 source 独立启停；双击任何副本就是启停它自己

    async function toggle(m) {
      // ====== 2026-11-06 架构重构：纯前端改 state，0 延迟 ======
      const enable = !m.is_enabled;
      try {
        _toggleFrontend(m, enable);
        _localRecheckDeps();  // 操作后重算依赖警告
        _multiClear();
        // 选中保持当前点击的副本
        const flatAfter = expandModList([...state.enabled, ...state.disabled]);
        const findM = flatAfter.find(x => x.__row_key === m.__row_key);
        if (findM) selected.value = findM;
        nextTick(drawDeps);
        loadWorkshop(m);
      } catch (e) {
        console.error('[toggle] 前端操作失败:', e);
      }
    }

    /** 纯前端启停副本（延迟保存架构核心）。
     *
     * state.enabled/disabled 存 canonical 对象（带 all_versions 副本数组）。
     * displayList 合并两侧 → 展开 all_versions → 按 flat 副本的 is_enabled 过滤分侧。
     * 所以正确做法是改 canonical.all_versions 里对应副本的 is_enabled，
     * 然后维护 canonical 在 enabled/disabled 数组的归属（影响启用侧排序）。
     */
    // 历史状态提示去重键：同 mod 多副本同时启用的组合不变时不重复弹
    let _dupCopyWarnKey = '';

    /** 同 mod 副本检测：启用侧是否已有同一 mod_id 的其它副本（含工坊/本地副本） */
    function _findSameModEnabled(flatM, vk) {
      const mid = (flatM.id || '').toLowerCase();
      if (!mid) return null;
      const selfKey = flatM.__row_key || (mid + '@' + vk);
      const allFlat = expandModList([...state.enabled, ...state.disabled]);
      return allFlat.find(f =>
        !!f.is_enabled &&
        f.__row_key !== selfKey &&
        (f.id || '').toLowerCase() === mid
      ) || null;
    }

    /** refresh 后检测：已有多个同 mod_id 副本同时启用（只提示，不改状态） */
    function _checkDuplicateEnabledCopies() {
      const allFlat = expandModList([...state.enabled, ...state.disabled]);
      const countById = new Map();
      for (const f of allFlat) {
        if (!f.is_enabled) continue;
        const mid = (f.id || '').toLowerCase();
        if (!mid) continue;
        countById.set(mid, (countById.get(mid) || 0) + 1);
      }
      const dups = [...countById.entries()].filter(([, n]) => n > 1);
      const sig = dups.map(([mid, n]) => mid + ':' + n).sort().join('|');
      if (sig && sig !== _dupCopyWarnKey) {
        showToast(t('duplicate_copies_enabled_warning', { n: dups.length }), 'warning');
      }
      _dupCopyWarnKey = sig;
    }

    function _toggleFrontend(flatM, enable, opts) {
      opts = opts || {};
      const wasEnabled = !!flatM.is_enabled;
      const _thisRoundKeys = new Set();
      const vk = flatM.__version_key;
      const mid = flatM.id;

      // 1. 找到 canonical（可能在 enabled 或 disabled）
      // canonical 级别一个 mod_id 一条，用纯 mod_id 匹配即可；
      // 再靠 vIdx 精确定位 all_versions 里的目标副本
      const midLow = (mid || '').toLowerCase();
      let arr = state.enabled;
      let sideIdx = arr.findIndex(c => (c.id || '').toLowerCase() === midLow);
      if (sideIdx < 0) {
        arr = state.disabled;
        sideIdx = arr.findIndex(c => (c.id || '').toLowerCase() === midLow);
      }
      if (sideIdx < 0) { console.warn('[toggleFrontend] canonical not found id=' + mid); return; }
      const canonical = arr[sideIdx];

      // 2. 改 all_versions 里对应副本的 is_enabled
      const vIdx = canonical.all_versions.findIndex(v => v.key === vk);
      if (vIdx < 0) {
        console.warn('[toggleFrontend] version not found vk=' + vk);
        console.warn('[toggleFrontend] available keys:', canonical.all_versions.map(v => v.key && v.key.slice ? v.key.slice(-40) : v.key));
        return;
      }
      // 同 mod 副本检测已删除（2026-11-09 需求反转：副本是独立模组，允许同时启用）
      // 警告检测保留在 applyState 后的 _checkDuplicateEnabledCopies() 里
      canonical.all_versions[vIdx].is_enabled = enable;
      // 2026-11-07 Step 1: 同步写 enabled_at / disabled_at（前端本地显示用）
      // 2026-11-07 Step 2 清理：batch_seq 彻底删，换后端微秒差
      const _nowTs = Date.now() / 1000;
      canonical.all_versions[vIdx].last_toggled_at = _nowTs;
      if (enable) {
        canonical.all_versions[vIdx].enabled_at = _nowTs;
      } else {
        canonical.all_versions[vIdx].disabled_at = _nowTs;
      }
      // 2026-11-07 Step 3: 前端脏数据标记（漏洞 3: workshop_id 统一转 null）
      const _wid = flatM.workshop_id || null;
      const _k1 = JSON.stringify([midLow, _wid, vk]);
      touchedKeys.add(_k1); _thisRoundKeys.add(_k1);

      // 互斥逻辑已删除（2026-11-09 需求反转：local/disabled 副本启用不再被动停其他副本）

      // 4. 维护 canonical 在 state.enabled / disabled 的归属
      const hasAnyEnabled = canonical.all_versions.some(v => v.is_enabled);
      const isInEnabled = arr === state.enabled;
      if (hasAnyEnabled && !isInEnabled) {
        // 从 disabled 移到 enabled —— 自动排序（按依赖链）
        arr.splice(sideIdx, 1);
        if (opts.insertAt !== undefined) {
          state.enabled.splice(Math.min(opts.insertAt, state.enabled.length), 0, canonical);
        } else {
          // 插入式排序：让 canonical 插到正确的依赖链位置
          // _localInsertNewlyEnabled 返回一个新数组，需整体替换 state.enabled 保持响应式
          const sorted = _localInsertNewlyEnabled(canonical, state.enabled);
          state.enabled.splice(0, state.enabled.length, ...sorted);
        }
      } else if (!hasAnyEnabled && isInEnabled) {
        arr.splice(sideIdx, 1);
        state.disabled.push(canonical);  // 追加到末尾（不破坏 disabled_order 手动顺序）
      } else {
      }
      checkDirtyStatus();
      // 工坊 mod 前端启用即拉依赖（不等扫描/保存），用户视角“启用就算成功”
      if (enable && flatM.workshop_id && !(canonical.dependencies || []).length) {
        _fetchWorkshopDeps(mid);
      }
      // 同步后端内存状态（只改内存不写盘，微秒级，fire-and-forget）
      try { api().apply_frontend_delta(
        state.enabled.map(c => c.id),
        state.disabled.map(c => c.id),
        { version_key: flatM.__version_key, mod_id: flatM.id,
          workshop_id: flatM.workshop_id || null, is_enabled: !!enable }
      ); } catch(e) { console.warn('[toggleFrontend] apply_frontend_delta 失败', e); }
      return true;
    }

    /** 乐观版插入式排序：新启用 mod 插到依赖链正确位置 */
    function _localInsertNewlyEnabled(newMod, enabledArr) {
      const newId = newMod.id.toLowerCase();
      const arr = [...enabledArr];
      const ids = arr.map(x => x.id.toLowerCase());

      // 找所有依赖此 mod 的子 mod 在 arr 里的最小位置
      let minChildIdx = null;
      for (let i = 0; i < arr.length; i++) {
        const deps = (arr[i].dependencies || []).map(d => typeof d === 'string' ? d.toLowerCase() : (d.id || '').toLowerCase());
        if (deps.includes(newId)) { minChildIdx = i; break; }
      }
      // 找此 mod 依赖的前置在 arr 里的最大位置
      let maxParentIdx = -1;
      const myDeps = (newMod.dependencies || []).map(d => typeof d === 'string' ? d.toLowerCase() : (d.id || '').toLowerCase());
      for (let i = 0; i < arr.length; i++) {
        if (myDeps.includes(arr[i].id.toLowerCase())) maxParentIdx = i;
      }
      let insertAt = arr.length;
      if (minChildIdx !== null && maxParentIdx >= 0) insertAt = Math.max(minChildIdx, maxParentIdx + 1);
      else if (minChildIdx !== null) insertAt = minChildIdx;
      else if (maxParentIdx >= 0) insertAt = maxParentIdx + 1;
      arr.splice(insertAt, 0, newMod);
      return arr;
    }

    /** 前端启用工坊 mod 时立即拉依赖：只回填该 mod 的 dependencies，不整包 applyState（避免覆盖并发 toggle） */
    const _depsFetching = new Set();
    const _wsDepsCache = new Map();  // 工坊 API 拉取的依赖缓存（modId → deps[]），applyState 后回填用
    function _fetchWorkshopDeps(modId) {
      const key = String(modId || '').toLowerCase();
      if (!key || _depsFetching.has(key)) return;
      _depsFetching.add(key);
      try {
        Promise.resolve(api().fetch_mod_deps(modId)).then(res => {
          _depsFetching.delete(key);
          if (!res || !res.ok || !Array.isArray(res.deps) || res.deps.length === 0) return;
          const target = [...state.enabled, ...state.disabled]
            .find(m => (m.id || '').toLowerCase() === key);
          if (!target) return;
          target.dependencies = res.deps;
          _localRecheckDeps();
        }).catch(e => { _depsFetching.delete(key); console.debug('[deps] 启用拉取失败', e); });
      } catch (e) { _depsFetching.delete(key); console.debug('[deps] 调用失败', e); }
    }

    /** 乐观版依赖检查：重算所有 mod（启用 + 停用）的 has_dep_warning / dep_flags / missing_deps
     *  启用列表会检查 missing_install + missing_enable + order_warn + dup_warn
     *  停用列表只检查 missing_install / missing_enable（order_warn 和 dup_warn 没意义）
     *  任何列表变了（toggle / 拖拽 / loadPack）都要全量重算，保证实时 */
    function _localRecheckDeps() {
      const allMods = [...state.enabled, ...state.disabled];
      const allIds = new Set(allMods.map(m => (m.id || '').toLowerCase()));
      const enabledIds = new Set(state.enabled.map(m => (m.id || '').toLowerCase()));
      const enabledIdx = {};
      state.enabled.forEach((m, i) => { enabledIdx[(m.id || '').toLowerCase()] = i; });

      // 双向引用豁免：工坊 mod 常把联动 mod 互相写进 Required Items。A 依赖 B 且 B 依赖 A 时，
      // 任何排序都必有一条被判倒序，属于必然误报 → 双向边不参与顺序检查（与后端同规则）。
      const enabledDepEdges = new Set();
      state.enabled.forEach(m => {
        const from = (m.id || '').toLowerCase();
        (m.dependencies || []).forEach(depRaw => {
          const depId = String(depRaw).toLowerCase();
          if (!depId) return;
          for (const e of enabledIds) {
            if (e === depId || e.startsWith(depId + '#') || depId.startsWith(e + '#')) {
              if (e !== from) enabledDepEdges.add(from + '>' + e);
              return;
            }
          }
        });
      });

      allMods.forEach(m => {
        const flags = [];
        const missingDeps = [];
        // 预览面板直接渲染 dep_statuses，后端只在刷新时才重算 → 前端启停后必须同步重算，
        // 否则前置已启用但预览仍显示「未启用」。
        const depStatuses = [];
        const myId = (m.id || '').toLowerCase();
        const myIdx = enabledIdx[myId];  // 停用 mod 是 undefined
        const deps = m.dependencies || [];
        const isEnabled = myIdx !== undefined;

        // 停用 mod 也要算 depStatuses（预览固定显示依赖），
        // 但 has_dep_warning/dep_flags/missing_deps 只对启用 mod 有意义 → 给 false/空
        if (!isEnabled) {
          // 源头清理：停用侧所有警告标志强制清空（这些只对启用侧有意义）
          m.has_dep_warning = false;
          m.dep_flags = [];
          m.missing_deps = [];
          m.has_dup_copies = false;
          m.game_incompat = false;
          m.has_siblings = false;
          return;  // forEach 里用 return 跳过当前迭代
        }

        for (const depRaw of deps) {
          const depId = String(depRaw).toLowerCase();
          if (!depId) continue;
          let matchedAll = null;
          for (const a of allIds) { if (a === depId || a.startsWith(depId + '#') || depId.startsWith(a + '#')) { matchedAll = a; break; } }
          let matchedEnabled = null;
          for (const e of enabledIds) { if (e === depId || e.startsWith(depId + '#') || depId.startsWith(e + '#')) { matchedEnabled = e; break; } }

          if (matchedAll === null) {
            if (isEnabled) { flags.push('missing_install'); missingDeps.push(depId); }
            depStatuses.push({ id: String(depRaw), status: 'missing_install' });
          } else if (matchedEnabled === null) {
            if (isEnabled) { flags.push('missing_enable'); missingDeps.push(matchedAll); }
            depStatuses.push({ id: String(depRaw), status: 'missing_enable' });
          } else {
            depStatuses.push({ id: String(depRaw), status: 'ok' });
            // 顺序检查只对启用 mod 有意义；反向也依赖我 = 双向引用，不算顺序错误
            if (isEnabled) {
              const depIdx = enabledIdx[matchedEnabled];
              if (depIdx !== undefined && depIdx > myIdx && !enabledDepEdges.has(matchedEnabled + '>' + myId)) {
                flags.push('order_warn');
              }
            }
          }
        }

        // 相同模组（多物理副本）警告：只对启用列表有意义
        // 停用 mod 不加载，多副本不存在冲突 → 停用侧永远不打 dup_warn
        // 启用侧也得真的 ≥2 份启用态副本才打（只有 1 份启用 + 其它停用 = 不冲突）
        if (isEnabled && Array.isArray(m.all_versions)) {
          const enabledCount = m.all_versions.filter(v => v.is_enabled).length;
          if (enabledCount >= 2) {
            flags.push('dup_warn');
          }
        }

        // dup_warn 是「提醒」而非「依赖错误」：不计入 has_dep_warning（不触发红色警告样式 / ⚠ 徽章），
        // 仅保留在 dep_flags 里供黄色提醒渲染（.dup-warn 边框 / dup-warn-panel）
        if (isEnabled) {
          m.has_dep_warning = flags.some(f => f !== 'dup_warn');
          m.dep_flags = flags;
          m.missing_deps = missingDeps;
        }
        // dep_statuses 每次都写（预览固定显示依赖状态）
        if (Array.isArray(m.dep_statuses)) {
          m.dep_statuses.splice(0, m.dep_statuses.length, ...depStatuses);
        } else {
          m.dep_statuses = depStatuses;
        }
      });
    }

    // P0: Alias / Notes / Color 编辑（直接改后端，然后手动更新 selected + 列表里对应的 mod）
    async function setAlias(e) {
      if (!selected.value) return;
      const alias = e.target.value.trim();
      const note = (selected.value.mod_meta && selected.value.mod_meta.note) || '';
      const r = await api().set_mod_meta(selected.value.id, alias, note);
      if (r.ok) {
        selected.value.mod_meta = r.meta;
        _syncSelectedToList(selected.value.id);
      }
    }
    async function setNote(e) {
      if (!selected.value) return;
      const note = e.target.value;
      const alias = (selected.value.mod_meta && selected.value.mod_meta.alias) || '';
      const r = await api().set_mod_meta(selected.value.id, alias, note);
      if (r.ok) {
        selected.value.mod_meta = r.meta;
        _syncSelectedToList(selected.value.id);
      }
    }
    async function setColor(e) {
      if (!selected.value) return;
      const color = e === null ? '' : e.target.value;
      const r = await api().set_mod_color(selected.value.id, color);
      if (r.ok) {
        selected.value.mod_color = r.color;
        _syncSelectedToList(selected.value.id);
      }
    }
    function _syncSelectedToList(id) {
      // 把 selected 的新字段同步到 enabled/disabled 列表里对应的 mod 对象（Vue 响应式）
      if (!selected.value) return;
      const src = selected.value;
      for (const list of [state.enabled, state.disabled]) {
        const m = list.find(x => x.id === id);
        if (m) {
          m.mod_meta = src.mod_meta;
          m.mod_color = src.mod_color;
          break;
        }
      }
    }

    // P1: Pinned Mod 锁定/解锁
    async function togglePin(action) {
      if (!selected.value) return;
      if (action === 'unpin') {
        const r = await api().unpin_mod(selected.value.id);
        if (r.ok) { selected.value.pinned = null; _syncSelectedToList(selected.value.id); showToast('已解锁', 'success'); }
        return;
      }
      if (action === 'version') {
        if (selected.value.pinned) {
          // 已锁 → 解锁
          const r = await api().unpin_mod(selected.value.id);
          if (r.ok) { selected.value.pinned = null; _syncSelectedToList(selected.value.id); showToast('已解锁', 'success'); }
        } else {
          // 未锁 → 锁当前版本
          const r = await api().pin_mod(selected.value.id, true, false);
          if (r.ok) { selected.value.pinned = r.pinned; _syncSelectedToList(selected.value.id); showToast('已锁定版本', 'success'); }
        }
      }
    }

    // P0: 导出 CSV/Markdown → 浏览器下载
    async function doExport(format) {
      const r = await api().export_profile(format);
      if (!r.ok) { showToast('导出失败', 'error'); return; }
      const mime = format === 'csv' ? 'text/csv' : 'text/markdown';
      const blob = new Blob([r.data], { type: mime + ';charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = r.filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast(`已导出 ${r.filename}`, 'success');
    }

    async function autoSort() {
      const s = await api().auto_sort();
      applyState(s);
      showToast(t('sort_done'), 'success');
    }

    async function save() {
      // ====== 延迟保存架构：commit_changes 原子化提交 ======
      try {
        const enabledIds = state.enabled.map(m => m.id);
        const disabledIds = state.disabled.map(m => m.id);
        // 副本级明细：同 mod_id 多个工坊/本地副本时，canonical 无法表达
        // 「只启用其中一个副本」，必须把每个副本的 is_enabled 一起提交
        const copyStates = [];
        for (const side of ['enabled', 'disabled']) {
          for (const m of state[side]) {
            if (m.__ph || !Array.isArray(m.all_versions)) continue;
            for (const v of m.all_versions) {
              // 只提交显式设置过 is_enabled 的副本；undefined 跳过
              // 绝对不能用 side fallback → 跨源副本会被错误标记（Baselib 联动根因）
              if (v.is_enabled === undefined || v.is_enabled === null) continue;
              copyStates.push({
                version_key: v.key,
                mod_id: m.id,
                workshop_id: v.workshop_id || null,
                enabled: !!v.is_enabled,
              });
            }
          }
        }
        // 2026-11-07 Step 3: 反解 touchedKeys → just_touched_list
        // 按 disabledIds 顺序排序（微秒差倒序用：越前的 touched 副本微秒差越大 → 排越上）
        const touchedArr = [];
        for (const jsonKey of touchedKeys) {
          try {
            const triple = JSON.parse(jsonKey);
            touchedArr.push({
              mod_id: triple[0],       // lower
              workshop_id: triple[1], // null 或 wid 字符串
              version_key: triple[2],
            });
          } catch (e) { /* 忽略格式错的 */ }
        }
        // 排序：按 disabledIds 顺序（没在 disabled 的排最后）
        const _disIdx = Object.fromEntries(disabledIds.map((id, i) => [id.toLowerCase(), i]));
        const _enIdx = Object.fromEntries(enabledIds.map((id, i) => [id.toLowerCase(), i]));
        touchedArr.sort((a, b) => {
          const ai = a.mod_id in _disIdx ? _disIdx[a.mod_id] : 99999 + (a.mod_id in _enIdx ? _enIdx[a.mod_id] : 199999);
          const bi = b.mod_id in _disIdx ? _disIdx[b.mod_id] : 99999 + (b.mod_id in _enIdx ? _enIdx[b.mod_id] : 199999);
          return ai - bi;
        });

        const r = await api().commit_changes(enabledIds, disabledIds, copyStates, touchedArr);
        if (r && r.ok) {
          clearDirty();
          // no_op：没有实际物理操作（什么都没改）
          if (r.no_op) {
            showToast('保存成功（无变更）', 'info');
            return;
          }
          // 后端物理层可能扫到了不同结构（搬目录后 install_dir 变了），re-sync 一次
          const s = await api().get_state();
          applyState(s);

          const st = r.stats || {};
          const totalSucc = (st.enabled_success || 0) + (st.disabled_success || 0);
          const totalFail = (st.enabled_fail || 0) + (st.disabled_fail || 0);

          // Toast：统一简化文案，不弹具体模组名（用户不想看细节）
          // 有任何类型的警告（同副本/缺依赖/版本/同文件夹）或保存有失败 → 统一提示
          // hasWarnings 只扫启用侧 —— 停用侧警告已在 _localRecheckDeps 源头清空，
          // 加 m.is_enabled 双重保险（防止未来有人加新警告标志忘了源头清）
          const hasWarnings = _cachedAllFlat.value.some(m => m.is_enabled &&
            (m.has_dep_warning || m.game_incompat || m.has_siblings || m.has_dup_copies));

          if (totalFail === 0 && !hasWarnings) {
            showToast('保存成功', 'success');
          } else {
            let msg = '保存成功';
            if (totalFail > 0) msg += `，部分模组未生效（可能文件被占用）`;
            if (hasWarnings) msg += '，已定位相关模组';
            showToast(msg, totalFail > 0 ? 'warning' : 'success');
          }

          // 聚焦：从 flat 行实际展示顺序（从上到下）找第一个有问题的
          setTimeout(() => {
            // 先从保存失败的模组里找（用户最关心）
            let targetRow = null;
            if (totalFail > 0) {
              const firstFail = (st.fail_details || [])[0];
              if (firstFail) {
                targetRow = _cachedAllFlat.value.find(m =>
                  (m.id || '').toLowerCase() === (firstFail.mod || '').toLowerCase());
              }
            }
            // 没有失败 → 找第一个有警告的（只扫启用侧！停用侧的 has_dup_copies / game_incompat 没被清）
            if (!targetRow) {
              targetRow = _cachedAllFlat.value.find(m => m.is_enabled &&
                (m.has_dep_warning || m.game_incompat || m.has_siblings || m.has_dup_copies));
            }
            if (targetRow) {
              select(targetRow);
              // scrollToFlatRow 匹配的是 li 上的 data-ver-key（= __version_key），
              // 不能传 __row_key（= modid@version_key）——格式不匹配永远找不到
              const rk = targetRow.__version_key || targetRow.__row_key;
              scrollToFlatRow(rk);
            }
          }, 400);
        } else {
          // ok=false：后端返回了 errors（全失败，所有 attempted 操作都失败）
          // 检查是不是 WinError 32 / Permission denied（文件被游戏占用）
          const errs = (r && r.errors) || [];
          const isFileLock = errs.some(e =>
            (e.error || '').includes('32') ||
            (e.error || '').includes('WinError') ||
            (e.error || '').toLowerCase().includes('permission') ||
            (e.error || '').includes('文件被占用'));
          if (isFileLock) {
            showToast('保存失败：文件被占用，请关闭游戏后重试', 'error');
          } else {
            showToast('保存失败，请重试', 'error');
          }
        }
      } catch (e) {
        showToast('保存异常: ' + (e.message || e), 'error');
      }
    }

    function openPackMenu(e, pack) {
      packMenu.pack = pack;
      packMenu.x = e.clientX;
      packMenu.y = e.clientY;
      packMenu.show = true;
      nextTick(flipPackMenu);
    }

    function viewportSize() {
      const vv = window.visualViewport;
      if (vv && vv.width && vv.height) return { vw: vv.width, vh: vv.height };
      return { vw: window.innerWidth, vh: window.innerHeight };
    }

    function flipPackMenu() {
      const el = packMenuEl.value;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const { vw, vh } = viewportSize();
      const margin = 8;
      let dx = 0;
      let dy = 0;
      if (rect.right > vw - margin) dx = (vw - margin) - rect.right;
      if (rect.bottom > vh - margin) dy = (vh - margin) - rect.bottom;
      if (rect.left < margin) dx = margin - rect.left;
      if (rect.top < margin) dy = margin - rect.top;
      if (dx) packMenu.x += dx;
      if (dy) packMenu.y += dy;
    }

    function closePackMenu() { packMenu.show = false; }

    async function sharePack() {
      const pack = packMenu.pack;
      packMenu.show = false;
      if (!pack) return;
      // 识别码直接用整合包 id（可用于识别/导入）
      await navigator.clipboard.writeText(pack.id).catch(() => {});
      alert(t('share_code', { name: pack.name, id: pack.id }));
    }

    async function renamePack() {
      const pack = packMenu.pack;
      packMenu.show = false;
      if (!pack) return;
      const newName = prompt(t('prompt_rename'), pack.name);
      if (newName == null || !newName.trim()) return;
      const s = await api().rename_pack(pack.id, newName.trim());
      applyState(s);
    }

    async function deletePack() {
      const pack = packMenu.pack;
      packMenu.show = false;
      if (!pack) return;
      if (!confirm(t('confirm_delete_pack', { name: pack.name }))) return;
      const s = await api().delete_pack(pack.id);
      applyState(s);
    }

    async function loadPack(pack) {
      if (!pack) return;
      // D3: confirm 里展示整合包内容（前 5 个 mod 名 + 总数）
      let modsHint = '';
      const modIds = pack.mods || [];
      if (modIds && modIds.length) {
        const allMods = [...state.enabled, ...state.disabled];
        const preview = modIds.slice(0, 5).map(id => {
          const found = allMods.find(m => m.id.toLowerCase() === id.toLowerCase());
          return found ? found.name : id;
        }).join('、');
        modsHint = `\n\n包含模组：${preview}${modIds.length > 5 ? ` 等 ${modIds.length} 个` : ''}`;
      }
      if (!confirm(t('confirm_load_pack', { name: pack.name }) + modsHint)) return;
      const s = await api().load_pack(pack.id);
      if (s && s.ok) {
        applyState(s);
        showToast(t('loaded_pack', { name: pack.name }), 'success');
      } else if (s && s.error) {
        alert(t('load_failed') + s.error);
      }
    }

    // ======================= mod 备份 =======================
    function fmtBytes(n) {
      n = Number(n) || 0;
      if (n < 1024) return n + ' B';
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
      if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
      return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    }

    // 备份来源徽章的 CSS 类（external_source 优先；停用态按实际来源显示颜色）
    function backupSrcClass(b) {
      if (!b) return 'workshop';
      if (b.external_source) return b.external_source;
      if (b.source === 'disabled') return 'local';
      return b.source || 'workshop';
    }

    function openBackupMenu(e, b) {
      if (!b) return;
      backupMenu.backup = b;
      backupMenu.x = e.clientX;
      backupMenu.y = e.clientY;
      backupMenu.show = true;
      nextTick(flipBackupMenu);
    }
    function flipBackupMenu() {
      const el = backupMenuEl.value;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const { vw, vh } = viewportSize();
      const margin = 8;
      let dx = 0, dy = 0;
      if (rect.right > vw - margin) dx = (vw - margin) - rect.right;
      if (rect.bottom > vh - margin) dy = (vh - margin) - rect.bottom;
      if (rect.left < margin) dx = margin - rect.left;
      if (rect.top < margin) dy = margin - rect.top;
      if (dx) backupMenu.x += dx;
      if (dy) backupMenu.y += dy;
    }
    function closeBackupMenu() { backupMenu.show = false; }

    // 全量备份期间轮询后端进度（确定进度：processed/total）
    function startBackupProgressPolling() {
      let stopped = false;
      const tick = async () => {
        if (stopped) return;
        try {
          const p = await api().get_backup_progress();
          if (!p || !p.total) return;
          backupBusy.total = p.total;
          backupBusy.done = (p.processed != null) ? p.processed : ((p.done || 0) + (p.failed || 0));
          backupBusy.mode = 'determinate';
        } catch (_) {}
      };
      tick();
      const timer = setInterval(tick, 200);
      return () => {
        stopped = true;
        clearInterval(timer);
      };
    }

    async function backupMod(m) {
      if (!m) return;
      menu.show = false;
      backupBusy.show = true;
      backupBusy.mode = 'indeterminate';
      backupBusy.done = 0;
      backupBusy.total = 0;
      backupBusy.text = t('backing_up') + (m.name || '');
      try {
        const s = await api().backup_mod(m.id);
        if (s && s.ok) showToast(t('backup_done', { name: m.name }), 'success');
        else alert(t('backup_failed') + (s && s.error ? s.error : ''));
        if (s && s.backups) { state.backups = s.backups; state.backup_stats = s.stats || state.backup_stats; }
      } catch (e) {
        alert(t('backup_failed') + (e && e.message ? e.message : e));
      } finally {
        backupBusy.show = false;
      }
    }

    async function backupAll(scope) {
      if (scope === 'all' && !confirm(t('confirm_backup_all', { n: state.enabled.length + state.disabled.length }))) return;
      backupBusy.show = true;
      backupBusy.mode = 'indeterminate';
      backupBusy.done = 0;
      backupBusy.total = 0;
      backupBusy.text = scope === 'enabled' ? t('backing_up_enabled') : t('backing_up_all');
      const stopPolling = startBackupProgressPolling();
      try {
        const s = await api().backup_all(scope || 'all');
        if (s && s.backups) { state.backups = s.backups; state.backup_stats = s.stats || state.backup_stats; }
        if (s && s.ok) {
          showToast(
            t('backup_all_done', { done: s.done, failed: s.failed }),
            (s.failed || 0) > 0 ? 'warning' : 'success'
          );
        }
      } catch (e) {
        alert(t('backup_failed') + (e && e.message ? e.message : e));
      } finally {
        stopPolling();
        backupBusy.show = false;
        backupBusy.mode = 'indeterminate';
        backupBusy.done = 0;
        backupBusy.total = 0;
      }
    }

    function openRestore(b) {
      if (!b) return;
      closeBackupMenu();
      if (!b.exists) { alert(t('backup_archive_missing')); return; }
      restoreDlg.backup = b;
      restoreDlg.show = true;
    }
    function closeRestore() {
      restoreDlg.show = false;
      restoreDlg.backup = null;
      restoreDlg.busy = false;
    }

    async function confirmRestore(target) {
      const b = restoreDlg.backup;
      if (!b || restoreDlg.busy) return;
      restoreDlg.busy = true;
      backupBusy.show = true;
      backupBusy.mode = 'indeterminate';
      backupBusy.done = 0;
      backupBusy.total = 0;
      backupBusy.text = t('restoring');
      try {
        const s = await api().restore_backup(b.id, target);
        if (s && s.ok) {
          applyState(s);
          if (s.backups) { state.backups = s.backups; state.backup_stats = s.stats || state.backup_stats; }
          closeRestore();
          // B1 + B4：用后端返回的 scan_report 只列非零项（定论方案）
          const rep = s.scan_report || {};
          const parts = [];
          if (rep.new) parts.push(t('restore.done.new', { n: rep.new }));
          if (rep.updated) parts.push(t('restore.done.overwrite', { n: rep.updated }));
          if (parts.length) {
            showToast(t('restore.done.batch') + '：' + parts.join('，'), 'success');
          } else {
            showToast(t('restore_done'), 'success');
          }
        } else {
          alert(t('restore_failed') + (s && s.error ? s.error : ''));
        }
      } catch (e) {
        alert(t('restore_failed') + (e && e.message ? e.message : e));
      } finally {
        restoreDlg.busy = false;
        backupBusy.show = false;
      }
    }

    async function deleteBackup(b) {
      if (!b) return;
      closeBackupMenu();
      if (!confirm(t('confirm_delete_backup', { name: b.name }))) return;
      const s = await api().delete_backup(b.id);
      if (s && s.backups) { state.backups = s.backups; state.backup_stats = s.stats || state.backup_stats; }
      showToast(t('backup_deleted'), 'success');
    }

    async function verifyBackup(b) {
      if (!b) return;
      closeBackupMenu();
      backupBusy.show = true;
      backupBusy.mode = 'indeterminate';
      backupBusy.done = 0;
      backupBusy.total = 0;
      backupBusy.text = t('verifying');
      try {
        const s = await api().verify_backup(b.id);
        if (s && s.ok) showToast(t('verify_ok', { n: s.files }), 'success');
        else showToast(t('verify_bad') + (s && s.error ? s.error : ''), 'warning');
      } finally {
        backupBusy.show = false;
      }
    }

    async function openBackupFolder(backupId) {
      closeBackupMenu();
      await api().open_backup_folder(backupId || '');
    }

    function loadBackupSettings(s) {
      const b = (s && s.backup) || {};
      backupSettings.auto_backup = b.auto_backup !== false;
      backupSettings.keep_versions = b.keep_versions || 3;
      backupSettings.backup_dir = b.backup_dir || '';
      backupSettings.effective_dir = b.effective_dir || '';
    }
    async function pickBackupDir() {
      const r = await api().pick_folder();
      if (r && r.path) backupSettings.backup_dir = r.path;
    }
    async function saveBackupSettings() {
      const s = await api().set_backup_settings(
        !!backupSettings.auto_backup,
        Number(backupSettings.keep_versions) || 3,
        backupSettings.backup_dir || '',
      );
      if (s) {
        loadBackupSettings({ backup: s.settings || {} });
        if (s.backups) { state.backups = s.backups; state.backup_stats = s.stats || state.backup_stats; }
      }
      showToast(t('save_done'), 'success');
    }

    async function loadSkinState() {
      const s = await api().get_skin_state();
      skin.available = !!s.available;
      skin.roles = s.roles || [];
      skin.config_found = !!s.config_found;
      skin.manager = s.manager || 'native';
    }

    async function setSkinActive(role, variant) {
      const s = await api().set_skin_active(role, variant);
      if (s.ok) {
        skin.available = !!s.available;
        skin.roles = s.roles || [];
        skin.config_found = !!s.config_found;
        skin.manager = s.manager || 'native';
      }
    }

    async function loadCardState() {
      const s = await api().get_card_state();
      card.available = !!s.available;
      card.roles = s.roles || [];
      card.config_found = !!s.config_found;
      card.manager = s.manager || 'native';
    }

    async function setCardActive(role, cardId) {
      const s = await api().set_card_active(role, cardId);
      if (s.ok) {
        card.available = !!s.available;
        card.roles = s.roles || [];
        card.config_found = !!s.config_found;
        card.manager = s.manager || 'native';
      }
    }

    function clearEnabled() {
      // ====== 纯前端：确认后把所有 canonical 所有副本 is_enabled 改成 false + 移到 disabled ======
      const allCanonicals = [...state.enabled, ...state.disabled];
      // 只读扫描：有没有任何启用的副本
      let hadAny = false;
      for (const c of allCanonicals) {
        for (const v of c.all_versions) {
          if (v.is_enabled) { hadAny = true; break; }
        }
        if (hadAny) break;
      }
      if (!hadAny) { alert(t('empty_enabled')); return; }
      if (!confirm(t('confirm_clear_enabled'))) return;
      // 确认后才写：副本 is_enabled 置 false
      for (const c of allCanonicals) {
        for (const v of c.all_versions) {
          v.is_enabled = false;
          v.last_toggled_at = Date.now() / 1000;
        }
      }
      // 原子归属：所有 canonical 移到 disabled
      state.disabled.push(...state.enabled.splice(0, state.enabled.length));
      checkDirtyStatus();
      showToast(t('cleared_enabled'), 'success');
    }

    // 2026-11-06 批量启停：纯前端，循环 _toggleFrontend。批量启用最后统一互斥收口。
    function _reconcileFrontendMutualExclusion() {
      // 2026-11-09 需求反转：副本是独立模组，不再互斥停其他副本
      // 此函数清空保留（调用点 L3113 还在，改成空操作避免调用点报错）
    }

    // ------------------------------------------------------------------ 游戏版本 & Beta 分支
    const betaSwitching = ref(false);       // 正在切换（禁用按钮）
    const appUpdateStatus = ref(null);      // 下载状态 { is_downloading, progress, bytes... }
    let _statusPollTimer = null;

    function _startStatusPolling() {
      if (_statusPollTimer) return;
      _statusPollTimer = setInterval(async () => {
        try {
          const s = await api().get_app_update_status();
          if (s.ok) {
            appUpdateStatus.value = s;
            // 下载完成 → 停轮询
            if (s.is_installed && !s.is_downloading) {
              _stopStatusPolling();
              await refreshGameVersion();
            }
          }
        } catch (e) { /* ignore */ }
      }, 2000);
    }
    function _stopStatusPolling() {
      if (_statusPollTimer) { clearInterval(_statusPollTimer); _statusPollTimer = null; }
    }

    async function setAppBeta(branch) {
      if (betaSwitching.value) return;
      try {
        console.log('[setAppBeta] 点击分支:', branch);
        betaSwitching.value = true;
        showToast(t('beta_switching', { branch: branch }), 'info');
        const result = await api().set_app_beta(branch);
        console.log('[setAppBeta] 后端返回:', JSON.stringify(result));
        if (result.ok) {
          // 不重启 Steam！瞬间返回
          showToast(result.note || 'Steam 已开始下载...', 'info');
          _startStatusPolling();  // 开始轮询下载状态
          await refresh(true);    // 刷新 mod 列表（毫秒级）
        } else {
          if (result.error === 'game_running') {
            showToast(t('beta_switch_game_running'), 'warning');
          } else {
            showToast(t('beta_switch_failed') + ': ' + (result.message || result.error), 'error');
          }
        }
      } catch (e) {
        console.error('[setAppBeta] 异常:', e);
        showToast(t('beta_switch_failed') + ': ' + (e && e.message || e), 'error');
      } finally {
        betaSwitching.value = false;
      }
    }

    async function refreshGameVersion() {
      const v = await api().get_game_version_info();
      Object.assign(gameInfo, v);
    }

    // 存档管理辅助函数
    function formatSize(bytes) {
      if (!bytes || bytes <= 0) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0, n = bytes;
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
      return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
    }
    function formatTime(ts) {
      if (!ts) return '';
      try {
        const d = new Date(ts * 1000);
        const now = Date.now();
        const diff = now - d.getTime();
        if (diff < 60000) return t('saves_mtime_just_now') || '刚刚';
        if (diff < 3600000) return Math.floor(diff / 60000) + ' ' + (t('saves_mtime_min_ago') || '分钟前');
        if (diff < 86400000) return Math.floor(diff / 3600000) + ' ' + (t('saves_mtime_hour_ago') || '小时前');
        if (diff < 7 * 86400000) return Math.floor(diff / 86400000) + ' ' + (t('saves_mtime_day_ago') || '天前');
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      } catch (e) { return ''; }
    }

    // ------------------------------------------------------------------ 存档管理（MVP 只读展示 + 备份）
    async function openSavesManager() {
      savesManager.show = true;
      await loadSavesList();
    }

    async function loadSavesList() {
      savesManager.busy = true;
      try {
        const [slots, bk] = await Promise.all([
          api().list_save_slots(),
          api().list_save_backups(),
        ]);
        savesManager.profiles = slots.profiles || [];
        savesManager.backups = (bk && bk.profiles) || [];
      } catch (e) {
        showToast(t('saves_load_failed') + ': ' + e.message, 'error');
      } finally {
        savesManager.busy = false;
      }
    }

    async function backupSaveSlot(profileNum, kind) {
      // kind: 'official' | 'modded'
      try {
        const r = await api().backup_save_slot(profileNum, kind);
        if (r.ok) {
          showToast(t('saves_backup_ok'), 'success');
          await loadSavesList();
        } else {
          showToast(t('saves_backup_failed') + ': ' + r.error, 'error');
        }
      } catch (e) {
        showToast(t('saves_backup_failed') + ': ' + e.message, 'error');
      }
    }

    async function restoreSaveSlot(profileNum, kind, backupPath) {
      if (!confirm(t('saves_restore_confirm'))) return;
      try {
        const r = await api().restore_save_slot(profileNum, kind, backupPath);
        if (r.ok) {
          showToast(t('saves_restore_ok'), 'success');
          await loadSavesList();
        } else {
          showToast(t('saves_restore_failed') + ': ' + r.error, 'error');
        }
      } catch (e) {
        showToast(t('saves_restore_failed') + ': ' + e.message, 'error');
      }
    }

    async function launch() {
      // 延迟保存兜底：有未保存变更时先自动 commit，确保游戏读到最新 settings.save
      if (hasUnsavedChanges.value) {
        await save();
      }
      const force = false;
      launchState.value = 'launching';
      await api().launch_game(force);
      // C3: 轮询检测进程 —— 加 45 秒总超时 + 进程出现后又消失的"闪退"检测
      const startedAt = Date.now();
      let everRunning = false;
      const timer = setInterval(async () => {
        try {
          const running = await api().is_running();
          if (running) {
            everRunning = true;
            launchState.value = 'launched';
          } else if (everRunning) {
            // 之前检测到运行过，现在消失了 → 闪退，回到 idle
            launchState.value = 'idle';
            clearInterval(timer);
            showToast('游戏启动后立即退出，请检查游戏日志', 'error');
          }
          // 超时兜底
          if (Date.now() - startedAt > 45000) {
            clearInterval(timer);
            if (launchState.value === 'launching') {
              launchState.value = 'idle';
              showToast('启动超时（45秒），请检查 Steam 是否正常运行', 'error');
            }
          }
        } catch (e) {
          // API 异常不应该卡住轮询，继续等
          console.warn('[launch] is_running 异常:', e);
        }
      }, 2000);
    }

    // P1: 自实现拖拽 —— Pointer Events + 占位符驱动列表实时挤开
    // 思路: pointerdown 记录起始 → pointermove 算目标 idx 实时改 dragDisplay（Vue 重渲 → flex 自动挤开）
    //  → pointerup 直接改前端 state.enabled 数组顺序 + markDirty（延迟保存架构，不调 set_enabled / reorder_enabled）
    let _cleanupFns = [];  // onPointerMove/onPointerUp 引用（需动态移除）

    function _getListEl(which) {
      if (which === 'disabled') return document.querySelector('.disabled-mod-list');
      return document.querySelector('.enabled-list');
    }

    /** 根据光标坐标判断当前在哪个列表（或都不在）。 */
    function _hitList(clientX, clientY) {
      const el = _getListEl('enabled');
      if (el) {
        const r = el.getBoundingClientRect();
        if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom)
          return 'enabled';
      }
      const dl = _getListEl('disabled');
      if (dl) {
        const r = dl.getBoundingClientRect();
        if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom)
          return 'disabled';
      }
      return null;
    }

    /**
     * 中心点命中法：遍历 DOM 真实条目（过滤 placeholder），返回 flat 数组索引。
     * 语义：插在 flatList[返回值] 前面。
     * which: 'enabled' 或 'disabled'
     */
    function _computeFlatTargetIdx(clientY, which) {
      const list = _getListEl(which || 'enabled');
      if (!list) return -1;
      // 关键：排除 .drag-placeholder，让 DOM 索引和 flatList 对齐
      const items = list.querySelectorAll('.mod-item:not(.drag-placeholder)');
      for (let i = 0; i < items.length; i++) {
        const rect = items[i].getBoundingClientRect();
        const midY = rect.top + rect.height / 2;
        if (clientY < midY) return i;
      }
      return items.length;
    }

    function _autoScroll(clientY, which) {
      const list = _getListEl(which || 'enabled');
      if (!list) return;
      const rect = list.getBoundingClientRect();
      const edge = 40;
      if (clientY < rect.top + edge) {
        list.scrollTop -= 14;
        requestAnimationFrame(() => drag.active && _autoScroll(clientY, which));
      } else if (clientY > rect.bottom - edge) {
        list.scrollTop += 14;
        requestAnimationFrame(() => drag.active && _autoScroll(clientY, which));
      }
    }

    // === 拖拽系统（flat idx 语义）===
    function onDragStart(e, idx, m, whichList) {
      if (e.button !== 0) return;
      // workshop 副本可以精确启停（改 save 文件），允许拖拽
      drag.active = true;
      drag.fromList = whichList; // 'enabled' 或 'disabled'
      drag.targetList = whichList; // 初始默认 = 起始列表
      drag.fromIdx = idx;       // flat 数组起始索引（仅 enabled 内部排序用）
      drag.targetIdx = idx;     // flat 数组目标索引（仅 enabled 内部排序用）
      drag.fromModId = m.id;    // canonical id（延迟保存架构后只改前端 state，不再调 set_enabled / reorder_enabled）
      drag.fromMod = m;
      drag.pointerX = e.clientX;
      drag.pointerY = e.clientY;
      drag._pointerDownX = e.clientX;
      drag._pointerDownY = e.clientY;
      drag._hasMoved = false;

      // === 幽灵卡牌：clone 被拖的 .mod-item，紧贴光标走 ===
      const gripEl = e.currentTarget; // 即 .drag-grip
      const itemEl = gripEl.closest('.mod-item');
      if (itemEl) {
        drag._ghost = itemEl.cloneNode(true);
        drag._ghost.classList.add('drag-ghost');
        drag._ghost.style.position = 'fixed';
        drag._ghost.style.left = '0';
        drag._ghost.style.top = '0';
        drag._ghost.style.zIndex = '9999';
        drag._ghost.style.pointerEvents = 'none';
        drag._ghost.style.margin = '0';  // 消除可能的 margin 影响
        drag._ghost.style.transition = 'none';  // 禁止 CSS 过渡动画

        // 先隐藏 append 到 DOM 以便测量尺寸
        drag._ghost.style.visibility = 'hidden';
        document.body.appendChild(drag._ghost);
        // 拿原始元素尺寸（只算一次，存下来）
        const rect = itemEl.getBoundingClientRect();
        drag._ghostWidth = rect.width;
        drag._ghostHeight = rect.height;
        drag._ghost.style.width = rect.width + 'px';
        drag._ghost.style.height = rect.height + 'px';
        drag._ghost.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
        drag._ghost.style.visibility = 'visible';

        // 原位置半透明（虚影保留在原位，让用户知道自己从哪拖起来的）
        itemEl.style.opacity = '0.5';
        drag._originalItem = itemEl;
      }

      const moveHandler = (ev) => onDragMove(ev);
      const upHandler = (ev) => onDragUp(ev);
      window.addEventListener('pointermove', moveHandler);
      window.addEventListener('pointerup', upHandler);
      _cleanupFns = [() => window.removeEventListener('pointermove', moveHandler),
                     () => window.removeEventListener('pointerup', upHandler)];
      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'grabbing';
    }

    function onDragMove(e) {
      if (!drag.active) return;
      drag.pointerX = e.clientX;
      drag.pointerY = e.clientY;

      // 幽灵卡牌紧贴光标（用存下来的原始尺寸，不要再调 getBoundingClientRect）
      if (drag._ghost && drag._ghost.parentNode) {
        drag._ghost.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
      }

      if (!drag._hasMoved) {
        const dx = e.clientX - drag._pointerDownX;
        const dy = e.clientY - drag._pointerDownY;
        if (Math.hypot(dx, dy) < 4) return;
        drag._hasMoved = true;
      }

      // 实时检测光标当前在哪个列表
      drag.targetList = _hitList(e.clientX, e.clientY);

      // 只有 targetList === 'enabled' 才需要算 targetIdx + autoScroll（placeholder 挤开用）
      if (drag.targetList === 'enabled') {
        drag.targetIdx = _computeFlatTargetIdx(e.clientY, 'enabled');
        _autoScroll(e.clientY, 'enabled');
      } else if (drag.targetList === 'disabled') {
        // 拖到 disabled 列表也自动滚（方便用户往下拖找位置，虽然 disabled 不排序）
        _autoScroll(e.clientY, 'disabled');
      }
    }

    async function onDragUp(e) {
      _cleanupFns.forEach(fn => fn());
      _cleanupFns = [];
      document.body.style.userSelect = '';
      document.body.style.cursor = '';

      // === 清理幽灵卡牌 + 无条件恢复原元素 opacity ===
      if (drag._ghost && drag._ghost.parentNode) {
        drag._ghost.parentNode.removeChild(drag._ghost);
        drag._ghost = null;
      }
      if (drag._originalItem && drag._originalItem.parentNode) {
        drag._originalItem.style.opacity = '';
      }
      drag._originalItem = null;

      const wasMoved = drag._hasMoved;
      const fromList = drag.fromList;
      const targetList = drag.targetList;
      const fromFlat = drag.fromIdx;
      const toFlat = drag.targetIdx;
      const fromModId = drag.fromModId;
      const fromMod = drag.fromMod;
      drag.active = false;

      if (!wasMoved) return;

      // === 分四种情况处理 ===
      // A) disabled → enabled    = 启用 + 插到目标位置
      // B) enabled → disabled    = 停用
      // C) enabled → enabled     = 内部排序（现有逻辑）
      // D) disabled → disabled   = 不处理（停用列表无排序）
      // E) targetList=null       = 没丢到任何列表（丢到空白处）→ 取消

      if (targetList === null) {
        return; // 丢到空白处，取消
      }

      if (fromList === 'disabled' && targetList === 'enabled') {
        // —— A) 启用 + 插到 placeholder 位置（纯前端，0 延迟）——
        let insertFlatItem = null;
        if (toFlat >= 0) {
          const flatList = displayList(filteredEnabled.value, 'enabled', enabledSearch.value);
          const insertAt = Math.max(0, Math.min(toFlat, flatList.length));
          for (let i = insertAt; i < flatList.length; i++) {
            if (flatList[i] && !flatList[i].__ph) { insertFlatItem = flatList[i]; break; }
          }
        }
        let targetCanonicalIdx;
        if (insertFlatItem) {
          const targetMid = (insertFlatItem.id || '').toLowerCase();
          targetCanonicalIdx = state.enabled.findIndex(c => (c.id || '').toLowerCase() === targetMid);
        } else {
          targetCanonicalIdx = state.enabled.length;
        }
        // 用 _toggleFrontend 改副本 is_enabled，并指定 insertAt 插入 canonical 到 enabled
        _toggleFrontend(fromMod, true, { insertAt: targetCanonicalIdx });
        _localRecheckDeps();
        nextTick(drawDeps);
        return;
      }

      if (fromList === 'enabled' && targetList === 'disabled') {
        // —— B) 停用（纯前端，0 延迟）——
        _toggleFrontend(fromMod, false);
        nextTick(drawDeps);
        return;
      }

      if (fromList !== 'enabled' || targetList !== 'enabled') {
        // —— D) disabled→disabled 或 其它非排序情况 ——
        return;
      }

      // —— C) enabled → enabled 内部排序（现有逻辑，完整保留）——
      if (fromFlat < 0 || toFlat < 0) return;
      if (fromFlat === toFlat) return;

      // _ek2 保留：L2649-2655 的 flat↔flat 比较还要用它区分同 mod_id 不同副本
      const _ek2 = window.__entryKey || (m => (m.workshop_id ? String(m.workshop_id) : '') + '|' + (m.id || '').toLowerCase());
      const enabledArr = [...state.enabled];
      // canonical↔flat 比较用纯 mod_id（canonical 级别一个 mod_id 一条）
      const fromMid = (fromMod.id || '').toLowerCase();
      const fromCanonicalIdx = enabledArr.findIndex(c => (c.id || '').toLowerCase() === fromMid);
      if (fromCanonicalIdx < 0) return;

      const flatList = displayList(filteredEnabled.value, 'enabled', enabledSearch.value);
      const insertAt = Math.max(0, Math.min(toFlat, flatList.length));

      let leftFlat = null;
      let rightFlat = null;
      for (let i = insertAt - 1; i >= 0; i--) {
        if (flatList[i] && !flatList[i].__ph) { leftFlat = flatList[i]; break; }
      }
      for (let i = insertAt; i < flatList.length; i++) {
        if (flatList[i] && !flatList[i].__ph) { rightFlat = flatList[i]; break; }
      }

      // flat↔flat 用 entryKey（同 mod_id 多副本要按 wid 区分）
      const fromEk = _ek2(fromMod);
      const leftEk = leftFlat ? _ek2(leftFlat) : null;
      const rightEk = rightFlat ? _ek2(rightFlat) : null;
      if (leftEk === fromEk && rightEk === fromEk) return;
      if (leftEk === rightEk && leftEk !== null) return;

      // canonical↔flat 用纯 mod_id（canonical 级别唯一）
      let toCanonicalIdx;
      if (rightFlat) {
        const rightMid = (rightFlat.id || '').toLowerCase();
        toCanonicalIdx = enabledArr.findIndex(c => (c.id || '').toLowerCase() === rightMid);
      } else if (leftFlat) {
        const leftMid = (leftFlat.id || '').toLowerCase();
        const leftIdx = enabledArr.findIndex(c => (c.id || '').toLowerCase() === leftMid);
        toCanonicalIdx = leftIdx + 1;
      } else {
        toCanonicalIdx = 0;
      }

      if (fromCanonicalIdx === toCanonicalIdx) return;

      const moved = enabledArr.splice(fromCanonicalIdx, 1)[0];
      let finalTo = toCanonicalIdx;
      if (fromCanonicalIdx < toCanonicalIdx) {
        finalTo--;
        if (finalTo === fromCanonicalIdx) {
          finalTo = toCanonicalIdx;
        }
      }
      finalTo = Math.max(0, Math.min(finalTo, enabledArr.length));
      enabledArr.splice(finalTo, 0, moved);

      state.enabled = enabledArr;
      _localRecheckDeps();

      if (selected.value && fromMod && fromMod.__row_key === selected.value.__row_key) {
        requestAnimationFrame(() => {
          const newList = displayList(filteredEnabled.value, 'enabled', enabledSearch.value);
          const found = newList.find(x => x.__row_key === fromMod.__row_key);
          if (found) selected.value = found;
        });
      }

      if (enabledSearch.value.trim()) {
        showToast('已在当前搜索范围内重排', 'success');
      }
      // 延迟保存架构：排序只改前端 state.enabled 数组，commit_changes 统一同步
      checkDirtyStatus();
    }

    async function openMenu(e, m) {
      // workshop 副本可以精确启停，允许弹右键菜单
      menu.mod = m;
      menu.x = e.clientX;
      menu.y = e.clientY;
      // 2026-10：活动/非活动副本区分已删除，右键菜单直接显示启/停
      // 按当前副本 m.is_enabled 判断（方案 B 独立启停）
      // 2026-11-05: 如果有多选集合（Ctrl/Shift），toggleText 加 (n) 批量标记
      const baseText = m.is_enabled ? t('disable') : t('enable');
      menu.toggleText = (multiSelectedCount.value > 1) ? `${baseText} (${multiSelectedCount.value})` : baseText;
      menu.versions = [];  // available_versions 公开 API 已删除
      menu.show = true;
      await nextTick();
      flipMenu();
      // 再补一帧，兜底某些 webview 里 nextTick 后仍未完成布局的情况
      requestAnimationFrame(flipMenu);
    }

    // 菜单靠近底部/右侧时按「实际渲染位置」动态收拢，避免超出窗口
    function flipMenu() {
      const el = menuEl.value;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const { vw, vh } = viewportSize();
      const margin = 8;
      let dx = 0;
      let dy = 0;
      if (rect.right > vw - margin) dx = (vw - margin) - rect.right;
      if (rect.bottom > vh - margin) dy = (vh - margin) - rect.bottom;
      if (rect.left < margin) dx = margin - rect.left;
      if (rect.top < margin) dy = margin - rect.top;
      if (dx) menu.x += dx;
      if (dy) menu.y += dy;
    }

    function closeMenu() { menu.show = false; }

    // === 列表扁平化 (canonical → flat 副本数组) ===
    // state.enabled/disabled 是 canonical 粒度（每个 mod_id 一条）
    // 但 flat 副本要按自己的 source_states.is_enabled 决定显示在哪个侧
    function expandModList(list) {
      const result = [];
      const seenKeys = new Set();
      for (const m of list) {
        if (m.__ph) { result.push(m); continue; }
        const versions = (Array.isArray(m.all_versions) && m.all_versions.length > 0)
          ? m.all_versions
          : [{ key: m.id + '-default', version: m.version, source: m.source, source_label: m.source_label, workshop_id: m.workshop_id, path: null, is_enabled: (m.is_enabled !== undefined ? m.is_enabled : true) }];
        for (const v of versions) {
          // 去重必须按「mod_id + 物理 key」：工坊合集同一个 install_dir 下有多个
          // 不同 mod_id 的 manifest，只用 v.key 会把兄弟 mod 全部合并成一条，
          // 表现为双击停用后「又出现一条」，要再双击才能停掉下一个。
          const rk = (m.id || '').toLowerCase() + '@' + v.key;
          if (seenKeys.has(rk)) continue;
          seenKeys.add(rk);
          result.push({
            ...m,
            // 2026-11-01：每个副本独立显示名（workshop title > mod.json name）
            // 同 mod_id 多 workshop 副本（如圣主历险记原版 vs 修复版）各自显示 Steam 上的真实标题
            name: v.name || m.name,
            __version_key: v.key,
            // 工坊合集：同一 install_dir 下可能有多个 manifest（不同 mod_id），
            // __version_key 会重复；UI 身份用 mod_id + 物理 key 保证一条一个唯一 key
            __row_key: rk,
            source: v.source,
            // 关键：覆盖 external_source！flat 副本可能是本地副本，
            // 不能继承 canonical 的 external_source="steam"（工坊）
            external_source: v.external_source || null,
            source_label: v.source_label || v.source,
            workshop_id: v.workshop_id,
            version: v.version,
            install_path: v.path,
            // 该副本独立的启停状态
            is_enabled: (v.is_enabled !== undefined) ? v.is_enabled : m.is_enabled,
            // 该副本最近一次 toggle 时间戳（后端 _source_last_toggled 下发）
            last_toggled_at: v.last_toggled_at || 0,
            // 分开的启停时间（后端 2026-11-06 Step 2 新增）
            enabled_at: v.enabled_at || 0,
            disabled_at: v.disabled_at || 0,
            // 2026-11-07: batch_seq 彻底删
            // 2026-11-07 Step 1: scanner 固化的文件系统时间（排序链兜底）
            mtime: v.mtime || 0,
            ctime: v.ctime || 0,
          });
        }
      }
      return result;
    }
    // 新：把两侧 canonical 都拉平，按每条 flat 副本自己的 is_enabled 分到对应侧
    // side 为 'enabled' 时返回 is_enabled=true 的副本，按 state.enabled 顺序排
    // side 为 'disabled' 时返回 is_enabled=false 的副本
    // 内部处理搜索过滤（flat 副本级别）
    function displayList(rawList, side /* 'enabled' | 'disabled' */, searchQuery) {
      // 用全局缓存（state.enabled/disabled 变动时 computed 自动失效重建）
      const allFlat = _cachedAllFlat.value;
      const targetEnabled = (side === 'enabled');
      let filtered = allFlat.filter(f => !!f.is_enabled === targetEnabled);
      // 搜索过滤（flat 副本级别，匹配 id/name/author）
      const q = (searchQuery || '').trim().toLowerCase();
      if (q) {
        filtered = filtered.filter(f => {
          const hay = [f.name, f.id, f.author, f.version, f.source_label, f.source]
            .filter(Boolean).join(' ').toLowerCase();
          return hay.includes(q);
        });
      }
      // 统一：按 canonical 在对应侧 state 数组里的顺序排
      // state.enabled/disabled 是 canonical 粒度（一个 mod_id 一条），所以用纯 modid_lower 匹配
      // （不能用 wid|modid 因为 canonical 对象的 wid 可能是 null，而 flat 行有具体 wid）
      const _mid = (m) => (m.id || '').toLowerCase();
      const _stableSub = (a, b) => {
        // 同 canonical 下多副本：按 install_dir（__version_key 的尾部）稳定排序
        const ak = a.__version_key || '';
        const bk = b.__version_key || '';
        if (ak === bk) return 0;
        return ak < bk ? -1 : 1;
      };
      if (side === 'enabled') {
        const order = state.enabled.map(_mid);
        filtered.sort((a, b) => {
          const ai = order.indexOf(_mid(a));
          const bi = order.indexOf(_mid(b));
          if (ai === -1 && bi === -1) return _stableSub(a, b);
          if (ai === -1) return 1;
          if (bi === -1) return -1;
          if (ai !== bi) return ai - bi;
          return _stableSub(a, b);
        });
      } else {
        // disabled 侧三级排序：disabled_at 倒序 → state.disabled index → __version_key 兜底
        // 最新停用的飞第一，多选批量停用按 canonical 顺序平移，同 mod_id 副本各排各的
        const indexMap = new Map();
        state.disabled.forEach((m, idx) => indexMap.set((m.id || '').toLowerCase(), idx));
        filtered.sort((a, b) => {
          const ta = a.disabled_at || 0;
          const tb = b.disabled_at || 0;
          if (ta !== tb) return tb - ta;                         // 第一关：时间大的（最近停用）排前面
          const ia = indexMap.get((a.id || '').toLowerCase()) ?? 999999;
          const ib = indexMap.get((b.id || '').toLowerCase()) ?? 999999;
          if (ia !== ib) return ia - ib;                         // 第二关：时间相同（多选批量停）→ 按 state.disabled 尺码
          return String(a.__version_key).localeCompare(String(b.__version_key));  // 第三关：同 canonical 多副本 → 按物理路径兜底
        });
      }
      return filtered;
    }
    function dupCount(m) {
      return (m && Array.isArray(m.all_versions)) ? m.all_versions.length : 0;
    }
    async function locateVersion(targetKey) {
      menu.show = false;
      const allList = expandModList([...state.enabled, ...state.disabled]);
      const found = allList.find(x => x.__version_key === targetKey);
      if (!found) { showToast('找不到此副本', 'warning'); return; }
      selected.value = found;
      await nextTick();
      const el = document.querySelector(`.mod-item[data-ver-key="${CSS.escape(targetKey)}"]`);
      if (!el) return;
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      let flashes = 0;
      const origClass = el.className;
      const flash = () => {
        if (flashes >= 6) { el.className = origClass; return; }
        if (el.classList.contains('selected')) el.classList.remove('selected'); else el.classList.add('selected');
        flashes++;
        setTimeout(flash, 120);
      };
      setTimeout(flash, 300);
    }

    function menuToggle() {
      menu.show = false;
      if (multiSelectedCount.value > 1) {
        // 多选批量模式：包含工坊副本（单副本启停本来就支持工坊）
        const targetEnable = !menu.mod.is_enabled;
        const allFlat = expandModList([...state.enabled, ...state.disabled]);
        const targets = allFlat.filter(m => _multiHas(m.__row_key));
        let okCount = 0;
        if (targetEnable) {
          // 批量启用：副本独立，不再互斥拦截
          for (const m of targets) {
            if (m.is_enabled === targetEnable) continue;
            _toggleFrontend(m, targetEnable, { suppressDupToast: true });
            okCount++;
          }
        } else {
          // 批量停用：绕开 toggleFrontend，手写纯净逻辑避免 unshift 反转
          const touchedCanonicals = new Set();
          for (const m of targets) {
            if (m.is_enabled === targetEnable) continue;
            const midLow = (m.id || '').toLowerCase();
            const canonical = state.enabled.find(c => (c.id || '').toLowerCase() === midLow);
            if (!canonical) continue;
            const vIdx = canonical.all_versions.findIndex(v => v.key === m.__version_key);
            if (vIdx < 0) continue;
            canonical.all_versions[vIdx].is_enabled = false;
            const _now = Date.now() / 1000;
            canonical.all_versions[vIdx].disabled_at = _now;
            canonical.all_versions[vIdx].last_toggled_at = _now;
            const _wid = m.workshop_id || null;
            touchedKeys.add(JSON.stringify([midLow, _wid, m.__version_key]));
            touchedCanonicals.add(canonical);
            okCount++;
          }
          const movedCanonicals = state.enabled.filter(c =>
            touchedCanonicals.has(c) && !c.all_versions.some(v => v.is_enabled)
          );
          state.enabled = state.enabled.filter(c => !movedCanonicals.includes(c));
          state.disabled = [...state.disabled, ...movedCanonicals];
          checkDirtyStatus();
          try { api().apply_frontend_delta(
            state.enabled.map(c => c.id),
            state.disabled.map(c => c.id), null
          ); } catch(e) { console.warn('[menuToggleDisable] apply_frontend_delta 失败', e); }
        }
        if (targetEnable) _reconcileFrontendMutualExclusion(); // 空操作，保留调用点
        _multiClear();
        _localRecheckDeps();
        const verb = targetEnable ? t('batch_enable') : t('batch_disable');
        showToast(`${verb}: ${okCount}/${targets.length}`, 'success');
      } else {
        // 单副本模式
        const m = menu.mod;
        const enable = !m.is_enabled;
        _toggleFrontend(m, enable);
        _localRecheckDeps();
      }
    }

    async function refreshWsPreview() {
      const m = menu.mod;
      menu.show = false;
      if (!m || !m.workshop_id) return;
      try {
        const r = await api().refresh_workshop_preview(m.id, m.workshop_id);
        if (r && r.ok) {
          // 后端已删完两层缓存并立刻重抓了一次。无论当前选中没选中这个 mod，
          // 都主动触发右侧预览面板刷新——让用户立刻看到新数据。
          const d = await api().get_workshop_details(m.id, m.workshop_id);
          workshop.data = (d && d.workshop_id && !d.error) ? d : null;
          workshop.error = d && d.error ? d.error : '';
          // 同时全量刷新一次 state，确保列表里的标题/标签也同步更新
          const s = await api().get_state();
          applyState(s);
          // 组合提示：删了哪些缓存 + 抓到的标题
          const del = r.deleted || {};
          const delParts = [];
          if (del.steam_webapi) delParts.push('Web API 缓存');
          if (del.workshop) delParts.push('工坊预览缓存');
          const delText = delParts.length ? `已删除 ${delParts.join('、')}` : '无缓存需清理';
          const titlePart = r.title ? `，重抓标题「${r.title}」` : '，重抓标题时未获取到（可能在风控中）';
          showToast(`${t('preview_refreshed')}：${delText}${titlePart}`, 'success');
        } else if (r && r.error === 'rate_limited') {
          showToast(t('preview_rate_limited'), 'warning');
        } else {
          showToast(t('preview_refresh_failed'), 'error');
        }
      } catch (e) {
        showToast(t('preview_refresh_failed'), 'error');
      }
    }

    async function openFolder() {
      const m = menu.mod;
      menu.show = false;
      await api().open_folder(m.id, m.path || null);
    }

    async function deleteMod() {
      const m = menu.mod;
      menu.show = false;
      if (!confirm(t('confirm_delete_mod', { name: m.name, path: m.path || t('unknown_path') }))) return;
      const s = await api().delete_mod(m.id, m.__version_key || null);
      if (s && s.ok) {
        applyState(s);
        // B1 + B3：用后端 partial 区分"全删" vs "部分变更"
        if (s.partial) {
          showToast(t('delete.done.partial', { name: m.name || m.id }), 'success');
        } else {
          showToast(t('delete.done.single', { name: m.name || m.id }), 'success');
        }
      } else {
        showToast(t('delete_failed'), 'error');
      }
    }

    // ---- 管理器标签弹窗 ----
    async function openTagMenu() {
      const m = menu.mod;
      menu.show = false;
      if (!m) return;
      const data = await api().get_manager_tags();
      const modTags = (data.mod_tags && data.mod_tags[m.id.toLowerCase()]) || [];
      tagMenu.mod = m;
      tagMenu.allTags = data.tags || [];
      tagMenu.tags = [...modTags];
      tagMenu.newTag = '';
      tagMenu.show = true;
    }

    function closeTagMenu() { tagMenu.show = false; }

    function toggleTag(tag) {
      const i = tagMenu.tags.indexOf(tag);
      if (i >= 0) tagMenu.tags.splice(i, 1);
      else tagMenu.tags.push(tag);
    }

    async function addTag() {
      const name = tagMenu.newTag.trim();
      if (!name) return;
      await api().add_manager_tag(name);
      const data = await api().get_manager_tags();
      tagMenu.allTags = data.tags || [];
      if (tagMenu.tags.indexOf(name) < 0) tagMenu.tags.push(name);
      tagMenu.newTag = '';
    }

    async function deleteTag(tag) {
      await api().delete_manager_tag(tag);
      const data = await api().get_manager_tags();
      tagMenu.allTags = data.tags || [];
      tagMenu.tags = tagMenu.tags.filter(t => t !== tag);
    }

    async function saveModTags() {
      if (!tagMenu.mod) { tagMenu.show = false; return; }
      // 乐观更新：直接改本地 state 里的 mod 对象（避免 applyState 整条重灌闪烁）
      const modId = tagMenu.mod.id;
      const allMods = [...state.enabled, ...state.disabled];
      allMods.forEach(m => { if (m.id === modId) m.manager_tags = tagMenu.tags; });
      tagMenu.show = false;
      // 后台同步：只写磁盘，成功不覆盖 state，失败回滚
      api().set_mod_tags(modId, tagMenu.tags).catch(() => {
        api().get_state().then(s => applyState(s)).catch(() => {});
      });
    }

    async function openSettings() {
      const s = await api().get_settings();
      settings.form = {
        manual_game_root: s.manual_game_root || '',
        manual_mods_dirs: s.manual_mods_dirs || '',
        extra_mods_list: (s.manual_mods_dirs || '').split(/[;|]/).map(x => x.trim()).filter(Boolean),
        manual_workshop_dir: s.manual_workshop_dir || '',
        manual_mods_dir: s.manual_mods_dir || '',
        manual_save_dir: s.manual_save_dir || '',
        language: (s.language === 'en') ? 'en' : 'zh',
        workshop_html_fill: s.workshop_html_fill !== false,
      };
      if (s.backup) loadBackupSettings(s);
      settings.show = true;
      settingsTab.value = 'general';
      loadAppInfo();
    }

    function closeSettings() { settings.show = false; }

    async function loadAppInfo() {
      try {
        const info = await api().get_app_info();
        appInfo.version = info.version || '';
      } catch (e) {
        appInfo.version = '';
      }
    }

    async function clearWorkshopCache() {
      if (!confirm(t('cache_confirm'))) return;
      const r = await api().clear_workshop_cache();
      const removed = (r && r.removed) || 0;
      const refreshing = (r && r.refreshing) || 0;
      showToast(refreshing
        ? t('cache_cleaned_refreshing', { removed, refreshing })
        : t('cache_cleaned', { removed }), 'success');
    }

    async function clearWorkshopCacheAll() {
      if (!confirm(t('cache_clear_all_confirm'))) return;
      const r = await api().clear_workshop_cache_all();
      const total = (r && r.refreshing) || 0;
      if (!total) return;
      bulkProgress.show = true;
      bulkProgress.done = 0;
      bulkProgress.total = total;
      await new Promise((resolve) => {
        const started = Date.now();
        const timeout = 120000; // 最长等 2 分钟
        const timer = setInterval(async () => {
          try {
            const p = await api().get_workshop_cache_progress();
            bulkProgress.done = p.done || 0;
            bulkProgress.total = p.total || total;
            if (p.total && p.done >= p.total) {
              clearInterval(timer);
              resolve();
              return;
            }
          } catch (e) {
            clearInterval(timer);
            resolve();
            return;
          }
          if (Date.now() - started > timeout) {
            clearInterval(timer);
            resolve();
          }
        }, 200);
      });
      bulkProgress.show = false;
      showToast(t('cache_clear_all_toast', { refreshing: total }), 'success');
    }

    async function clearDllCache() {
      if (!confirm(t('dll_cache_confirm'))) return;
      await api().clear_dll_cache();
      showToast(t('dll_cache_cleared'), 'success');
    }

    // Steam 工坊更新弹窗
    const updateDialog = reactive({ show: false });
    function openUpdateDialog() { updateDialog.show = true; }
    function closeUpdateDialog() { updateDialog.show = false; }

    function fmtUnixTime(ts) {
      if (!ts || ts <= 0) return '未知';
      const d = new Date(ts * 1000);
      return d.toLocaleString();
    }

    // 打开 Steam 工坊页面（让 Steam 客户端自动触发下载更新）
    function openSteamWs(wid) {
      if (!wid) return;
      // steam://url/CommunityFilePage/<wid> 直接在 Steam 客户端里打开工坊页面
      window.open(`steam://url/CommunityFilePage/${wid}`, '_blank');
    }

    // 弹窗里单个 mod 的备份按钮
    async function backupOneUpdate(u) {
      if (!u.id || u._backupStatus === 'loading' || u._backupStatus === 'ok') return;
      u._backupStatus = 'loading';
      try {
        const r = await api().backup_mod(u.id, 'manual');
        if (r && r.ok) {
          u._backupStatus = 'ok';
        } else {
          u._backupStatus = 'err';
          alert('备份失败: ' + (r && r.error || '未知错误'));
        }
      } catch (e) {
        u._backupStatus = 'err';
        alert('备份异常: ' + e.message);
      }
    }

    async function checkWorkshopUpdates() {
      workshopUpdates.checking = true;
      workshopUpdates.updates = [];
      workshopUpdates.checked = 0;
      workshopUpdates.errors = 0;
      try {
        const r = await api().check_workshop_updates();
        workshopUpdates.checked = r.checked || 0;
        workshopUpdates.errors = r.errors || 0;
        workshopUpdates.updates = r.updates || [];
        workshopUpdates.lastChecked = new Date().toLocaleTimeString();
        if (!workshopUpdates.updates.length) {
          alert(t('no_updates'));
        }
      } catch (e) {
        alert(t('check_update_failed') + (e && e.message ? e.message : e));
      } finally {
        workshopUpdates.checking = false;
      }
    }

    async function findReplacementMods() {
      replacementChecking.value = true;
      try {
        const r = await api().find_replacement_mods();
        replacementSuggestions.suggestions = r.suggestions || [];
        replacementSuggestions.scanned_count = r.scanned_count || 0;
        replacementSuggestions.idle_count = r.idle_count || 0;
        replacementSuggestions.note = r.note || '';
        replacementSuggestions.lastChecked = new Date().toLocaleTimeString();
      } catch (e) {
        showToast('分析失败: ' + (e.message || e), 'error');
      } finally {
        replacementChecking.value = false;
      }
    }

    async function exportSteamdb() {
      steamdbExport.exporting = true;
      try {
        const r = await api().export_steamdb();
        if (r.ok) {
          steamdbExport.path = r.path;
          steamdbExport.entries = r.entries || 0;
          steamdbExport.with_deps = r.with_deps || 0;
          showToast(`✅ 已导出 ${r.entries} 条 → ${r.path}`, 'success');
        } else {
          showToast('导出失败: ' + (r.error || '未知错误'), 'error');
        }
      } catch (e) {
        showToast('导出失败: ' + (e.message || e), 'error');
      } finally {
        steamdbExport.exporting = false;
      }
    }

    async function checkMpCompatibility() {
      mpCompat.checking = true;
      try {
        const r = await api().check_mp_compatibility();
        mpCompat.results = r.results || {};
        mpCompat.stats = r.stats || {};
        mpCompat.lastChecked = new Date().toLocaleTimeString();
      } catch (e) {
        showToast('联机检测失败: ' + (e.message || e), 'error');
      } finally {
        mpCompat.checking = false;
      }
    }

    async function setMpOverride(modId, status) {
      try {
        await api().set_mp_compat_override(modId, status);
        // 重新扫描以更新显示
        await checkMpCompatibility();
      } catch (e) {
        showToast('设置失败: ' + (e.message || e), 'error');
      }
    }

    function mpStatusLabel(status) {
      return {
        'full': '✅ 完全',
        'partial': '⚡ 部分',
        'incompatible': '⛔ 不兼容',
        'mp-required': '🔴 框架',
        'unknown': '❓ 未提及',
      }[status] || status;
    }

    async function openWorkshopItem(workshopId) {
      await api().open_workshop(workshopId, 'steam');
    }

    async function clearWsCache() {
      if (!confirm('确定清除所有 Steam 工坊预览缓存？\n下次点选工坊 mod 时会重新下载（可能触发 Steam 访问限制）')) return;
      try {
        const r = await api().clear_workshop_cache_force();
        workshop.data = null;
        workshop.error = '';
        showToast(`已清除 ${r.cleared || 0} 个工坊缓存`, 'success');
      } catch (e) {
        showToast('清除失败: ' + (e.message || e), 'error');
      }
    }

    async function openLogDiag() {
      logDiag.show = true;
      await loadGameLog();
      // 打开后自动滚到底（看最新日志）
      Vue.nextTick(() => {
        const v = document.querySelector('.log-view');
        if (v) v.scrollTop = v.scrollHeight;
      });
    }

    async function loadGameLog() {
      logDiag.loading = true;
      try {
        const d = await api().get_game_log(logDiag.file || '', logDiag.level, logDiag.keyword, logDiag.useRegex);
        logDiag.available = !!d.available;
        logDiag.path = d.path || '';
        logDiag.files = d.files || [];
        logDiag.lines = d.lines || [];
        logDiag.counts = d.counts || { info: 0, warn: 0, error: 0, other: 0 };
        logDiag.total = d.total || 0;
        logDiag.shown = d.shown || 0;
        logDiag.truncated = !!d.truncated;
        if (d.name) logDiag.file = d.name;  // 同步下拉选中项（含后端回退到默认日志的情况）
        // 加载完也自动滚到底（用户切换级别/关键词后刷新也是看最新）
        Vue.nextTick(() => {
          const v = document.querySelector('.log-view');
          if (v) v.scrollTop = v.scrollHeight;
        });
      } catch (e) {
        logDiag.available = false;
        logDiag.files = [];
        logDiag.lines = [];
      } finally {
        logDiag.loading = false;
      }
    }

    function closeLogDiag() { logDiag.show = false; }

    const filteredTagDiag = computed(() => {
      const q = tagDiag.filter.trim().toLowerCase();
      if (!q) return tagDiag.items;
      return tagDiag.items.filter(it =>
        (it.name || '').toLowerCase().includes(q)
        || (it.id || '').toLowerCase().includes(q)
        || (it.tags || []).join(',').toLowerCase().includes(q)
        || (it.reason || '').toLowerCase().includes(q)
      );
    });

    async function openTagDiag() {
      tagDiag.show = true;
      tagDiag.loading = true;
      tagDiag.filter = '';
      try {
        const d = await api().get_tag_diagnostics();
        tagDiag.items = d.items || [];
      } catch (e) {
        tagDiag.items = [];
      } finally {
        tagDiag.loading = false;
      }
      runTagSelftest();
    }

    async function runTagSelftest() {
      tagDiag.selftestLoading = true;
      try {
        const d = await api().run_tag_selftest();
        tagDiag.selftest = d.items || [];
      } catch (e) {
        tagDiag.selftest = [];
      } finally {
        tagDiag.selftestLoading = false;
      }
    }

    function closeTagDiag() { tagDiag.show = false; }

    async function saveSettings() {
      const extraStr = (settings.form.extra_mods_list || []).map(x => (x || '').trim()).filter(Boolean).join(';');
      const s = await api().set_settings(
        settings.form.manual_game_root || null,
        extraStr || null,
        settings.form.manual_workshop_dir || null,
        settings.form.manual_mods_dir || null,
        settings.form.manual_save_dir || null,
        settings.form.language === 'en' ? 'en' : 'zh',
        !!settings.form.workshop_html_fill,
      );
      settings.show = false;
      lang.value = settings.form.language === 'en' ? 'en' : 'zh';
      applyState(s);
    }

    async function pickFolder(field) {
      const r = await api().pick_folder();
      if (!r || !r.path) return;
      // 支持数组索引格式：'extra_mods_list[0]'
      const arrMatch = field.match(/^([a-zA-Z_][\w.]*)\[(\d+)\]$/);
      if (arrMatch) {
        const arrName = arrMatch[1];
        const idx = parseInt(arrMatch[2], 10);
        if (Array.isArray(settings.form[arrName])) {
          settings.form[arrName].splice(idx, 1, r.path);
          return;
        }
      }
      settings.form[field] = r.path;
    }

    async function detectPaths() {
      const r = await api().detect_paths();
      if (r.game_root) settings.form.manual_game_root = r.game_root;
      if (r.workshop_dir) settings.form.manual_workshop_dir = r.workshop_dir;
      if (r.mods_dir) settings.form.manual_mods_dir = r.mods_dir;
      if (r.save_dir) settings.form.manual_save_dir = r.save_dir;
    }

    function closeWindow() {
      api().close_window();
    }

    // === 依赖线 SVG 层 ===
    function computeGroups(list, selectedId) {
      const idToIndex = {};
      list.forEach((m, i) => { idToIndex[m.id.toLowerCase()] = i; });
      const groupsMap = {};
      list.forEach((m, i) => {
        (m.dependencies || []).forEach(dep => {
          const dl = dep.toLowerCase();
          if (dl in idToIndex) {
            const pi = idToIndex[dl];
            if (pi !== i) {
              if (!groupsMap[dl]) groupsMap[dl] = { parentIndex: pi, children: new Set() };
              groupsMap[dl].children.add(i);
            }
          }
        });
      });
      const groups = [];
      let lane = 0;
      const entries = Object.entries(groupsMap).sort((a, b) => a[1].parentIndex - b[1].parentIndex);
      for (const [dep, g] of entries) {
        const children = [...g.children].sort((a, b) => a - b);
        const all = [g.parentIndex, ...children];
        const error = g.parentIndex > Math.min(...children);
        const active = !!selectedId && (list[g.parentIndex].id === selectedId || children.some(c => list[c].id === selectedId));
        groups.push({
          parentIndex: g.parentIndex,
          children,
          minIndex: Math.min(...all),
          maxIndex: Math.max(...all),
          lane: lane % DEP_MAX_LANES,
          color: DEP_COLORS[lane % DEP_COLORS.length],
          active,
          error,
        });
        lane++;
      }
      return groups;
    }

    function drawDeps() {
      if (!depLayer.value) return;
      // 用 flat 副本数组（displayList），因为 __version_key 在 flat 上不在 canonical 上
      const list = displayList(filteredEnabled.value, 'enabled', enabledSearch.value);

      // 搜索启用 mod 时隐藏依赖线（过滤后索引对不上，视觉上也难看）
      if (enabledSearch.value.trim()) {
        depHeight.value = 0;
        depLayer.value.innerHTML = '';
        return;
      }

      depHeight.value = list.length * ITEM_HEIGHT;
      if (!list.length) { depLayer.value.innerHTML = ''; return; }
      // 只有当 selected 实际在启用列表（flat）里时才用来高亮依赖线
      // __version_key 是物理副本唯一标识，跨侧不会误匹配
      const selId = (selected.value && list.some(x => x.__row_key === selected.value.__row_key))
        ? selected.value.id : null;
      const groups = computeGroups(list, selId);
      const nodeX = DEP_AREA;
      let svg = '';
      groups.forEach(g => {
        const color = g.error ? '#ef4444' : g.color;
        const alpha = g.active ? 1 : 0.2;
        const laneX = DEP_BASE_X + g.lane * DEP_LANE_WIDTH;
        const y = idx => idx * ITEM_HEIGHT + ITEM_HEIGHT / 2;
        const yMin = y(g.minIndex);
        const yMax = y(g.maxIndex);
        const yParent = y(g.parentIndex);
        svg += `<line x1="${laneX}" y1="${yMin}" x2="${laneX}" y2="${yMax}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="2"/>`;
        svg += `<circle cx="${nodeX - 8}" cy="${yParent}" r="4" fill="${color}" fill-opacity="${alpha}"/>`;
        svg += `<line x1="${nodeX - 12}" y1="${yParent}" x2="${laneX}" y2="${yParent}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="2"/>`;
        g.children.forEach(c => {
          const yChild = y(c);
          svg += `<circle cx="${nodeX - 6}" cy="${yChild}" r="3" fill="${color}" fill-opacity="${alpha}"/>`;
          svg += `<line x1="${laneX}" y1="${yChild}" x2="${nodeX - 9}" y2="${yChild}" stroke="${color}" stroke-opacity="${alpha}" stroke-width="2"/>`;
        });
      });
      depLayer.value.innerHTML = svg;
    }

    // 启用列表滚动时，同步依赖线（绝对定位层需手动跟随滚动）
    function onDepScroll(e) {
      depScrollTop.value = e.target.scrollTop || 0;
    }

    const depStyle = computed(() => ({ transform: `translateY(-${depScrollTop.value}px)` }));

    // 搜索词变化会改变过滤列表，需重绘（或隐藏）依赖线
    watch(enabledSearch, () => nextTick(drawDeps));

    // 核心：任何启用列表变化（toggle、拖拽、loadPack、autoSort、save 等）都自动重绘依赖线
    // filteredEnabled 是 computed，state.enabled 引用变了就触发
    watch(filteredEnabled, () => nextTick(drawDeps));

    // 菜单每次显示后都再按实际位置收拢一次（兜底：版本列表异步加载导致的高度变化）
    watch(() => menu.show, (v) => {
      if (v) {
        nextTick(() => requestAnimationFrame(flipMenu));
      }
    });

    onMounted(() => {
      document.addEventListener('click', (e) => {
        if (!e.target.closest('.context-menu')) closeMenu();
        if (!e.target.closest('.ws-menu') && !e.target.closest('.ws-open-btn')) closeWsMenu();
        if (!e.target.closest('.pack-menu')) closePackMenu();
        if (!e.target.closest('.backup-menu')) closeBackupMenu();
      });

      // GUI 环境：等 pywebviewready（后端 API 就绪）后再用真实数据刷新；
      // 浏览器预览：pywebview 不会派发 pywebviewready，超时后用 mock 渲染。
      let refreshed = false;
      const doRefresh = () => {
        if (refreshed) return;
        refreshed = true;
        loadLanguageSettings();
        loadAppInfo();
        refreshGameVersion().then(() => {
          // 如果 Steam 正在下载，自动开始轮询（比如上次切换后下载还没完）
          api().get_app_update_status().then(s => {
            if (s.ok && s.is_downloading) _startStatusPolling();
          });
        });
        refresh();
        loadSkinState();
        loadCardState();
      };

      if (hasPywebviewApi()) {
        doRefresh();
      } else {
        window.addEventListener('pywebviewready', doRefresh, { once: true });
        setTimeout(() => {
          if (!hasPywebviewApi()) doRefresh();
        }, 400);
      }
    });

    // 临时诊断：return 之前确认 _relTime 存在
    console.log('[DIAG] setup() about to return, _relTime exists:', typeof _relTime, 'DEP_AREA:', typeof DEP_AREA);

    // ====== 延迟保存：beforeunload 拦截 ======
    // 关闭应用时如有未保存变更，弹浏览器原生确认框
    // 注意：Electron/Tauri WebView2 支持 beforeunload，退出进程时的原生退出不触发
    window.addEventListener('beforeunload', (e) => {
      if (hasUnsavedChanges.value) {
        e.preventDefault();
        e.returnValue = t('unsaved_changes_warn');
        return e.returnValue;
      }
    });

    // 格式化时间戳为本地时间字符串（调试/状态信息面板用）
    const fmtTs = (ts) => {
      if (!ts || ts <= 0) return '';
      const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    };

    return {
      state, breakdown: computed(() => state.breakdown), selected, multiSelected, multiSelectedCount, hasUnsavedChanges, enabledSearch, disabledSearch, launchText, depLayer, depHeight, depStyle,
      menuEl, packMenuEl,
      toasts, pauseToast, resumeToast, dismissToast, dllProgress, bulkProgress, scanProgress,
      filteredEnabled, filteredDisabled, selectedMeta, selectedDesc, selectedLocalTags, selectedManagerTags, selectedTagHits, selectedTags, selectedSiblings,
      canFetchTranslation, translationHint, translationBusy, fetchCommunityTranslation,
      effectiveDescription, effectivePreview, effectiveTitle, effectiveAuthor,
      subText, tooltip, itemWarnTitle, versionLabel,
      previewCollapse, fmtTs,
      workshop, wsMenu, openWsMenu, closeWsMenu, openWorkshop, bgProgress, currentProgressCard, progressTaskCount,
      refresh, select, toggle, autoSort, save, clearEnabled, launch, scan, copyText, doExport, togglePin,
      packMenu, openPackMenu, closePackMenu, sharePack, renamePack, deletePack, loadPack,
      packTab, skin, loadSkinState, setSkinActive, card, loadCardState, setCardActive,
      backupMenu, backupMenuEl, backupMod, backupAll, openBackupMenu, closeBackupMenu,
      restoreDlg, openRestore, closeRestore, confirmRestore, deleteBackup, verifyBackup, openBackupFolder,
      backupBusy, backupSettings, pickBackupDir, saveBackupSettings, fmtBytes, backupSrcClass,
      tagMenu, openTagMenu, closeTagMenu, toggleTag, addTag, deleteTag, saveModTags,
      onDragStart, onDragMove, onDragUp, openMenu, locateVersion, menuToggle, openFolder, deleteMod, refreshWsPreview,
      dblClickHandler,
      onDepScroll, drag, dragRenderList, indicatorStyle,
      expandModList, displayList, dupCount,
      openSettings, closeSettings, saveSettings, pickFolder, detectPaths, closeWindow, menu, settings,
      settingsTab, workshopUpdates, appInfo, feVer, checkWorkshopUpdates, replacementSuggestions, replacementChecking, findReplacementMods, steamdbExport, exportSteamdb, mpCompat, checkMpCompatibility, setMpOverride, mpStatusLabel, clearWorkshopCache, clearWorkshopCacheAll, clearDllCache, analyzeDlls, openWorkshopItem,
      logDiag, openLogDiag, closeLogDiag, loadGameLog,
      tagDiag, filteredTagDiag, openTagDiag, closeTagDiag,
      lang, tr, t,
      // Steam 工坊更新弹窗
      updateDialog, openUpdateDialog, closeUpdateDialog, fmtUnixTime, openSteamWs, backupOneUpdate,
      DEP_AREA,
      // 游戏版本 & Beta & 存档管理
      gameInfo, betaSwitching, appUpdateStatus, setAppBeta, refreshGameVersion,
      savesManager, openSavesManager, loadSavesList, backupSaveSlot, restoreSaveSlot,
      formatSize, formatTime,
    };
  },
});

app.config.errorHandler = function(err, vm, info) {
  console.error('[Vue ERR]', err, info);
  try {
    const api = window.pywebview && window.pywebview.api;
    if (api && api.log_js_error) {
      api.log_js_error(
        String(err && err.message || err),
        '',
        0, 0,
        `[Vue errorHandler] ${info}\n${err && err.stack || err}`
      );
    }
  } catch(_) {}
};
app.config.globalProperties._relTime = _relTime;
app.mount('#app');
