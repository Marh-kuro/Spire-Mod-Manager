// ==========================================================================
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
      fromModId: null,       // 被拖 canonical id（用于 set_enabled / reorder_enabled）
      fromMod: null,         // 被拖 flat 副本对象（ghost 显示用）
      pointerX: 0,
      pointerY: 0,
      itemHeight: ITEM_HEIGHT,
      _pointerDownX: 0,
      _pointerDownY: 0,
      _hasMoved: false,
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
    const settings = reactive({ show: false, form: { manual_game_root: '', manual_mods_dirs: '', manual_workshop_dir: '', manual_mods_dir: '', manual_save_dir: '', language: 'zh', workshop_html_fill: true } });
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
          if (opts.sticky) _clearToastTimer(found.id);
          else armToastTimer(found.id, TOAST_DURATION[found.type]);
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
      toasts.push({ id, key, text: content, type: kind, mergeKey: opts.mergeKey || null, count: 1 });
      if (opts.sticky) _clearToastTimer(id);
      else armToastTimer(id, TOAST_DURATION[kind]);
      return id;
    }

    function armToastTimer(id, duration) {
      _clearToastTimer(id);
      const st = { started: Date.now(), remaining: duration, timer: setTimeout(() => dismissToast(id), duration) };
      toastTimers.set(id, st);
    }

    function pauseToast(item) {
      const st = item && toastTimers.get(item.id);
      if (!st || !st.timer) return;
      clearTimeout(st.timer);
      st.timer = null;
      st.remaining = Math.max(0, st.remaining - (Date.now() - st.started));
    }

    function resumeToast(item) {
      const st = item && toastTimers.get(item.id);
      if (!st) return;
      if (st.remaining <= 0) { dismissToast(item.id); return; }
      armToastTimer(item.id, st.remaining);
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
      let list = q ? state.disabled.filter(m => matches(m, q)) : state.disabled;
      // 停用列表默认按最近活动时间倒序（刚停的排最上）
      return [...list].sort((a, b) => (b.last_activity || 0) - (a.last_activity || 0));
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
        const r = await api().request_workshop_translation(m.id);
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

    function depWarnTitle(m) {
      if (!m || !m.dep_flags) return '';
      const flagMap = {
        missing_install: '依赖的 mod 未安装',
        missing_enable: '依赖的 mod 未启用',
        order_warn: '依赖顺序可能错误',
        cycle: '检测到循环依赖',
        dup_warn: '同一 mod_id 存在多个物理副本（游戏仅加载活动版本）',
      };
      return '依赖问题：\n' + m.dep_flags.map(f => '  • ' + (flagMap[f] || f)).join('\n');
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
      // 防御性去重：后端应该保证无重复，但前端加保险
      // 多副本场景下如果 state.enabled/disabled 有同 id 重复 canonical，
      // expandModList 会把每个 canonical 的 all_versions 都展开 → 翻倍
      const _dedup = (arr) => {
        const seen = new Set();
        return (arr || []).filter(m => {
          const id = (m.id || '').toLowerCase();
          if (!id || seen.has(id)) return false;
          seen.add(id); return true;
        });
      };
      state.enabled = _dedup(s.enabled);
      state.disabled = _dedup(s.disabled);
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

      // 选中保留：先按 __version_key（install_dir hash）精确匹配；
      // 找不到再按 id 找；还找不到 → 保留旧 selected（如果它仍在 state 里）。
      // 只有 selected 真被删了（不在新 enabled+disabled 里）才清掉。
      const prevKey = selected.value ? selected.value.__version_key : null;
      const prevId = selected.value ? selected.value.id : null;
      let newSelected = null;
      if (prevKey && prevId) {
        const allFlat = expandModList([...state.enabled, ...state.disabled]);
        newSelected = allFlat.find(x => x.__version_key === prevKey);
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
          // show 条件：正在跑，或有明确进度差（done < total）
          // 跑完（done >= total）且没在跑 → 瞬间隐藏，不留 30s
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

    function select(m, side, event) {
      const vk = m.__version_key;
      // Ctrl/Meta（Mac）点击：toggle 单个
      if (event && (event.ctrlKey || event.metaKey)) {
        if (_multiHas(vk)) {
          _multiDel(vk);
        } else {
          _multiAdd(vk);
        }
        selected.value = m;
        _rangeAnchorKey.value = vk;
        nextTick(drawDeps);
        loadWorkshop(m);
        return;
      }
      // Shift 点击：范围选择（从锚点到当前副本，限制在同 is_enabled 侧，不能跨启用/停用）
      if (event && event.shiftKey && _rangeAnchorKey.value) {
        const allFlat = expandModList([...state.enabled, ...state.disabled]);
        const anchorIdx = allFlat.findIndex(x => x.__version_key === _rangeAnchorKey.value);
        const curIdx = allFlat.findIndex(x => x.__version_key === vk);
        if (anchorIdx >= 0 && curIdx >= 0) {
          const anchorEnabled = allFlat[anchorIdx].is_enabled;
          const [lo, hi] = anchorIdx < curIdx ? [anchorIdx, curIdx] : [curIdx, anchorIdx];
          for (let i = lo; i <= hi; i++) {
            if (allFlat[i].is_enabled === anchorEnabled) {
              _multiAdd(allFlat[i].__version_key);
            }
          }
        }
        selected.value = m;
        nextTick(drawDeps);
        loadWorkshop(m);
        return;
      }
      // 普通点击：清空多选 + 设置主选中
      _multiClear();
      selected.value = m;
      _rangeAnchorKey.value = vk;
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
        const p1 = bgProgress.phase1;
        const title = p1.total < 0 ? t('bg_fill_preparing') : (p1.running ? t('bg_fill_phase1') : t('bg_fill_done'));
        cards.push({
          id: 'bg',
          title,
          indeterminate: p1.total < 0,
          done: Math.max(0, p1.done || 0),
          total: Math.max(0, p1.total || 0),
          fillClass: p1.total < 0 ? 'fill-purple' : 'fill-green',
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
          countText: indeterminate ? '' : `${Math.min(done, total)}/${total}`,
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
        cached = await api().is_workshop_cached(m.id);
      } catch (e) {
        cached = true;
      }
      if (seq !== workshopSeq) { workshop.loading = false; return; }

      workshop.loading = true;
      try {
        const d = await api().get_workshop_details(m.id);
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
      const modName = (m && (m.name || m.title)) || '';
      const toastKeyOf = () => {
        const wid = workshop.data && workshop.data.workshop_id;
        return wid ? 'html:' + wid : '';
      };
      const token = ++translationPollToken;
      translationPolling.value = true;
      const stop = (_tk) => {
        if (token === translationPollToken) translationPolling.value = false;
        if (_tk) dismissToastKey(_tk);
        clearInterval(timer);
      };
      const tick = async () => {
        if (busy) return;
        busy = true;
        const tk = toastKeyOf();
        try {
          ticks++;
          if (seq !== workshopSeq) { stop(tk); return; }
          if (ticks > 60) { stop(tk); return; }  // 5 分钟兜底
          // 先看统一队列里该 wid 的状态，避免每 5s 重复拉完整详情
          const tasks = await api().get_html_tasks();
          if (seq !== workshopSeq) { stop(tk); return; }
          const wid = workshop.data && workshop.data.workshop_id;
          const st = wid && tasks && tasks.items ? tasks.items[wid] : null;
          if (st && (st.status === 'queued' || st.status === 'running')) {
            if (tk) showToast(t('preview_fetching', { name: modName }), 'info', { key: tk, sticky: true });
            return;
          }
          if (st && st.status === 'failed') {
            if (tk) showToast(t('preview_fetch_failed', { name: modName }), 'warning', { key: tk });
            stop(tk);
            return;
          }

          const d = await api().get_workshop_details(m.id);
          if (seq !== workshopSeq) { stop(tk); return; }
          if (d && d.html_desc_state === 'updated') {
            // 后台 HTML 补完 desc！刷新前端显示
            workshop.data = d;
            workshop.error = '';
            workshop.html_just_filled = true;
            if (tk) showToast(t('preview_fetched', { name: modName }), 'success', { key: tk });
            stop(tk);
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
            stop(tk);
          } else if (!st) {
            // 未入队且未检查过（例如开关关闭/退避中）→ 停止轮询
            stop(tk);
          } else {
            // st 存在但既没 updated 也没 html_checked（理论上不该发生）→ 停止
            stop(tk);
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
      await api().open_workshop(selected.value.id, mode);
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
      // m 是 displayList 展开出来的 flat 副本
      const canonical = state.enabled.find(x => x.id === m.id)
                     || state.disabled.find(x => x.id === m.id);
      if (!canonical) return;

      // 用 version_key（= install_dir 路径）精确找到用户点的物理副本
      // 这是唯一的精确定位方式，因为 norm_mod_source 无法区分 local vs disabled
      const targetKey = m.__version_key;

      // 精确获取当前副本的启停状态：从 all_versions 里找同 __version_key 的副本
      let curCopyEnabled = m.is_enabled;  // flat 副本已经带了后端返回的 is_enabled
      const enable = !curCopyEnabled;

      try {
        _localRecheckDeps();
        // workshop 副本和 local 副本用不同 toast——workshop 是改 save 文件（工坊状态），local 是搬目录
        const isWs = !!m.workshop_id;
        const toastKey = enable
          ? (isWs ? 'enabled_toast_workshop' : 'enabled_toast')
          : (isWs ? 'disabled_toast_workshop' : 'disabled_toast');
        showToast(t(toastKey, { name: canonical.name }), 'success');

        // 2026-10 关键：传 version_key 精确操作物理副本
        const s = await api().set_enabled(canonical.id, enable, null, targetKey);
        applyState(s);
        _localRecheckDeps();
        // 2026-11-05: toggle 不经过 select()，必须手动清空多选
        console.log('[DEBUG toggle after apply] multiSelected before clear=', JSON.stringify(multiSelected.value));
        _multiClear();
        // 选中保持当前点击的副本
        const flatAfter = expandModList([...state.enabled, ...state.disabled]);
        const findM = flatAfter.find(x => x.__version_key === targetKey);
        selected.value = findM || canonical;
      } catch (err) {
        console.error('[toggle] 失败:', err);
        showToast('操作失败: ' + (err.message || err), 'error');
        api().get_state().then(s => applyState(s)).catch(() => {});
      }
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
        const myId = (m.id || '').toLowerCase();
        const myIdx = enabledIdx[myId];  // 停用 mod 是 undefined
        const deps = m.dependencies || [];
        const isEnabled = myIdx !== undefined;

        // 停用 mod 不加载，依赖警告无意义 → 直接清零跳过
        if (!isEnabled) {
          m.has_dep_warning = false;
          m.dep_flags = [];
          m.missing_deps = [];
          return;
        }

        for (const depRaw of deps) {
          const depId = String(depRaw).toLowerCase();
          if (!depId) continue;
          let matchedAll = null;
          for (const a of allIds) { if (a === depId || a.startsWith(depId + '#') || depId.startsWith(a + '#')) { matchedAll = a; break; } }
          let matchedEnabled = null;
          for (const e of enabledIds) { if (e === depId || e.startsWith(depId + '#') || depId.startsWith(e + '#')) { matchedEnabled = e; break; } }

          if (matchedAll === null) {
            flags.push('missing_install');
            missingDeps.push(depId);
          } else if (matchedEnabled === null) {
            flags.push('missing_enable');
            missingDeps.push(matchedAll);
          } else if (isEnabled) {
            // 顺序检查只对启用 mod 有意义；反向也依赖我 = 双向引用，不算顺序错误
            const depIdx = enabledIdx[matchedEnabled];
            if (depIdx !== undefined && depIdx > myIdx && !enabledDepEdges.has(matchedEnabled + '>' + myId)) {
              flags.push('order_warn');
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
        m.has_dep_warning = flags.some(f => f !== 'dup_warn');
        m.dep_flags = flags;
        m.missing_deps = missingDeps;
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
      const r = await api().save_all();
      if (r && r.ok) {
        showToast(t('save_done'), 'success');
      } else {
        showToast((r && r.errors && r.errors.join('; ')) || '保存失败', 'error');
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
      if (scope === 'all' && !confirm(t('confirm_backup_all'))) return;
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

    async function clearEnabled() {
      if (!state.enabled.length) { alert(t('empty_enabled')); return; }
      if (!confirm(t('confirm_clear_enabled', { n: state.enabled.length }))) return;
      const s = await api().clear_enabled();
      applyState(s);
      showToast(t('cleared_enabled'), 'success');
    }

    // 2026-11-05 批量启停：遍历 multiSelected 逐个调精确 set_enabled
    async function batchSetEnabled(enable) {
      const count = multiSelected.value.length;
      if (!count) { showToast(t('no_multi_selected'), 'warning'); return; }
      const verb = enable ? t('batch_enable_verb') : t('batch_disable_verb');
      if (!confirm(t('confirm_batch_enable', { verb, n: count }))) return;
      const allFlat = expandModList([...state.enabled, ...state.disabled]);
      const targets = allFlat.filter(m => _multiHas(m.__version_key) && !m.workshop_id);
      let ok = 0, fail = 0;
      for (const m of targets) {
        try {
          const r = await api().set_enabled(m.id, enable, m.__version_key);
          if (r.ok) ok++; else fail++;
        } catch (e) { fail++; }
      }
      // 最后 applyState 一次（只在循环结束后）
      const lastState = await api().get_state();
      applyState(lastState);
      _multiClear();
      if (fail === 0) {
        showToast(t('batch_done_all', { verb, n: ok }), 'success');
      } else {
        showToast(t('batch_done_partial', { verb, ok, fail }), 'warning');
      }
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
    //  → pointerup 提交后端 set_enabled / reorder_enabled → applyState
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
      drag.fromModId = m.id;    // canonical id（用于 set_enabled / reorder_enabled）
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
        // —— A) 启用 + 插到 placeholder 位置 ——
        const enabledArr = [...state.enabled];
        let insertCid = null; // 插到哪个 canonical 前面（或后面追加）
        if (toFlat >= 0) {
          const flatList = displayList(filteredEnabled.value, 'enabled', enabledSearch.value);
          const insertAt = Math.max(0, Math.min(toFlat, flatList.length));
          // 找 placeholder 右侧（被插入位置）的真实 canonical
          for (let i = insertAt; i < flatList.length; i++) {
            if (flatList[i] && !flatList[i].__ph) { insertCid = flatList[i].id; break; }
          }
        }
        let targetCanonicalIdx;
        if (insertCid) {
          targetCanonicalIdx = enabledArr.findIndex(c => c.id === insertCid);
        } else {
          targetCanonicalIdx = enabledArr.length; // 追加到末尾
        }
        // 后端：先启用再重排（必须传 version_key 精确操作物理副本）
        await api().set_enabled(fromModId, true, null, fromMod.__version_key);
        const newState = await api().get_state();
        applyState(newState);
        // applyState 后从新的 state.enabled 里找到刚启用的 mod
        const freshEnabled = [...state.enabled];
        const justEnabledIdx = freshEnabled.findIndex(c => c.id === fromModId);
        // workshop 副本和 local 副本用不同 toast
        const dragIsWs = !!fromMod.workshop_id;
        const dragEnableKey = dragIsWs ? 'enabled_toast_workshop' : 'enabled_toast';
        const dragDisableKey = dragIsWs ? 'disabled_toast_workshop' : 'disabled_toast';
        if (justEnabledIdx >= 0 && justEnabledIdx !== targetCanonicalIdx) {
          const moved = freshEnabled.splice(justEnabledIdx, 1)[0];
          let finalTo = targetCanonicalIdx;
          if (justEnabledIdx < targetCanonicalIdx) finalTo--;
          finalTo = Math.max(0, Math.min(finalTo, freshEnabled.length));
          freshEnabled.splice(finalTo, 0, moved);
          state.enabled = freshEnabled;
          await api().reorder_enabled(freshEnabled.map(m => m.id));
          _localRecheckDeps();
          showToast(t(dragEnableKey, { name: fromMod?.name || fromModId }), 'success');
        } else {
          showToast(t(dragEnableKey, { name: fromMod?.name || fromModId }), 'success');
        }
        return;
      }

      if (fromList === 'enabled' && targetList === 'disabled') {
        // —— B) 停用（必须传 version_key 精确操作物理副本）——
        const dragIsWs = !!fromMod.workshop_id;
        const dragDisableKey = dragIsWs ? 'disabled_toast_workshop' : 'disabled_toast';
        await api().set_enabled(fromModId, false, null, fromMod.__version_key);
        const newState = await api().get_state();
        applyState(newState);
        showToast(t(dragDisableKey, { name: fromMod?.name || fromModId }), 'success');
        return;
      }

      if (fromList !== 'enabled' || targetList !== 'enabled') {
        // —— D) disabled→disabled 或 其它非排序情况 ——
        return;
      }

      // —— C) enabled → enabled 内部排序（现有逻辑，完整保留）——
      if (fromFlat < 0 || toFlat < 0) return;
      if (fromFlat === toFlat) return;

      const enabledArr = [...state.enabled];
      const fromCanonicalIdx = enabledArr.findIndex(c => c.id === fromModId);
      if (fromCanonicalIdx < 0) return;

      const flatList = displayList(filteredEnabled.value, 'enabled', enabledSearch.value);
      const insertAt = Math.max(0, Math.min(toFlat, flatList.length));

      let leftCid = null;
      let rightCid = null;
      for (let i = insertAt - 1; i >= 0; i--) {
        if (flatList[i] && !flatList[i].__ph) { leftCid = flatList[i].id; break; }
      }
      for (let i = insertAt; i < flatList.length; i++) {
        if (flatList[i] && !flatList[i].__ph) { rightCid = flatList[i].id; break; }
      }

      if (leftCid === fromModId && rightCid === fromModId) return;
      if (leftCid === rightCid && leftCid !== null) return;

      let toCanonicalIdx;
      if (rightCid) {
        toCanonicalIdx = enabledArr.findIndex(c => c.id === rightCid);
      } else if (leftCid) {
        const leftIdx = enabledArr.findIndex(c => c.id === leftCid);
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

      if (selected.value && fromMod && fromMod.__version_key === selected.value.__version_key) {
        requestAnimationFrame(() => {
          const newList = displayList(filteredEnabled.value, 'enabled', enabledSearch.value);
          const found = newList.find(x => x.__version_key === fromMod.__version_key);
          if (found) selected.value = found;
        });
      }

      if (enabledSearch.value.trim()) {
        showToast('已在当前搜索范围内重排', 'success');
      }

      api().reorder_enabled(enabledArr.map(m => m.id)).then(() => {
        api().get_state().then(s => applyState(s)).catch(() => {});
      }).catch(err => {
        console.error('[drag] reorder failed:', err);
        showToast('排序保存失败，请检查后端日志', 'error');
        api().get_state().then(s => applyState(s)).catch(() => {});
      });
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
          // 防御性去重：如果 canonical 有重复，versions 也会被重复展开
          if (seenKeys.has(v.key)) continue;
          seenKeys.add(v.key);
          result.push({
            ...m,
            // 2026-11-01：每个副本独立显示名（workshop title > mod.json name）
            // 同 mod_id 多 workshop 副本（如圣主历险记原版 vs 修复版）各自显示 Steam 上的真实标题
            name: v.name || m.name,
            __version_key: v.key,
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
      // 合并两侧 canonical 全部拉平
      const allFlat = expandModList([...state.enabled, ...state.disabled]);
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
      // 启用侧：保持 canonical 在 state.enabled 的顺序（load order 有意义）
      if (side === 'enabled') {
        const order = state.enabled.map(m => m.id.toLowerCase());
        filtered.sort((a, b) => {
          const ai = order.indexOf(a.id.toLowerCase());
          const bi = order.indexOf(b.id.toLowerCase());
          if (ai === -1 && bi === -1) return 0;
          if (ai === -1) return 1;
          if (bi === -1) return -1;
          return ai - bi;
        });
      } else {
        // 停用侧：按 last_toggled_at 倒序（从新到旧）
        // ts > 0 = 本次 GUI 会话刚 toggle 过（刚停的）→ 排前面
        // ts = 0 = settings.save 加载的初始状态（早停的）→ 保持原始位置不动
        // 关键：ts 相等时返回 0 让稳定排序保持 filtered 原始顺序
        //       这样 ts=0 的 flat 副本保持它们在 settings.save 里的顺序
        //       同 canonical 的两个 flat 因为 ts 不同（一个刚停 ts>0，一个早停 ts=0）→ 自然分开
        filtered.sort((a, b) => {
          const ta = a.last_toggled_at || 0;
          const tb = b.last_toggled_at || 0;
          if (tb !== ta) return tb - ta;
          return 0;  // 稳定排序：保持 filtered 原始顺序
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

    async function menuToggle() {
      menu.show = false;
      if (multiSelectedCount.value > 1) {
        // 多选批量模式：只对本地副本生效，跳过 workshop（和后端 store_mods.py 批量模式一致）
        const targetEnable = !menu.mod.is_enabled;
        const allFlat = expandModList([...state.enabled, ...state.disabled]);
        const targets = allFlat.filter(m => _multiHas(m.__version_key) && !m.workshop_id);
        let okCount = 0;
        for (const m of targets) {
          try {
            await api().set_enabled(m.id, targetEnable, null, m.__version_key);
            okCount++;
          } catch (err) {
            console.warn('[menuToggle batch] 单个失败:', m.id, err);
          }
        }
        const s = await api().get_state();
        applyState(s);
        _multiClear();
        const verb = targetEnable ? t('batch_enable') : t('batch_disable');
        showToast(`${verb}: ${okCount}/${targets.length}`, okCount === targets.length ? 'success' : 'warning');
      } else {
        // 单副本模式（原逻辑）
        const m = menu.mod;
        const enable = !m.is_enabled;
        const s = await api().set_enabled(m.id, enable, null, m.__version_key);
        applyState(s);
        const isWs = !!m.workshop_id;
        const toastKey = enable
          ? (isWs ? 'enabled_toast_workshop' : 'enabled_toast')
          : (isWs ? 'disabled_toast_workshop' : 'disabled_toast');
        showToast(t(toastKey, { name: m.name || m.id }), 'success');
      }
    }

    async function refreshWsPreview() {
      const m = menu.mod;
      menu.show = false;
      if (!m || !m.workshop_id) return;
      try {
        const r = await api().refresh_workshop_preview(m.id);
        if (r && r.ok) {
          // 后端已删完两层缓存并立刻重抓了一次。无论当前选中没选中这个 mod，
          // 都主动触发右侧预览面板刷新——让用户立刻看到新数据。
          const d = await api().get_workshop_details(m.id);
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
      await api().open_folder(m.id, m.install_path || null);
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
      const s = await api().set_settings(
        settings.form.manual_game_root || null,
        settings.form.manual_mods_dirs || null,
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
      if (r && r.path) settings.form[field] = r.path;
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
      const selId = (selected.value && list.some(x => x.__version_key === selected.value.__version_key))
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

    return {
      state, breakdown: computed(() => state.breakdown), selected, multiSelected, multiSelectedCount, batchSetEnabled, enabledSearch, disabledSearch, launchText, depLayer, depHeight, depStyle,
      menuEl, packMenuEl,
      toasts, pauseToast, resumeToast, dismissToast, dllProgress, bulkProgress, scanProgress,
      filteredEnabled, filteredDisabled, selectedMeta, selectedDesc, selectedLocalTags, selectedManagerTags, selectedTagHits, selectedTags,
      canFetchTranslation, translationHint, translationBusy, fetchCommunityTranslation,
      effectiveDescription, effectivePreview, effectiveTitle, effectiveAuthor,
      subText, tooltip, depWarnTitle, versionLabel,
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
      settingsTab, workshopUpdates, appInfo, checkWorkshopUpdates, replacementSuggestions, replacementChecking, findReplacementMods, steamdbExport, exportSteamdb, mpCompat, checkMpCompatibility, setMpOverride, mpStatusLabel, clearWorkshopCache, clearWorkshopCacheAll, clearDllCache, analyzeDlls, openWorkshopItem,
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
