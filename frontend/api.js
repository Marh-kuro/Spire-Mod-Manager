// ==========================================================================
// api.js — pywebview API 桥 + mock API（浏览器预览用）
// 从 app.js 拆分（2026-10-14 O4 重构）
// ==========================================================================

// pywebview 在页面加载完成后才注入 window.pywebview 并派发 pywebviewready
let pywebviewReady = false;
window.addEventListener('pywebviewready', () => {
  pywebviewReady = true;
});

function hasPywebviewApi() {
  return !!(window.pywebview && window.pywebview.api);
}

// pywebviewready 后绑定全局 JS 错误回写（让后端日志也能看到前端报错）
window.addEventListener('pywebviewready', () => {
  window.onerror = function(msg, url, line, col, err) {
    if (hasPywebviewApi()) {
      try { window.pywebview.api.log_js_error(String(msg||''), String(url||''), line||0, col||0, err && err.stack || ''); } catch(_) {}
    }
  };
  window.addEventListener('unhandledrejection', function(e) {
    if (hasPywebviewApi()) {
      try {
        const r = e.reason || {};
        window.pywebview.api.log_js_error(
          '[unhandledrejection] ' + (r.message || String(e)),
          '', 0, 0,
          r.stack || e && e.reason && e.reason.stack || ''
        );
      } catch(_) {}
    }
  });
});

// ==========================================================================
// 浏览器预览 mock API（pywebview 不可用时 fallback）
// ==========================================================================

const MOCK_STATE = {
  enabled: [
    { id: 'baselib', name: 'BaseLib', author: '作者A', version: 'v1.2.3', type: 'framework', type_label: '框架', source: 'steam_workshop', source_label: '工坊', workshop_id: '3748583469', folder: '3748583469', path: 'H:/SteamLibrary/steamapps/workshop/content/2868840/3748583469', workshop_url: 'https://steamcommunity.com/sharedfiles/filedetails/?id=3748583469', has_pck: true, has_dll: false, preview: null, icon: null, banner: null, character: null, game_version: null, min_game_version: null, max_game_version: null, last_activity: 0, manager_tags: ['framework'], dependencies: [], dep_flags: 0, missing_deps: [], version_key: '3748583469', external_source: null, install_dir: 'H:/SteamLibrary/steamapps/workshop/content/2868840/3748583469', display_name: 'BaseLib', mod_id: 'baselib', mod_type: 'framework', mod_type_icon: '⚙️', source_icon: '🏪', mod_tags: ['framework', 'utility'], card_roles: [], groups: [], group_order: [], mod_meta: { alias: '', note: '' }, mod_colors: '', pinned: false, is_enabled: true, version: 'v1.2.3' },
  ],
  disabled: [],
  packs: [],
  backups: [],
  workshop_updated: 0,
  workshop_total: 0,
  workshop_online: 0,
  workshop_valid: 0,
  workshop_deprecated: 0,
  workshop_subscribed: 0,
  game_version: '1.0.0',
  beta_key: '',
  beta_name: '稳定版',
  beta_switching: false,
  manual_game_root: '',
  manual_mods_dirs: [],
  manual_workshop_dir: '',
  manual_mods_dir: '',
  manual_save_dir: '',
  prefer_better_mod_menu: false,
  manager_tags: ['framework', 'utility', 'skin'],
  language: 'zh',
  auto_check_steam: true,
  last_activity: {},
  auto_backup: false,
  keep_versions: 5,
  backup_dir: null,
  version: 1,
  error: null,
  dep_warnings: [],
  game_root: '',
  mods_dir: '',
  workshop_dir: '',
  save_dir: '',
  has_bmm: false,
  scan_errors: [],
  scan_warnings: [],
  has_skin_manager: false,
  character_mods: [],
  skin_choices: {},
  profile: 'default',
  profiles: [{ name: 'default', mod_count: 1, enabled_count: 1, updated_at: 0 }],
  active_profile: 'default',
  pinned_mods: [],
  groups: [],
};

function makeMockApi() {
  const state = JSON.parse(JSON.stringify(MOCK_STATE));
  return {
    async get_state() { return state; },
    async get_skin_state() { return { skins: [], characters: {} }; },
    async refresh(...args) { return { ok: true, progress: 100 }; },
    async scan_all(...args) { return { ok: true, progress: 100 }; },
    async set_enabled(mod_id, enabled) { return { ok: true }; },
    async toggle_enabled(mod_id) { return { ok: true, enabled: true }; },
    async reorder_enabled(ids) { return { ok: true }; },
    async delete_mod(mod_id) { return { ok: true }; },
    async get_workshop_preview(mod_id) { return null; },
    async get_workshop_tags() { return []; },
    async clear_workshop_cache() { return 0; },
    async get_workshop_titles_pending() {
      if (hasPywebviewApi()) {
        try { return await window.pywebview.api.get_workshop_titles_pending(); } catch(_) {}
      }
      return { ok: true, pending: 0, running: false };
    },
    async get_workshop_bg_progress() {
      if (hasPywebviewApi()) {
        try { return await window.pywebview.api.get_workshop_bg_progress(); } catch(_) {}
      }
      return { ok: true, phase1: {done: 0, total: 0, running: false, ok: false},
               phase2: {done: 0, total: 0, running: false, ok: false}, running: false };
    },
    async refresh_workshop_preview(mod_id) { return { ok: true, title: null, workshop_id: null, deleted: {} }; },
    async set_mod_tags(mod_id, tags) { return null; },
    async set_mod_meta(mod_id, alias, note) { return null; },
    async set_mod_color(mod_id, color) { return null; },
    async set_language(lang) { return null; },
    async set_auto_check_steam(enabled) { return null; },
    async set_better_mod_menu(enabled) { return null; },
    async set_manual_paths(paths) { return null; },
    async get_dependency_warnings() { return []; },
    async get_skin_choices() { return {}; },
    async save_skin_choices(choices) { return null; },
    async open_mod_folder(mod_id) { return null; },
    async open_game_root() { return null; },
    async launch_game(force) { return { ok: true }; },
    async close_game() { return null; },
    async get_game_status() { return { running: false }; },
    async get_log_tail() { return ''; },
    async clear_log() { return null; },
    async get_profile_list() { return [{ name: 'default', mod_count: 1, enabled_count: 1, updated_at: Date.now() }]; },
    async switch_profile(name) { return { ok: true }; },
    async create_profile(name) { return { ok: true }; },
    async rename_profile(old, name) { return { ok: true }; },
    async delete_profile(name) { return { ok: true }; },
    async get_save_slots() { return []; },
    async backup_save(slot) { return { ok: true }; },
    async restore_save(slot) { return { ok: true }; },
    async list_save_backups(slot) { return []; },
    async delete_save_backup(slot, file) { return { ok: true }; },
    async get_backup_list(mod_id) { return []; },
    async create_backup(mod_id) { return { ok: true }; },
    async restore_backup(mod_id, file, target) { return { ok: true }; },
    async delete_backup(mod_id, file) { return { ok: true }; },
    async clear_duplicates() { return { ok: true, removed: 0 }; },
    async export_mods(fmt) { return ''; },
    async get_steam_branches() { return { ok: true, branches: [{ key: 'public', name: '稳定版', current: true }] }; },
    async switch_branch(key) { return { ok: true }; },
    async get_console_commands() { return []; },
    async get_autostart() { return false; },
    async set_autostart(enabled) { return null; },
    async log_js_error(msg, url, line, col, stack) { return null; },
  };
}

const mockApi = makeMockApi();
