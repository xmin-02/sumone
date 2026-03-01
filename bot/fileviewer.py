"""Read-only HTTP file viewer for modified files with diff and rollback."""
import difflib
import html
import json
import mimetypes
import os
import secrets
import socket
import threading
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from state import state
from config import SCRIPT_DIR, log

_SNAPSHOTS_DIR = os.path.join(SCRIPT_DIR, ".snapshots")

# ---------------------------------------------------------------------------
# Token management with TTL support
# ---------------------------------------------------------------------------
import time as _time

_tokens = {}         # token -> expiry_timestamp (0 = never expires)
_token_lock = threading.Lock()

# Settings-specific one-time access tokens (separate from file viewer tokens)
_settings_tokens = {}
_settings_token_lock = threading.Lock()


def _get_token_ttl_seconds():
    """Read token TTL from settings. Returns seconds or None (no expiry)."""
    from config import settings as _settings
    ttl = _settings.get("token_ttl", "session")
    if ttl == "unlimited":
        return None  # never expires
    if ttl == "session":
        return 0  # special: valid until clear_tokens() is called
    try:
        minutes = int(ttl)
        if 1 <= minutes <= 60:
            return minutes * 60
    except (ValueError, TypeError):
        pass
    return 0  # fallback to session


def generate_token():
    """Generate an access token with TTL from settings."""
    token = secrets.token_urlsafe(32)
    ttl_sec = _get_token_ttl_seconds()
    with _token_lock:
        if ttl_sec is None or ttl_sec == 0:
            _tokens[token] = 0  # 0 = no time expiry (session or unlimited)
        else:
            _tokens[token] = _time.time() + ttl_sec
    return token


def get_or_create_fixed_token():
    """Return a persistent fixed token stored in config.json. Creates one if absent."""
    import json as _json
    from config import CONFIG_FILE
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = _json.load(f)
    except Exception:
        cfg = {}
    token = cfg.get("viewer_fixed_token")
    if not token:
        token = secrets.token_urlsafe(32)
        cfg["viewer_fixed_token"] = token
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, indent=4, ensure_ascii=False)
        except Exception:
            pass
    # Ensure token is registered (no expiry)
    with _token_lock:
        _tokens[token] = 0
    return token


def _validate_token(token):
    """Validate token. Returns True if valid and not expired."""
    with _token_lock:
        expiry = _tokens.get(token)
        if expiry is None:
            return False
        if expiry == 0:
            return True  # session/unlimited - no time expiry
        if _time.time() > expiry:
            del _tokens[token]
            return False
        return True


def clear_tokens():
    """Invalidate all access tokens (called on bot restart)."""
    with _token_lock:
        _tokens.clear()
    _ViewerHandler.session_tokens.clear()


def generate_settings_token():
    """Generate a one-time settings page access token (separate from file viewer)."""
    token = secrets.token_urlsafe(32)
    with _settings_token_lock:
        _settings_tokens[token] = True
    return token


def _validate_settings_token(token):
    """Validate and consume a settings token (one-time use). Returns True if valid."""
    with _settings_token_lock:
        return _settings_tokens.pop(token, None) is not None


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------
_CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".html", ".css", ".scss",
    ".md", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash",
    ".ps1", ".bat", ".cmd", ".xml", ".svg", ".sql", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".lua", ".vim",
    ".gitignore", ".env", ".editorconfig", ".dockerignore", "Dockerfile",
    ".txt", ".log", ".csv",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}

# Extension → highlight.js language mapping
_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".json": "json",
    ".html": "xml", ".css": "css", ".scss": "scss",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "ini", ".sh": "bash", ".bash": "bash",
    ".xml": "xml", ".sql": "sql", ".go": "go",
    ".rs": "rust", ".java": "java", ".c": "c",
    ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".scala": "scala", ".lua": "lua",
    ".pl": "perl", ".ini": "ini", ".cfg": "ini",
    ".conf": "ini", ".log": "plaintext", ".txt": "plaintext",
    ".vue": "xml", ".svelte": "xml", ".r": "r",
    ".dockerfile": "dockerfile", ".graphql": "graphql",
}


def _get_lang(path):
    """Get highlight.js language class from file path."""
    _, ext = os.path.splitext(path)
    lang = _EXT_TO_LANG.get(ext.lower(), "")
    basename = os.path.basename(path).lower()
    if not lang:
        if basename == "dockerfile":
            lang = "dockerfile"
        elif basename == "makefile":
            lang = "makefile"
    return lang


def _file_type(path):
    """Return 'code', 'image', or 'other'."""
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    if ext in _CODE_EXTS:
        return "code"
    if ext in _IMAGE_EXTS:
        return "image"
    basename = os.path.basename(path)
    if basename in ("Dockerfile", "Makefile", "Gemfile", "Rakefile", ".gitignore"):
        return "code"
    return "other"


def _human_size(size):
    """Format bytes to human readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _format_ts(iso_ts):
    """Format ISO timestamp to 'YY.MM.DD HH:MM:SS'."""
    try:
        date_part, time_part = iso_ts.split("T")
        y, m, d = date_part.split("-")
        return f"{y[2:]}.{m}.{d} {time_part}"
    except Exception:
        return iso_ts


def _format_date(iso_ts):
    """Format ISO timestamp to 'YY.MM.DD'."""
    try:
        date_part = iso_ts.split("T")[0]
        y, m, d = date_part.split("-")
        return f"{y[2:]}.{m}.{d}"
    except Exception:
        return iso_ts


def _aggregate_files(entries):
    """Aggregate entries by path. Returns list of {path, latest_ts, history}.
    Hides rollback-backup entries from the UI (internal backups)."""
    file_map = defaultdict(list)
    for entry in entries:
        file_map[entry["path"]].append(entry)
    result = []
    for path, hist in file_map.items():
        # Filter out rollback-backup entries (internal, noisy in UI)
        visible_hist = [e for e in hist if e.get("op") != "rollback-backup"]
        if not visible_hist:
            continue
        hist_sorted = sorted(visible_hist, key=lambda e: e["ts"], reverse=True)
        result.append({
            "path": path,
            "latest_ts": hist_sorted[0]["ts"],
            "history": hist_sorted,
        })
    result.sort(key=lambda x: x["latest_ts"], reverse=True)
    return result


def _read_snapshot(snapshot_name):
    """Read snapshot content as text. Returns None on failure."""
    if not snapshot_name:
        return None
    p = os.path.join(_SNAPSHOTS_DIR, snapshot_name)
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Viewer i18n (embedded for client-side switching)
# ---------------------------------------------------------------------------
_VIEWER_I18N = {
    "ko": {
        "title": "수정된 파일", "read_only": "읽기 전용",
        "clear": "삭제", "rollback": "롤백", "diff_tool": "Diff",
        "back_list": "\u2190 목록", "download": "\u2b07 다운로드", "copy": "복사", "copied": "복사됨!",
        "search_ph": "파일 검색...", "search_name": "파일명", "search_path": "경로",
        "sort_time": "시간순", "sort_name": "이름순", "sort_type": "타입순",
        "op_write": "작성됨", "op_edit": "수정됨", "op_delete": "삭제됨",
        "op_rollback": "롤백됨", "op_rb_backup": "백업됨",
        "ts_fmt": "{ts}에 {op}", "mods": "건의 수정", "cur_only": "현재만",
        "snapshot": "스냅샷", "cycle_rb": "시점 복원",
        "cycle_desc": "복원할 시점을 선택하세요. 선택한 사이클의 상태로 모든 파일이 복원됩니다.",
        "cancel": "취소", "files_unit": "파일",
        "lines_hidden": "줄 숨김", "no_diff": "차이가 없습니다.",
        "del_title": "삭제됨", "del_msg": "이 파일은 삭제되었습니다.",
        "del_hint": "파일 목록의 히스토리 드롭다운에서 이전 스냅샷을 확인하세요.",
        "no_preview": "이 파일 유형은 미리보기를 지원하지 않습니다.\n위의 다운로드 버튼을 사용하세요.",
        "cfm_clear": "모든 파일 히스토리를 삭제하시겠습니까?\\n이 작업은 되돌릴 수 없습니다.",
        "cfm_rb": "이 스냅샷으로 파일을 롤백하시겠습니까?\\n현재 파일은 먼저 백업됩니다.",
        "cfm_cycle": "이 시점으로 모든 파일을 복원하시겠습니까?\\n현재 파일은 먼저 백업됩니다.",
        "rb_done": "롤백 완료!", "cycle_done": "시점 복원 완료!", "same_state": "현재와 동일한 시점입니다.",
        "failed": "실패: ", "req_fail": "요청 실패.",
        "dt_title": "Diff 비교 도구", "dt_select": "파일 선택",
        "dt_hint": "스냅샷이 2개 이상인 파일만 표시됩니다",
        "dt_left": "좌측 (이전)", "dt_right": "우측 (이후)",
        "dt_ordered": "자동 시간순 정렬됨", "dt_no_files": "비교 가능한 파일이 없습니다.",
        "dt_select_both": "양쪽 스냅샷을 선택하세요.",
        "s_disp": "디스플레이", "s_appear": "화면 설정", "s_storage": "저장소", "s_ai": "AI 모델",
        "s_cost": "비용 표시", "s_cost_d": "응답 후 API 비용 표시",
        "s_status": "작업 상태 메시지", "s_status_d": "처리 중 도구 사용 상태 표시",
        "s_global": "전체 비용 표시", "s_global_d": "/cost 명령어에서 전체 비용 표시",
        "s_remote": "다른 PC 토큰 합산", "s_remote_d": "footer에 원격 봇 토큰 합산",
        "s_tperiod": "토큰 표시 범위", "s_tperiod_d": "footer에 표시할 토큰 기간",
        "s_theme": "테마", "s_theme_d": "파일 뷰어 색상 테마",
        "s_snap": "스냅샷 보관 기간", "s_snap_d": "파일 스냅샷 보관 일수",
        "s_tttl": "뷰어 토큰 수명", "s_tttl_d": "파일 뷰어 링크 만료 시간",
        "s_tmin": "토큰 수명 (분)", "s_tmin_d": "1~60분",
        "s_aimod": "기본 AI", "s_aimod_d": "새 세션의 AI 제공자",
        "s_sub": "기본 서브 모델", "s_sub_d": "특정 모델 버전",
        "s_ttl_sess": "세션 (봇 수명)", "s_ttl_unltd": "무제한", "s_ttl_mins": "분 단위",
        "s_autolink": "파일 뷰어 링크 자동 전송", "s_autolink_d": "파일 수정 시 뷰어 링크 자동 전송",
        "s_fixedlink": "링크 고정", "s_fixedlink_d": "매번 같은 URL 사용 (북마크 가능)",
        "s_typing": "타이핑 인디케이터", "s_typing_d": "응답 중 '···' 메시지 표시",
        "s_botlang": "봇 언어", "s_botlang_d": "봇 응답 및 메시지 언어 (즉시 적용)",
        "s_system": "시스템",
        "s_workdir": "작업 디렉토리", "s_workdir_d": "파일 탐색 루트 경로",
        "s_stimeout": "설정 페이지 타임아웃", "s_stimeout_d": "비활성 후 자동 만료 시간 (분)",
        "s_save": "저장", "s_saving": "저장 중...", "s_saved": "✔ 저장됨", "s_restarting": "♻ 재시작 중...", "s_expired": "세션이 만료되었습니다. 창을 닫습니다.",
        "s_ai": "AI 모델", "s_ai_set": "설정하기", "s_ai_active": "설정됨", "s_ai_connect": "연결하기", "s_ai_connect_started": "연결을 시작합니다. 텔레그램을 확인하세요.",
    },
    "en": {
        "title": "Modified Files", "read_only": "Read-only view",
        "clear": "Clear", "rollback": "Rollback", "diff_tool": "Diff",
        "back_list": "\u2190 List", "download": "\u2b07 Download", "copy": "Copy", "copied": "Copied!",
        "search_ph": "Search files...", "search_name": "Filename", "search_path": "Path",
        "sort_time": "Time", "sort_name": "Name", "sort_type": "Type",
        "op_write": "Created", "op_edit": "Modified", "op_delete": "Deleted",
        "op_rollback": "Rolled back", "op_rb_backup": "Backed up",
        "ts_fmt": "{op} at {ts}", "mods": "modification(s)", "cur_only": "current only",
        "snapshot": "snapshot", "cycle_rb": "Restore to Point",
        "cycle_desc": "Select a cycle to restore to. All files will be restored to their state at that point.",
        "cancel": "Cancel", "files_unit": "files",
        "lines_hidden": "lines hidden", "no_diff": "No differences found.",
        "del_title": "Deleted", "del_msg": "This file has been deleted.",
        "del_hint": "Check the history dropdown on the file list for previous snapshots.",
        "no_preview": "Preview not available for this file type.\nUse the download button above.",
        "cfm_clear": "Clear all file history?\\nThis cannot be undone.",
        "cfm_rb": "Rollback to this snapshot?\\nCurrent file will be backed up first.",
        "cfm_cycle": "Restore all files to this point?\\nAll current files will be backed up first.",
        "rb_done": "Rollback complete!", "cycle_done": "Restore complete!", "same_state": "Already at this point.",
        "failed": "Failed: ", "req_fail": "Request failed.",
        "dt_title": "Diff Compare Tool", "dt_select": "Select file",
        "dt_hint": "Only files with 2+ snapshots shown",
        "dt_left": "Left (older)", "dt_right": "Right (newer)",
        "dt_ordered": "Auto time-ordered", "dt_no_files": "No files for comparison.",
        "dt_select_both": "Select both snapshots to compare.",
        "s_disp": "Display", "s_appear": "Appearance", "s_storage": "Storage", "s_ai": "AI Model",
        "s_cost": "Show Cost", "s_cost_d": "Show API cost after each response",
        "s_status": "Show Status", "s_status_d": "Show tool usage status during processing",
        "s_global": "Show Global Cost", "s_global_d": "Show total cost in /cost command",
        "s_remote": "Show Remote Tokens", "s_remote_d": "Include remote bot tokens in footer",
        "s_tperiod": "Token Display Period", "s_tperiod_d": "Token count period shown in footer",
        "s_theme": "Theme", "s_theme_d": "File viewer color theme",
        "s_snap": "Snapshot Retention", "s_snap_d": "Days to keep file snapshots",
        "s_tttl": "Viewer Token TTL", "s_tttl_d": "File viewer link expiry",
        "s_tmin": "Token TTL (minutes)", "s_tmin_d": "1-60 minutes",
        "s_aimod": "Default AI", "s_aimod_d": "AI provider for new sessions",
        "s_sub": "Default Sub-Model", "s_sub_d": "Specific model variant",
        "s_ttl_sess": "Session (bot lifetime)", "s_ttl_unltd": "Unlimited", "s_ttl_mins": "Minutes",
        "s_autolink": "Auto Viewer Link", "s_autolink_d": "Send file viewer link automatically when files change",
        "s_fixedlink": "Fixed Link", "s_fixedlink_d": "Reuse same URL every time (bookmarkable)",
        "s_typing": "Typing Indicator", "s_typing_d": "Show '···' message while processing",
        "s_botlang": "Bot Language", "s_botlang_d": "Bot response and message language (applied immediately)",
        "s_system": "System",
        "s_workdir": "Work Directory", "s_workdir_d": "Root directory for file browsing",
        "s_stimeout": "Settings Page Timeout", "s_stimeout_d": "Auto-expire after N minutes of inactivity",
        "s_save": "Save", "s_saving": "Saving...", "s_saved": "\u2714 Saved", "s_restarting": "\u267b Restarting...", "s_expired": "Session expired. Closing window.",
        "s_ai": "AI Model", "s_ai_set": "Set", "s_ai_active": "Active", "s_ai_connect": "Connect", "s_ai_connect_started": "Connection started. Check Telegram.",
    },
}

_VIEWER_I18N_JSON = json.dumps(_VIEWER_I18N, ensure_ascii=False)

# Human-readable setting names for Telegram change notification
_SETTING_NAMES = {
    "ko": {
        "show_cost": "비용 표시", "show_status": "작업 상태 메시지",
        "show_global_cost": "전체 비용 표시", "show_remote_tokens": "다른 PC 토큰 합산",
        "token_display": "토큰 표시 범위", "theme": "테마",
        "snapshot_ttl_days": "스냅샷 보관 기간", "token_ttl": "뷰어 토큰 수명",
        "auto_viewer_link": "파일 뷰어 링크 자동 전송", "viewer_link_fixed": "링크 고정",
        "show_typing": "타이핑 인디케이터",
        "default_model": "기본 AI", "default_sub_model": "기본 서브 모델",
        "bot_lang": "봇 언어",
        "work_dir": "작업 디렉토리", "settings_timeout_minutes": "설정 페이지 타임아웃",
    },
    "en": {
        "show_cost": "Show Cost", "show_status": "Show Status",
        "show_global_cost": "Show Global Cost", "show_remote_tokens": "Show Remote Tokens",
        "token_display": "Token Display Period", "theme": "Theme",
        "snapshot_ttl_days": "Snapshot Retention", "token_ttl": "Viewer Token TTL",
        "auto_viewer_link": "Auto Viewer Link", "viewer_link_fixed": "Fixed Link",
        "show_typing": "Typing Indicator",
        "default_model": "Default AI", "default_sub_model": "Default Sub-Model",
        "bot_lang": "Bot Language",
        "work_dir": "Work Directory", "settings_timeout_minutes": "Settings Page Timeout",
    },
}


# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------
_CSS = """
:root {
  --bg-base: #0d1117; --bg-raised: #161b22; --bg-overlay: #1c2128;
  --border-muted: #21262d; --border-default: #30363d;
  --text-primary: #c9d1d9; --text-secondary: #8b949e; --text-muted: #484f58;
  --accent-blue: #58a6ff; --accent-blue-hover: #79c0ff;
  --color-add: #3fb950; --color-del: #f85149; --color-warn: #d29922; --color-special: #a371f7;
  --text-xs: 0.75rem; --text-sm: 0.85rem; --text-base: 1rem; --text-lg: 1.2rem;
  --font-ui: -apple-system, BlinkMacSystemFont, 'Pretendard', 'Noto Sans KR', sans-serif;
  --font-mono: 'JetBrains Mono', 'Consolas', 'Monaco', 'Courier New', monospace;
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 20px; --space-6: 24px;
  --radius-sm: 4px; --radius-md: 6px; --radius-lg: 8px; --radius-xl: 10px;
  --code-font-size: 0.83rem; --code-line-height: 1.6;
}
[data-theme="light"] {
  --bg-base: #ffffff; --bg-raised: #f6f8fa; --bg-overlay: #eef1f5;
  --border-muted: #d0d7de; --border-default: #d0d7de;
  --text-primary: #1f2328; --text-secondary: #656d76; --text-muted: #8b949e;
  --accent-blue: #0969da; --accent-blue-hover: #0550ae;
  --color-add: #1a7f37; --color-del: #cf222e; --color-warn: #9a6700; --color-special: #8250df;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: var(--font-ui);
       background: var(--bg-base); color: var(--text-primary); line-height: 1.6; }
.container { max-width: 900px; margin: 0 auto; padding: 20px; }
h1 { color: var(--accent-blue); margin-bottom: 5px; font-size: 1.3em; }
.subtitle { color: var(--text-secondary); font-size: var(--text-sm); margin-bottom: 20px; }
.separator { border: none; border-top: 1px solid var(--border-muted); margin: 15px 0; }
.footer { color: var(--text-muted); font-size: var(--text-xs); margin-top: 25px; text-align: center; }

/* Header row */
.header-row { display: flex; align-items: center; justify-content: space-between; }
.header-btns { display: flex; gap: 8px; }

/* Unified button system */
.outline-btn { background: transparent; padding: 5px 12px; border-radius: var(--radius-md);
  font-size: var(--text-xs); cursor: pointer; transition: background 0.15s ease, color 0.15s ease;
  text-decoration: none; white-space: nowrap; border: 1px solid currentColor; }
.outline-btn--danger { color: var(--color-del); }
.outline-btn--warn { color: var(--color-warn); }
.outline-btn--primary { color: var(--accent-blue); }
.outline-btn--danger:hover { background: var(--color-del); color: #fff; }
.outline-btn--warn:hover { background: var(--color-warn); color: #fff; }
.outline-btn--primary:hover { background: var(--accent-blue); color: #fff; }

/* Backward compat aliases */
.clear-btn { background: transparent; border: 1px solid var(--color-del); color: var(--color-del);
             padding: 5px 12px; border-radius: var(--radius-md); font-size: var(--text-xs); cursor: pointer;
             transition: background 0.15s ease, color 0.15s ease; }
.clear-btn:hover { background: var(--color-del); color: #fff; }
.rollback-btn { background: transparent; border: 1px solid var(--color-warn); color: var(--color-warn);
                padding: 5px 12px; border-radius: var(--radius-md); font-size: var(--text-xs); cursor: pointer;
                transition: background 0.15s ease, color 0.15s ease; }
.rollback-btn:hover { background: var(--color-warn); color: #fff; }

/* Collapsible date group (outer) */
details.date-group { margin-bottom: 14px; }
details.date-group > summary { cursor: pointer; color: var(--accent-blue); font-size: 0.9em;
    padding: 6px 8px; border-radius: var(--radius-md); list-style: none; user-select: none; font-weight: 500; }
details.date-group > summary::-webkit-details-marker { display: none; }
details.date-group > summary::before { content: '\\25B6 '; font-size: 0.7em; margin-right: 6px;
    display: inline-block; transition: transform 0.2s; }
details.date-group[open] > summary::before { transform: rotate(90deg); }
details.date-group > summary:hover { background: var(--bg-raised); }

/* Collapsible directory group (inner) */
details.dir-group { margin: 4px 0 8px 12px; }
details.dir-group > summary { cursor: pointer; color: var(--text-secondary); font-size: 0.82em;
    padding: 4px 6px; border-radius: var(--radius-sm); list-style: none; user-select: none; }
details.dir-group > summary::-webkit-details-marker { display: none; }
details.dir-group > summary::before { content: '\\25B6 '; font-size: 0.6em; margin-right: 5px;
    display: inline-block; transition: transform 0.2s; }
details.dir-group[open] > summary::before { transform: rotate(90deg); }
details.dir-group > summary:hover { background: var(--bg-raised); }

/* File row */
.file-row { display: flex; align-items: center; padding: 8px 12px;
            border: 1px solid var(--border-muted); border-radius: var(--radius-md); margin: 3px 0 0 0;
            background: var(--bg-raised); transition: border-color 0.2s, background 0.2s; cursor: pointer; }
.file-row:hover { border-color: var(--accent-blue); background: var(--bg-overlay); }
@media (hover: none) { .file-row:active { background: var(--bg-overlay); transform: scale(0.99); } }
.file-icon { margin-right: 10px; font-size: 1.1em; flex-shrink: 0; }
.file-name { flex: 1; color: var(--text-primary); font-weight: 500; }
.file-ts { color: #7d8590; font-size: var(--text-xs); margin-right: 12px; white-space: nowrap; }
.file-size { color: var(--text-secondary); font-size: 0.8em; margin-right: 12px; white-space: nowrap; }
.download-btn { color: var(--accent-blue); text-decoration: none; font-size: 1.1em; padding: 4px;
                flex-shrink: 0; }
.download-btn:hover { color: var(--accent-blue-hover); }

/* History dropdown */
.history-dropdown { display: none; margin: 0 0 6px 32px; padding: 6px 0;
                    border: 1px solid var(--border-muted); border-radius: var(--radius-md); background: var(--bg-base); }
.history-dropdown.open { display: block; animation: slide-down 0.15s ease; }
.history-item { display: flex; align-items: center; padding: 5px 14px; gap: 10px; }
.history-item a { color: var(--accent-blue); text-decoration: none; font-size: 0.82em; }
.history-item a:hover { color: var(--accent-blue-hover); text-decoration: underline; }
.history-item .snap-badge { color: var(--color-add); font-size: 0.7em; }
.history-item .no-snap { color: var(--text-muted); font-size: 0.7em; }
.history-item .op-write { color: var(--color-add); font-size: 0.7em; font-weight: 500; }
.history-item .op-edit { color: var(--color-warn); font-size: 0.7em; font-weight: 500; }
.history-item .op-delete { color: var(--color-del); font-size: 0.7em; font-weight: 500; }
.history-item .op-rollback { color: var(--color-special); font-size: 0.7em; font-weight: 500; }
.hist-action { font-size: 0.7em; }
.hist-action a { font-size: 1em; }

/* Deleted file row */
.file-row.deleted { opacity: 0.6; border-color: rgba(248,81,73,0.5); }
.file-row.deleted .file-name { text-decoration: line-through; color: var(--color-del); }
.file-row.deleted .file-ts { color: rgba(248,81,73,0.5); }
.history-header { color: var(--text-secondary); font-size: var(--text-xs); padding: 4px 14px; border-bottom: 1px solid var(--border-muted);
                  margin-bottom: 4px; }

/* View page */
.topbar { display: flex; align-items: center; gap: 15px; margin-bottom: 15px; flex-wrap: wrap; }
.topbar a { color: var(--accent-blue); text-decoration: none; font-size: 0.9em; }
.topbar .fname { flex: 1; color: var(--text-primary); font-weight: bold; }
pre.code { background: var(--bg-raised); border: 1px solid var(--border-muted); border-radius: var(--radius-md);
           padding: 16px; overflow-x: auto; font-size: var(--text-sm); line-height: 1.5;
           white-space: pre; font-family: var(--font-mono); }
.line-num { color: var(--text-muted); display: inline-block; width: 45px; text-align: right;
            margin-right: 16px; user-select: none; }
.img-preview { max-width: 100%; border: 1px solid var(--border-muted); border-radius: var(--radius-md);
               margin: 15px 0; }
.no-preview { color: var(--text-secondary); padding: 40px; text-align: center;
              border: 1px dashed var(--border-muted); border-radius: var(--radius-md); margin: 15px 0; }
.snap-label { color: var(--color-add); font-size: 0.8em; margin-left: 10px; }

/* VS Code-style side-by-side diff */
.diff-page { max-width: 1400px; }
.diff-meta { display: flex; align-items: center; justify-content: space-between;
             padding: 10px 16px; background: var(--bg-overlay); border: 1px solid var(--border-default);
             border-radius: var(--radius-lg); margin-bottom: 12px; }
.diff-stats { font-size: var(--text-sm); white-space: nowrap; display: flex; gap: 12px; }
.diff-stats .add-count { color: var(--color-add); font-weight: 600; }
.diff-stats .del-count { color: var(--color-del); font-weight: 600; }
.diff-fheader { display: flex; border: 1px solid var(--border-default); border-bottom: none;
                border-radius: var(--radius-lg) var(--radius-lg) 0 0; overflow: hidden; }
.diff-fheader div { flex: 1; padding: 10px 16px; font-size: 0.82em; background: var(--bg-overlay);
                    color: var(--text-secondary); font-family: var(--font-mono); }
.diff-fheader div:first-child { border-right: 1px solid var(--border-default); }
.diff-fheader .fh-old::before { content: '\2212 '; color: var(--color-del); font-weight: 700; }
.diff-fheader .fh-new::before { content: '+ '; color: var(--color-add); font-weight: 700; }
.diff-fheader .fh-old { color: #f0a8a8; }
.diff-fheader .fh-new { color: #a8f0c0; }
.diff-wrap { width: 100%; overflow-x: auto; border: 1px solid var(--border-default);
             border-top: none; border-radius: 0 0 var(--radius-lg) var(--radius-lg); background: var(--bg-base); }
.diff-table { width: 100%; border-collapse: collapse; table-layout: fixed;
              font-family: var(--font-mono);
              font-size: 0.82em; line-height: 1.7; }
.diff-table col.ln { width: 52px; }
.diff-table col.mk { width: 20px; }
.diff-table col.gt { width: 2px; }
.diff-table td { padding: 0; vertical-align: top; white-space: pre;
                 overflow: hidden; text-overflow: ellipsis; }
.diff-table .ln { text-align: right; padding-right: 8px; color: rgba(139,148,158,0.5);
                  user-select: none; font-size: 0.9em; background: rgba(13,17,23,0.6);
                  border-right: 1px solid var(--border-muted); }
.diff-table .mk { text-align: center; color: rgba(139,148,158,0.4);
                   user-select: none; font-size: 0.85em; width: 20px; }
.diff-table .code { padding: 0 16px; color: var(--text-primary); }
.diff-table .gt { background: var(--border-default); padding: 0; }
/* Delete line (left) */
.diff-table .dl { background: rgba(248,81,73,0.13); }
.diff-table td.dl.ln { background: rgba(248,81,73,0.10); color: rgba(248,81,73,0.7); }
.diff-table td.dl.mk { background: rgba(248,81,73,0.13); color: var(--color-del); }
.diff-table td.dl.code { color: #e6b0aa; }
/* Add line (right) */
.diff-table .al { background: rgba(63,185,80,0.13); }
.diff-table td.al.ln { background: rgba(63,185,80,0.10); color: rgba(63,185,80,0.7); }
.diff-table td.al.mk { background: rgba(63,185,80,0.13); color: var(--color-add); }
.diff-table td.al.code { color: #a8e6b0; }
/* Empty placeholder */
.diff-table td.el { background: var(--bg-raised); }
.diff-table td.el.ln { background: rgba(22,27,34,0.8); border-right-color: var(--bg-overlay); }
/* Word-level highlights */
.diff-table .wd { background: rgba(248,81,73,0.4); border-radius: var(--radius-sm); padding: 1px 2px; }
.diff-table .wa { background: rgba(63,185,80,0.4); border-radius: var(--radius-sm); padding: 1px 2px; }
/* Fold separator */
.diff-table .fold-row td { background: var(--bg-overlay); height: 32px; text-align: center;
                           color: var(--accent-blue); font-size: 0.78em; letter-spacing: 0.5px;
                           border-top: 1px solid var(--border-muted); border-bottom: 1px solid var(--border-muted); }
.diff-table .fold-row td .fold-icon { margin-right: 6px; }
@media (max-width: 768px) {
  .diff-page { max-width: 100%; padding: 8px; }
  .diff-table { font-size: 0.72em; line-height: 1.5; }
  .diff-table col.ln { width: 32px; }
  .diff-table col.mk { width: 14px; }
  .diff-table .ln { padding-right: 4px; }
  .diff-table .code { padding: 0 6px; }
}

/* Rollback */
.rb-confirm { background: var(--bg-raised); border: 1px solid var(--border-muted); border-radius: var(--radius-md);
              padding: 20px; margin: 15px 0; }
.rb-confirm h3 { color: var(--color-warn); margin-bottom: 12px; font-size: 1em; }
.rb-info { color: var(--text-secondary); font-size: var(--text-sm); margin-bottom: 6px; }
.rb-btns { display: flex; gap: 10px; margin-top: 16px; }
.rb-btns .btn-rollback { background: var(--color-warn); color: #fff; border: none; padding: 8px 20px;
                         border-radius: var(--radius-md); cursor: pointer; font-size: var(--text-sm); }
.rb-btns .btn-rollback:hover { background: #e3b341; }
.rb-btns .btn-cancel { background: transparent; border: 1px solid var(--border-default); color: var(--text-secondary);
                       padding: 8px 20px; border-radius: var(--radius-md); cursor: pointer; font-size: var(--text-sm); }
.rb-btns .btn-cancel:hover { background: var(--border-muted); }

/* Toolbar: search + sort + lang + diff button */
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
.toolbar input[type="text"] { flex: 1; min-width: 120px; background: var(--bg-raised); border: 1px solid var(--border-default);
    color: var(--text-primary); padding: 6px 12px; border-radius: var(--radius-md); font-size: var(--text-sm); outline: none; }
.toolbar input[type="text"]:focus { border-color: var(--accent-blue); box-shadow: 0 0 0 3px rgba(88,166,255,0.1); }
.toolbar input[type="text"]::placeholder { color: var(--text-muted); }
.toolbar select { background: var(--bg-raised); border: 1px solid var(--border-default); color: var(--text-secondary);
    padding: 6px 8px; border-radius: var(--radius-md); font-size: 0.8em; cursor: pointer; outline: none;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%238b949e' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 8px center; padding-right: 24px; }
.toolbar select:focus { border-color: var(--accent-blue); }
.diff-tool-btn { background: transparent; border: 1px solid var(--accent-blue); color: var(--accent-blue);
    padding: 5px 12px; border-radius: var(--radius-md); font-size: 0.8em; cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease; text-decoration: none; white-space: nowrap; }
.diff-tool-btn:hover { background: var(--accent-blue); color: #fff; }

/* Code view: highlight.js + line numbers + copy button */
.code-wrap { position: relative; border: 1px solid var(--border-muted); border-radius: var(--radius-md);
             overflow: hidden; margin: 15px 0; }
.code-wrap .copy-btn { position: absolute; top: 8px; right: 8px; background: var(--border-muted);
    border: 1px solid var(--border-default); color: var(--text-secondary); padding: 4px 10px; border-radius: var(--radius-sm);
    font-size: var(--text-xs); cursor: pointer; z-index: 2; transition: background 0.2s, color 0.2s; }
.code-wrap .copy-btn:hover { background: var(--border-default); color: var(--text-primary); }
.code-wrap .copy-btn.ok { background: #238636; color: #fff; border-color: #238636; animation: copy-success 0.2s ease; }
.code-container { display: flex; overflow-x: auto; }
.code-container .line-nums { padding: 16px 0; background: var(--bg-base); border-right: 1px solid var(--border-muted);
    user-select: none; flex-shrink: 0; text-align: right; }
.code-container .line-nums span { display: block; padding: 0 12px 0 16px; color: var(--text-muted);
    font-family: var(--font-mono); font-size: var(--code-font-size); line-height: var(--code-line-height); }
.code-container pre { flex: 1; margin: 0; padding: 16px; background: var(--bg-raised); overflow-x: visible; }
.code-container pre code { font-size: var(--code-font-size); line-height: var(--code-line-height); background: transparent !important;
    padding: 0 !important; }

/* Diff tool page */
.dt-panel { background: var(--bg-raised); border: 1px solid var(--border-muted); border-radius: var(--radius-lg);
            padding: 20px; margin-bottom: 16px; }
.dt-panel label { color: var(--text-secondary); font-size: var(--text-sm); display: block; margin-bottom: 6px; }
.dt-panel select { width: 100%; background: var(--bg-base); border: 1px solid var(--border-default); color: var(--text-primary);
    padding: 8px 12px; border-radius: var(--radius-md); font-size: var(--text-sm); outline: none; cursor: pointer; }
.dt-panel select:focus { border-color: var(--accent-blue); }
.dt-panel select:disabled { opacity: 0.4; cursor: not-allowed; }
.dt-snap-row { display: grid; grid-template-columns: 1fr auto 1fr; gap: 16px; margin-top: 12px; }
.dt-snap-divider { color: var(--text-muted); font-size: 1.2em; display: flex;
  align-items: center; justify-content: center; user-select: none; padding-top: 20px; }
.dt-snap-row > div:first-child label { color: rgba(248,81,73,0.8); }
.dt-snap-row > div:last-child label { color: rgba(63,185,80,0.8); }
.dt-info { color: var(--color-add); font-size: 0.78em; margin-top: 6px; text-align: center; }
.dt-hint { color: var(--text-muted); font-size: 0.82em; }
.dt-result { margin-top: 16px; }

/* Language selector */
.lang-sel { background: var(--bg-raised); border: 1px solid var(--border-default); color: var(--text-secondary); padding: 4px 6px;
    border-radius: var(--radius-sm); font-size: var(--text-xs); cursor: pointer; outline: none; }
.lang-sel:focus { border-color: var(--accent-blue); }

/* Cycle rollback modal */
.modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                 background: rgba(0,0,0,0.7); z-index: 100; justify-content: center; align-items: center;
                 opacity: 0; transition: opacity 0.2s ease; }
.modal-overlay.open { display: flex; opacity: 1; }
.modal { background: var(--bg-raised); border: 1px solid var(--border-default); border-radius: var(--radius-xl);
         max-width: 600px; width: 90%; max-height: 80vh; overflow-y: auto; padding: var(--space-6);
         transform: translateY(20px); transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1); }
.modal-overlay.open .modal { transform: translateY(0); }
.modal::before { content: ''; display: block; width: 40px; height: 4px;
  background: var(--border-default); border-radius: 2px; margin: 0 auto var(--space-4); }
.modal h2 { color: var(--color-warn); font-size: 1.1em; margin-bottom: 16px; }
.cycle-item { display: flex; align-items: center; padding: 10px 14px; margin: 4px 0;
              border: 1px solid var(--border-muted); border-radius: var(--radius-md); cursor: pointer;
              transition: border-color 0.2s, background 0.2s; }
.cycle-item:hover { border-color: var(--color-warn); background: var(--bg-overlay); }
.cycle-info { flex: 1; }
.cycle-id { color: var(--color-warn); font-weight: 500; font-size: var(--text-sm); }
.cycle-label { color: var(--text-primary); font-size: 0.82em; margin-top: 3px;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 500px; }
.cycle-ts { color: var(--text-secondary); font-size: 0.78em; margin-top: 2px; }
.cycle-files { color: var(--text-secondary); font-size: var(--text-xs); margin-top: 2px; }
.modal-close { background: transparent; border: 1px solid var(--border-default); color: var(--text-secondary);
               padding: 6px 16px; border-radius: var(--radius-md); cursor: pointer; margin-top: 12px; }
.modal-close:hover { background: var(--border-muted); }

/* Modal responsive */
@media (min-width: 600px) { .modal::before { display: none; }
  .modal-overlay { align-items: center; }
  .modal { border-radius: var(--radius-xl); width: 90%; } }
@media (max-width: 599px) {
  .modal-overlay { align-items: flex-end; }
  .modal { border-radius: var(--radius-xl) var(--radius-xl) 0 0; width: 100%; max-width: 100%; } }

/* Animations */
@keyframes slide-down { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
@keyframes copy-success { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }

/* Toast */
.toast-box { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
  background: var(--bg-overlay); border: 1px solid var(--border-default); border-radius: var(--radius-lg);
  padding: var(--space-3) var(--space-4); min-width: 280px; max-width: 90vw; z-index: 200;
  box-shadow: 0 8px 24px rgba(0,0,0,0.5); animation: toast-up 0.2s ease;
  display: flex; flex-direction: column; gap: var(--space-2); }
@keyframes toast-up { from { transform: translateX(-50%) translateY(10px); opacity: 0; }
  to { transform: translateX(-50%) translateY(0); opacity: 1; } }
.toast-msg { font-size: var(--text-sm); color: var(--text-primary); }
.toast-btns { display: flex; gap: var(--space-2); justify-content: flex-end; }
.toast-ok { background: var(--color-warn); color: #fff; border: none; padding: 6px 16px;
  border-radius: var(--radius-md); cursor: pointer; font-size: var(--text-xs); }
.toast-cancel { background: transparent; border: 1px solid var(--border-default); color: var(--text-secondary);
  padding: 6px 16px; border-radius: var(--radius-md); cursor: pointer; font-size: var(--text-xs); }
.toast-ok:hover { background: var(--color-warn); filter: brightness(1.2); }
.toast-info { border-color: var(--accent-blue); }
.toast-ok-type { border-color: var(--color-add); }

/* Empty / error state boxes */
.state-box { padding: var(--space-5) var(--space-4); text-align: center;
  border-radius: var(--radius-lg); margin: var(--space-4) 0; }
.state-box--empty { border: 1px dashed var(--border-muted); color: var(--text-muted); }
.state-box--deleted { border: 1px dashed rgba(248,81,73,0.3); background: rgba(248,81,73,0.05); }
.state-box .state-icon { font-size: 2em; display: block; margin-bottom: var(--space-2); line-height: 1; }
.state-box .state-title { font-size: var(--text-base); color: var(--text-primary); margin-bottom: var(--space-1); }
.state-box .state-desc { font-size: var(--text-sm); color: var(--text-secondary); }

/* Focus styles */
details > summary:focus-visible { outline: 2px solid var(--accent-blue); outline-offset: 2px; border-radius: var(--radius-md); }

/* Mobile responsive */
@media (max-width: 600px) {
  .file-row { flex-wrap: wrap; gap: var(--space-1); padding: 10px 12px; }
  .file-name { width: calc(100% - 40px); flex: none; }
  .file-ts, .file-size { font-size: 0.72rem; margin-right: 8px; }
  .download-btn { padding: 8px; margin-left: auto; }
  .history-item { padding: 10px 14px; gap: 12px; }
  .history-item a { font-size: 0.85em; min-height: 24px; display: inline-flex; align-items: center; }
  .hist-action { display: flex; gap: 8px; align-items: center; }
  .header-row { flex-direction: column; align-items: stretch; }
  .header-btns { justify-content: flex-start; }
  .clear-btn, .outline-btn--danger { margin-left: auto; }
  .diff-wrap { -webkit-overflow-scrolling: touch; }
  .diff-table { min-width: 500px; font-size: 0.68em; line-height: 1.4; }
  .dt-snap-row { grid-template-columns: 1fr; gap: var(--space-2); }
  .dt-snap-divider { display: none; }
}
"""

_JS = ("""
<link id="hljs-theme" rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github-dark.min.css">
<script>
var VI18N=""" + _VIEWER_I18N_JSON + """;

// --- Theme ---
var _hljsDark='https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github-dark.min.css';
var _hljsLight='https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github.min.css';
function _getSystemTheme(){return window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark';}
function _resolveTheme(pref){return pref==='system'?_getSystemTheme():pref;}
function applyTheme(pref){
  if(!pref)pref=localStorage.getItem('fv_theme')||'system';
  localStorage.setItem('fv_theme',pref);
  var resolved=_resolveTheme(pref);
  document.documentElement.setAttribute('data-theme',resolved==='light'?'light':'');
  if(resolved==='light')document.documentElement.removeAttribute('data-theme');
  if(resolved==='light')document.documentElement.setAttribute('data-theme','light');
  else document.documentElement.removeAttribute('data-theme');
  var hljsLink=document.getElementById('hljs-theme');
  if(hljsLink)hljsLink.href=resolved==='light'?_hljsLight:_hljsDark;
  var btn=document.getElementById('theme-toggle');
  if(btn)btn.textContent=resolved==='light'?'\\u2600':'\\u263D';
}
function toggleTheme(){
  var cur=localStorage.getItem('fv_theme')||'system';
  var resolved=_resolveTheme(cur);
  var next=resolved==='dark'?'light':'dark';
  applyTheme(next);
}
window.matchMedia('(prefers-color-scheme:light)').addEventListener('change',function(){
  if((localStorage.getItem('fv_theme')||'system')==='system')applyTheme('system');
});

var _lang=localStorage.getItem('fv_lang')||'ko';
function T(k){return (VI18N[_lang]||VI18N.ko)[k]||k;}
function applyI18n(){
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    var k=el.getAttribute('data-i18n');
    var v=T(k);if(v)el.textContent=v;
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(function(el){
    el.placeholder=T(el.getAttribute('data-i18n-ph'));
  });
  document.querySelectorAll('[data-i18n-title]').forEach(function(el){
    el.title=T(el.getAttribute('data-i18n-title'));
  });
  // ts+op dynamic
  document.querySelectorAll('[data-ts-op]').forEach(function(el){
    var ts=el.dataset.ts, op=el.dataset.tsOp;
    var opLabel=T('op_'+op);
    el.textContent=T('ts_fmt').replace('{ts}',ts).replace('{op}',opLabel);
  });
  // lang selector sync
  var ls=document.getElementById('lang-sel');
  if(ls)ls.value=_lang;
  // rebuild AI providers with updated lang
  if (typeof _buildAiProviders === 'function' && document.getElementById('ai-providers')) _buildAiProviders();
}
function switchLang(lang){
  _lang=lang;localStorage.setItem('fv_lang',lang);applyI18n();
}
// Toast notification (replaces confirm/alert)
function showToast(msg, type, onConfirm) {
  var old = document.querySelector('.toast-box'); if(old) old.remove();
  var t = document.createElement('div'); t.className = 'toast-box toast-' + (type||'info');
  if(onConfirm) {
    t.innerHTML = '<span class="toast-msg">' + msg + '</span><div class="toast-btns">' +
      '<button class="toast-cancel" onclick="this.closest(\\'.toast-box\\').remove()">' + T('cancel') + '</button>' +
      '<button class="toast-ok">OK</button></div>';
    t.querySelector('.toast-ok').onclick = function(){ t.remove(); onConfirm(); };
  } else {
    t.innerHTML = '<span class="toast-msg">' + msg + '</span>';
    setTimeout(function(){ t.remove(); }, 3000);
  }
  document.body.appendChild(t);
}
// Existing functions
function toggleHistory(btn, id) {
  var el = document.getElementById(id);
  if (!el) return;
  btn.parentElement.classList.toggle('expanded');
  el.classList.toggle('open');
}
function clearHistory(url) {
  showToast(T('cfm_clear'), 'warn', function(){
    fetch(url, {method:'POST'}).then(function(r){
      if (r.ok) location.reload();
      else showToast(T('req_fail'), 'info');
    }).catch(function(){showToast(T('req_fail'), 'info');});
  });
}
function doRollbackFile(url) {
  showToast(T('cfm_rb'), 'warn', function(){
    fetch(url, {method:'POST'}).then(function(r){
      if (r.ok) { showToast(T('rb_done'), 'ok'); setTimeout(function(){location.reload();},800); }
      else r.text().then(function(t){showToast(T('failed') + t, 'info');});
    }).catch(function(){showToast(T('req_fail'), 'info');});
  });
}
function showCycleModal() {
  document.getElementById('cycle-modal').classList.add('open');
}
function closeCycleModal() {
  document.getElementById('cycle-modal').classList.remove('open');
}
function doRollbackCycle(url) {
  showToast(T('cfm_cycle'), 'warn', function(){
    fetch(url, {method:'POST'}).then(function(r){
      if(r.ok) return r.text().then(function(t){
        if(t==='SAME_STATE'){showToast(T('same_state'),'info');}
        else{showToast(T('cycle_done'),'ok');setTimeout(function(){location.reload();},800);}
      });
      else r.text().then(function(t){showToast(T('failed') + t, 'info');});
    }).catch(function(){showToast(T('req_fail'), 'info');});
  });
}
// Copy code
function copyCode(btn){
  var pre=btn.closest('.code-wrap').querySelector('pre code');
  if(!pre)return;
  navigator.clipboard.writeText(pre.textContent).then(function(){
    btn.textContent=T('copied');btn.classList.add('ok');
    setTimeout(function(){btn.textContent=T('copy');btn.classList.remove('ok');},1500);
  });
}
// Search files
function filterFiles(q){
  q=q.toLowerCase();
  var mode=(document.getElementById('search-mode')||{}).value||'name';
  document.querySelectorAll('.file-entry').forEach(function(el){
    var target=mode==='path'?(el.dataset.fpath||''):(el.dataset.fname||'');
    var match=!q||target.toLowerCase().indexOf(q)>=0;
    el.style.display=match?'':'none';
    // also show/hide associated history dropdown
    var hid=el.dataset.histId;
    if(hid){var h=document.getElementById(hid);if(h&&!match){h.classList.remove('open');}}
  });
  // hide empty groups
  document.querySelectorAll('details.dir-group').forEach(function(g){
    var vis=g.querySelectorAll('.file-entry[style=""], .file-entry:not([style])');
    g.style.display=vis.length?'':'none';
  });
  document.querySelectorAll('details.date-group').forEach(function(g){
    var vis=g.querySelectorAll('details.dir-group[style=""], details.dir-group:not([style])');
    g.style.display=vis.length?'':'none';
  });
}
// Sort files
function sortFiles(by){
  var container=document.getElementById('grouped-file-list');
  if(!container)return;
  var entries=Array.from(container.querySelectorAll('.file-entry'));
  entries.sort(function(a,b){
    if(by==='name')return (a.dataset.fname||'').localeCompare(b.dataset.fname||'');
    if(by==='type')return (a.dataset.ftype||'').localeCompare(b.dataset.ftype||'');
    return (b.dataset.fts||'').localeCompare(a.dataset.fts||''); // time desc
  });
  // Re-render as flat list (collapse groups)
  var flat=document.getElementById('flat-file-list');
  if(!flat)return;
  var grouped=document.getElementById('grouped-file-list');
  if(by==='time'){
    flat.style.display='none';grouped.style.display='';
  } else {
    flat.innerHTML='';
    entries.forEach(function(e){
      var clone=e.cloneNode(true);
      flat.appendChild(clone);
      // also clone its history dropdown if any
      var hid=e.dataset.histId;
      if(hid){var h=document.getElementById(hid);if(h)flat.appendChild(h.cloneNode(true));}
    });
    flat.style.display='';grouped.style.display='none';
  }
}
// Highlight.js init
function initHL(){
  if(typeof hljs==='undefined')return;
  document.querySelectorAll('pre code[class*="language-"]').forEach(function(b){
    hljs.highlightElement(b);
  });
}
// Diff tool
function dtFileChange(sel,token){
  var path=sel.value;
  var lSel=document.getElementById('dt-snap-l');
  var rSel=document.getElementById('dt-snap-r');
  lSel.innerHTML='<option value="">--</option>';
  rSel.innerHTML='<option value="">--</option>';
  lSel.disabled=!path;rSel.disabled=!path;
  document.getElementById('dt-result').innerHTML='';
  document.getElementById('dt-info').textContent='';
  if(!path||!window._dtFiles)return;
  var snaps=window._dtFiles[path]||[];
  snaps.forEach(function(s){
    var opt='<option value="'+s.snap+'">'+s.label+'</option>';
    lSel.innerHTML+=opt;rSel.innerHTML+=opt;
  });
}
function dtSnapChange(token){
  var lSnap=document.getElementById('dt-snap-l').value;
  var rSnap=document.getElementById('dt-snap-r').value;
  var info=document.getElementById('dt-info');
  var result=document.getElementById('dt-result');
  if(!lSnap||!rSnap){result.innerHTML='';info.textContent='';return;}
  if(lSnap===rSnap){result.innerHTML='<div class="no-preview">'+T('no_diff')+'</div>';info.textContent='';return;}
  // Auto-order: older on left
  var lTs=lSnap.substring(0,15);var rTs=rSnap.substring(0,15);
  var swapped=false;
  if(lTs>rTs){var tmp=lSnap;lSnap=rSnap;rSnap=tmp;swapped=true;}
  if(swapped)info.textContent=T('dt_ordered');
  else info.textContent='';
  result.innerHTML='<div style="color:#8b949e;text-align:center;padding:20px">Loading...</div>';
  fetch('/diff-fragment/'+token+'/'+lSnap+'/'+rSnap).then(function(r){
    if(r.ok)return r.text();throw new Error();
  }).then(function(h){result.innerHTML=h;}).catch(function(){
    result.innerHTML='<div class="no-preview">'+T('req_fail')+'</div>';
  });
}
document.addEventListener('DOMContentLoaded',function(){
  applyTheme();initHL();applyI18n();
});
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js" async onload="initHL()"></script>
""")



def _op_label(op):
    """Return Korean label for operation type."""
    return {"write": "\uc791\uc131\ub428", "edit": "\uc218\uc815\ub428", "delete": "\uc0ad\uc81c\ub428",
            "rollback": "\ub864\ubc31\ub428"}.get(op, "\uc218\uc815\ub428")


def _op_label_short(op):
    """Return short label for history dropdown."""
    return {"write": "Write", "edit": "Edit", "delete": "Delete",
            "rollback": "Rollback"}.get(op, "Edit")


def _op_css_class(op):
    """Return CSS class for op badge."""
    return {"write": "op-write", "edit": "op-edit", "delete": "op-delete",
            "rollback": "op-rollback"}.get(op, "op-edit")


# ---------------------------------------------------------------------------
# Cycle (run_id) helpers
# ---------------------------------------------------------------------------
def _get_cycles(entries):
    """Group entries by run_id. Returns list of {run_id, ts_start, ts_end, files, entries} sorted desc."""
    by_run = defaultdict(list)
    for e in entries:
        rid = e.get("run_id", 0)
        if rid > 0 and e.get("op") != "rollback-backup":
            by_run[rid].append(e)
    cycles = []
    for rid, ents in sorted(by_run.items(), reverse=True):
        ts_list = [e["ts"] for e in ents]
        unique_paths = list(dict.fromkeys(e["path"] for e in ents))
        # Get the run_label from the first entry that has one
        label = ""
        for e in ents:
            lbl = e.get("run_label", "")
            if lbl:
                label = lbl
                break
        cycles.append({
            "run_id": rid,
            "ts_start": min(ts_list),
            "ts_end": max(ts_list),
            "files": unique_paths,
            "entries": ents,
            "label": label,
        })
    return cycles


# ---------------------------------------------------------------------------
# Page generators
# ---------------------------------------------------------------------------
def _page_list(entries, session_token):
    """Generate the file list HTML page."""
    unique_files = _aggregate_files(entries)

    # Group by DATE first, then by directory within each date
    date_groups = defaultdict(list)
    for i, finfo in enumerate(unique_files):
        date_key = _format_date(finfo["latest_ts"])
        date_groups[date_key].append((i, finfo))

    rows = []
    for date_key in sorted(date_groups.keys(), reverse=True):
        files_in_date = date_groups[date_key]
        date_count = len(files_in_date)

        dir_groups = defaultdict(list)
        for idx, finfo in files_in_date:
            dirname = os.path.dirname(finfo["path"])
            dir_groups[dirname].append((idx, finfo))

        dir_rows = []
        for dirname in sorted(dir_groups.keys()):
            dir_files = dir_groups[dirname]
            d_count = len(dir_files)
            esc_dir = html.escape(dirname)
            file_rows = []
            for idx, finfo in dir_files:
                fname = os.path.basename(finfo["path"])
                ftype = _file_type(finfo["path"])
                latest_op = finfo["history"][0].get("op", "edit")
                is_deleted = (latest_op == "delete")
                try:
                    fsize = _human_size(os.path.getsize(finfo["path"]))
                except OSError:
                    fsize = "\u2014"
                icon = "\U0001f5d1" if is_deleted else {"code": "\U0001f4c4", "image": "\U0001f5bc\ufe0f", "other": "\U0001f4e6"}[ftype]
                ts_display = _format_ts(finfo["latest_ts"])
                op_text = _op_label(latest_op)
                hist_id = f"hist_{idx}"
                hist_count = len(finfo["history"])

                # Build history dropdown items
                hist_items = []
                for hi, entry in enumerate(finfo["history"]):
                    ts_fmt = _format_ts(entry["ts"])
                    entry_op = entry.get("op", "edit")
                    op_short = _op_label_short(entry_op)
                    op_cls = _op_css_class(entry_op)
                    snap = entry.get("snapshot")

                    # Action buttons
                    actions = []
                    if snap:
                        snap_url = f"/snapshot/{session_token}/{snap}"
                        ts_link = f'<a href="{snap_url}">{ts_fmt}</a>'
                        actions.append(f'<span class="snap-badge">\u2713 snapshot</span>')
                        # Diff button: compare with next (older) snapshot
                        if hi + 1 < len(finfo["history"]):
                            older = finfo["history"][hi + 1]
                            if older.get("snapshot"):
                                diff_url = f"/diff/{session_token}/{older['snapshot']}/{snap}"
                                actions.append(f'<a href="{diff_url}" title="Diff" style="color:#58a6ff">diff</a>')
                        # Rollback button
                        rb_url = f"/rollback/{session_token}/{snap}"
                        actions.append(
                            f'<a href="javascript:void(0)" onclick="doRollbackFile(\'{rb_url}\')" '
                            f'title="Rollback to this snapshot" style="color:#d29922">\u21a9</a>')
                    else:
                        if entry_op == "delete":
                            ts_link = f'<span style="color:#8b949e">{ts_fmt}</span>'
                        else:
                            view_url = f"/view/{session_token}/{idx}"
                            ts_link = f'<a href="{view_url}">{ts_fmt}</a>'
                            actions.append(f'<span class="no-snap">current only</span>')

                    actions_html = ' '.join(actions)
                    hist_items.append(
                        f'<div class="history-item">'
                        f'{ts_link}'
                        f'<span class="{op_cls}">{op_short}</span>'
                        f'<span class="hist-action">{actions_html}</span></div>')

                hist_html = ""
                if hist_count > 0:
                    hist_html = (
                        f'<div class="history-dropdown" id="{hist_id}">'
                        f'<div class="history-header">\U0001f552 {hist_count} modification(s)</div>'
                        f'{"".join(hist_items)}</div>')

                row_cls = "file-row deleted" if is_deleted else "file-row"
                if is_deleted:
                    last_snap = None
                    for h in finfo["history"]:
                        if h.get("snapshot"):
                            last_snap = h["snapshot"]
                            break
                    if last_snap:
                        dl_btn = (
                            f'<a class="download-btn" href="/snapshot-dl/{session_token}/{last_snap}"'
                            f' title="Download last snapshot" aria-label="Download last snapshot"'
                            f' onclick="event.stopPropagation()">\u2b07</a>')
                    else:
                        dl_btn = ""
                else:
                    dl_btn = (
                        f'<a class="download-btn" href="/download/{session_token}/{idx}"'
                        f' title="Download" aria-label="Download file"'
                        f' onclick="event.stopPropagation()">\u2b07</a>')
                file_rows.append(f'''
                <div class="{row_cls} file-entry" data-fname="{html.escape(fname)}"
                     data-fpath="{html.escape(finfo['path'])}"
                     data-ftype="{ftype}" data-fts="{finfo['latest_ts']}"
                     data-hist-id="{hist_id}"
                     onclick="toggleHistory(this, '{hist_id}')">
                    <span class="file-icon">{icon}</span>
                    <span class="file-name">{html.escape(fname)}</span>
                    <span class="file-ts" data-ts-op="{latest_op}" data-ts="{ts_display}">{ts_display}\uc5d0 {op_text}</span>
                    <span class="file-size">{fsize}</span>
                    {dl_btn}
                </div>
                {hist_html}''')

            dir_rows.append(
                f'<details class="dir-group" open>'
                f'<summary>\U0001f4c1 {esc_dir} ({d_count})</summary>'
                f'{"".join(file_rows)}</details>')

        rows.append(
            f'<details class="date-group" open>'
            f'<summary>\U0001f4c5 {date_key} ({date_count})</summary>'
            f'{"".join(dir_rows)}</details>')

    # Build cycle rollback modal
    cycles = _get_cycles(entries)
    cycle_items = []
    for cyc in cycles:
        ts_range = f"{_format_ts(cyc['ts_start'])} ~ {_format_ts(cyc['ts_end'])}"
        file_names = [os.path.basename(p) for p in cyc["files"][:5]]
        more = f" +{len(cyc['files']) - 5}" if len(cyc["files"]) > 5 else ""
        files_str = html.escape(", ".join(file_names) + more)
        label = html.escape(cyc.get("label", ""))
        label_html = f'<div class="cycle-label">{label}</div>' if label else ""
        rb_url = f"/rollback-cycle/{session_token}/{cyc['run_id']}"
        cycle_items.append(
            f'<div class="cycle-item" onclick="doRollbackCycle(\'{rb_url}\')">'
            f'<div class="cycle-info">'
            f'<div class="cycle-id">Cycle #{cyc["run_id"]}</div>'
            f'{label_html}'
            f'<div class="cycle-ts">{ts_range}</div>'
            f'<div class="cycle-files">{files_str} ({len(cyc["files"])} files)</div>'
            f'</div></div>')

    modal_html = ""
    if cycle_items:
        modal_html = (
            f'<div class="modal-overlay" id="cycle-modal" onclick="if(event.target===this)closeCycleModal()">'
            f'<div class="modal">'
            f'<h2>\u21a9 <span data-i18n="cycle_rb">Cycle Rollback</span></h2>'
            f'<p style="color:#8b949e;font-size:0.85em;margin-bottom:12px" data-i18n="cycle_desc">'
            f'Select a cycle to rollback.</p>'
            f'{"".join(cycle_items)}'
            f'<button class="modal-close" onclick="closeCycleModal()" data-i18n="cancel">Cancel</button>'
            f'</div></div>')

    total_unique = len(unique_files)
    rollback_btn = (f'<button class="rollback-btn" onclick="showCycleModal()">'
                    f'\u21a9 <span data-i18n="rollback">Rollback</span></button>'
                    if cycle_items else "")
    diff_tool_btn = (f'<a class="diff-tool-btn" href="/diff-tool/{session_token}"'
                     f' data-i18n="diff_tool">Diff</a>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Modified Files</title><style>{_CSS}</style></head>
<body><div class="container">
<div class="header-row">
  <div><h1>\U0001f4c2 <span data-i18n="title">Modified Files</span> ({total_unique})</h1>
  <p class="subtitle">\U0001f512 <span data-i18n="read_only">Read-only view</span></p></div>
  <div class="header-btns">
    <button id="theme-toggle" class="outline-btn outline-btn--primary" onclick="toggleTheme()" title="Toggle theme">\u263D</button>
    <select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">
      <option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option>
    </select>
    {diff_tool_btn}
    {rollback_btn}
    <button class="clear-btn" onclick="clearHistory('/clear/{session_token}')">
      \U0001f5d1 <span data-i18n="clear">Clear</span></button>
  </div>
</div>
<div class="toolbar">
  <input type="text" id="search-input" data-i18n-ph="search_ph" placeholder="\ud30c\uc77c \uac80\uc0c9..."
         oninput="filterFiles(this.value)">
  <select id="search-mode" onchange="filterFiles(document.getElementById('search-input').value)">
    <option value="name" data-i18n="search_name">\ud30c\uc77c\uba85</option>
    <option value="path" data-i18n="search_path">\uacbd\ub85c</option>
  </select>
  <select onchange="sortFiles(this.value)">
    <option value="time" data-i18n="sort_time">\uc2dc\uac04\uc21c</option>
    <option value="name" data-i18n="sort_name">\uc774\ub984\uc21c</option>
    <option value="type" data-i18n="sort_type">\ud0c0\uc785\uc21c</option>
  </select>
</div>
<hr class="separator">
<div id="grouped-file-list">{''.join(rows)}</div>
<div id="flat-file-list" style="display:none"></div>
<hr class="separator">
<div class="footer">\U0001f512 <span data-i18n="read_only">Read-only</span> &middot; Session access</div>
</div>{modal_html}{_JS}</body></html>"""


def _render_code_block(text, path):
    """Render code with highlight.js + line numbers + copy button."""
    lang = _get_lang(path)
    lines = text.split("\n")
    line_nums = "".join(f"<span>{i}</span>" for i in range(1, len(lines) + 1))
    lang_cls = f' class="language-{lang}"' if lang else ""
    return (f'<div class="code-wrap">'
            f'<button class="copy-btn" onclick="copyCode(this)" data-i18n="copy">Copy</button>'
            f'<div class="code-container">'
            f'<div class="line-nums">{line_nums}</div>'
            f'<pre><code{lang_cls}>{html.escape(text)}</code></pre>'
            f'</div></div>')


def _page_view(fpath, idx, session_token, title_suffix=""):
    """Generate the file view HTML page."""
    fname = os.path.basename(fpath)
    ftype = _file_type(fpath)
    back_link = f'<a href="/list/{session_token}" data-i18n="back_list">\u2190 List</a>'
    download_link = (f'<a class="download-btn" href="/download/{session_token}/{idx}"'
                     f' data-i18n="download">\u2b07 Download</a>')

    content_html = ""
    if ftype == "code":
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                text = f.read()
            content_html = _render_code_block(text, fpath)
        except Exception as e:
            content_html = f'<div class="no-preview">Cannot read file: {html.escape(str(e))}</div>'
    elif ftype == "image":
        content_html = f'<img class="img-preview" src="/raw/{session_token}/{idx}" alt="{html.escape(fname)}">'
    else:
        content_html = '<div class="no-preview" data-i18n="no_preview">Preview not available.</div>'

    suffix_html = f'<span class="snap-label">{html.escape(title_suffix)}</span>' if title_suffix else ""
    theme_btn = '<button id="theme-toggle" class="outline-btn outline-btn--primary" onclick="toggleTheme()" title="Toggle theme">\u263D</button>'
    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(fname)}</title><style>{_CSS}</style></head>
<body><div class="container">
<div class="topbar">{back_link}<span class="fname">{html.escape(fname)}{suffix_html}</span>{theme_btn}{lang_sel}{download_link}</div>
<hr class="separator">
{content_html}
</div>{_JS}</body></html>"""


def _page_deleted(fpath, idx, session_token):
    """Generate a page for deleted files."""
    fname = os.path.basename(fpath)
    back_link = f'<a href="/list/{session_token}">\u2190 List</a>'
    theme_btn = '<button id="theme-toggle" class="outline-btn outline-btn--primary" onclick="toggleTheme()" title="Toggle theme">\u263D</button>'
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(fname)} (deleted)</title><style>{_CSS}</style></head>
<body><div class="container">
<div class="topbar">{back_link}<span class="fname" style="color:var(--color-del);text-decoration:line-through">{html.escape(fname)}</span>
{theme_btn}<span style="color:var(--color-del);font-size:0.85em">\U0001f5d1 Deleted</span></div>
<hr class="separator">
<div class="no-preview" style="border-color:rgba(248,81,73,0.25)">
  <p style="font-size:1.2em;margin-bottom:10px">\U0001f5d1</p>
  <p>This file has been deleted.</p>
  <p style="color:var(--text-muted);font-size:0.85em;margin-top:8px">{html.escape(fpath)}</p>
  <p style="color:var(--text-muted);font-size:0.85em;margin-top:4px">Check the history dropdown on the file list for previous snapshots.</p>
</div>
</div>{_JS}</body></html>"""


def _word_highlight(old_line, new_line):
    """Character-level diff for replaced lines. Returns (old_html, new_html)."""
    csm = difflib.SequenceMatcher(None, old_line, new_line)
    old_parts, new_parts = [], []
    for tag, i1, i2, j1, j2 in csm.get_opcodes():
        if tag == "equal":
            old_parts.append(html.escape(old_line[i1:i2]))
            new_parts.append(html.escape(new_line[j1:j2]))
        elif tag == "replace":
            old_parts.append(f'<span class="wd">{html.escape(old_line[i1:i2])}</span>')
            new_parts.append(f'<span class="wa">{html.escape(new_line[j1:j2])}</span>')
        elif tag == "delete":
            old_parts.append(f'<span class="wd">{html.escape(old_line[i1:i2])}</span>')
        elif tag == "insert":
            new_parts.append(f'<span class="wa">{html.escape(new_line[j1:j2])}</span>')
    return "".join(old_parts), "".join(new_parts)


def _diff_ctx_row(i, j, text):
    """Shared diff table helper: context (unchanged) row."""
    esc = html.escape(text)
    return (f'<tr><td class="ln">{i}</td><td class="mk"></td><td class="code">{esc}</td>'
            f'<td class="gt"></td>'
            f'<td class="ln">{j}</td><td class="mk"></td><td class="code">{esc}</td></tr>')


def _diff_del_row(i, text):
    """Shared diff table helper: deleted line row."""
    return (f'<tr><td class="ln dl">{i}</td><td class="mk dl">\u2212</td><td class="code dl">{text}</td>'
            f'<td class="gt"></td>'
            f'<td class="ln el"></td><td class="mk el"></td><td class="code el"></td></tr>')


def _diff_add_row(j, text):
    """Shared diff table helper: added line row."""
    return (f'<tr><td class="ln el"></td><td class="mk el"></td><td class="code el"></td>'
            f'<td class="gt"></td>'
            f'<td class="ln al">{j}</td><td class="mk al">+</td><td class="code al">{text}</td></tr>')


def _diff_replace_row(i, old_html, j, new_html):
    """Shared diff table helper: replaced line row (side-by-side)."""
    return (f'<tr><td class="ln dl">{i}</td><td class="mk dl">\u2212</td><td class="code dl">{old_html}</td>'
            f'<td class="gt"></td>'
            f'<td class="ln al">{j}</td><td class="mk al">+</td><td class="code al">{new_html}</td></tr>')


def _make_diff_rows(old_lines, new_lines, context=3):
    """Build diff table rows from two lists of lines. Returns (table_rows, add_count, del_count)."""
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    table_rows = []
    add_count = 0
    del_count = 0
    CONTEXT = context

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            n = i2 - i1
            if n > CONTEXT * 2 + 1:
                for k in range(CONTEXT):
                    table_rows.append(_diff_ctx_row(i1+k+1, j1+k+1, old_lines[i1+k]))
                folded = n - CONTEXT * 2
                table_rows.append(
                    f'<tr class="fold-row"><td colspan="3"></td><td class="gt"></td>'
                    f'<td colspan="3"><span class="fold-icon">\u2195</span>{folded} lines hidden</td></tr>')
                for k in range(CONTEXT):
                    ii, jj = i2 - CONTEXT + k, j2 - CONTEXT + k
                    table_rows.append(_diff_ctx_row(ii+1, jj+1, old_lines[ii]))
            else:
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    table_rows.append(_diff_ctx_row(i+1, j+1, old_lines[i]))

        elif tag == "replace":
            old_n = i2 - i1
            new_n = j2 - j1
            del_count += old_n
            add_count += new_n
            # If sizes differ a lot, render as separate delete + insert blocks
            # for clearer visual alignment (avoids confusing N:1 pairing)
            if max(old_n, new_n) > 2 * min(old_n, new_n):
                for i in range(i1, i2):
                    table_rows.append(_diff_del_row(i+1, html.escape(old_lines[i])))
                for j in range(j1, j2):
                    table_rows.append(_diff_add_row(j+1, html.escape(new_lines[j])))
            else:
                # Paired with word-level highlighting
                max_len = max(old_n, new_n)
                for k in range(max_len):
                    has_old = k < old_n
                    has_new = k < new_n
                    if has_old and has_new:
                        oh, nh = _word_highlight(old_lines[i1+k], new_lines[j1+k])
                        table_rows.append(_diff_replace_row(i1+k+1, oh, j1+k+1, nh))
                    elif has_old:
                        table_rows.append(_diff_del_row(i1+k+1, html.escape(old_lines[i1+k])))
                    else:
                        table_rows.append(_diff_add_row(j1+k+1, html.escape(new_lines[j1+k])))

        elif tag == "delete":
            del_count += i2 - i1
            for i in range(i1, i2):
                table_rows.append(_diff_del_row(i+1, html.escape(old_lines[i])))

        elif tag == "insert":
            add_count += j2 - j1
            for j in range(j1, j2):
                table_rows.append(_diff_add_row(j+1, html.escape(new_lines[j])))

    return table_rows, add_count, del_count


def _snap_ts_label(snap_name):
    """Extract human-readable timestamp from snapshot filename (YYYYMMDD_HHMMSS_...)."""
    try:
        parts = snap_name.split("_")
        return f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]} {parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:6]}"
    except (IndexError, ValueError):
        return snap_name


def _page_diff(old_name, old_text, new_name, new_text, session_token, real_path=None):
    """Generate a VS Code-style side-by-side diff page."""
    back_link = f'<a href="/list/{session_token}">\u2190 List</a>'
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    display_name = os.path.basename(real_path) if real_path else os.path.splitext(old_name.split("_", 2)[-1])[0] + os.path.splitext(old_name)[1] if "_" in old_name else old_name
    old_ts = _snap_ts_label(old_name)
    new_ts = _snap_ts_label(new_name)

    table_rows, add_count, del_count = _make_diff_rows(old_lines, new_lines)

    if not table_rows:
        diff_html = '<div class="no-preview">No differences found.</div>'
    else:
        diff_html = (
            f'<div class="diff-fheader">'
            f'<div><span class="fh-old">Old File</span></div>'
            f'<div><span class="fh-new">New File</span></div></div>'
            f'<div class="diff-wrap"><table class="diff-table">'
            f'<colgroup><col class="ln"><col class="mk"><col>'
            f'<col class="gt">'
            f'<col class="ln"><col class="mk"><col></colgroup>'
            f'{"".join(table_rows)}</table></div>')

    theme_btn = '<button id="theme-toggle" class="outline-btn outline-btn--primary" onclick="toggleTheme()" title="Toggle theme">\u263D</button>'
    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diff: {html.escape(display_name)}</title><style>{_CSS}</style></head>
<body><div class="container diff-page">
<div class="topbar">{back_link}<span class="fname">{html.escape(display_name)}</span>{theme_btn}{lang_sel}</div>
<div class="diff-meta">
  <div style="color:#8b949e;font-size:0.85em">{old_ts} \u2192 {new_ts}</div>
  <div class="diff-stats"><span class="add-count">+{add_count}</span><span class="del-count">\u2212{del_count}</span></div>
</div>
<hr class="separator">
{diff_html}
</div>{_JS}</body></html>"""


def _diff_fragment(old_name, old_text, new_name, new_text):
    """Generate just the diff HTML fragment (no full page wrapper). For AJAX loading."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    old_ts = _snap_ts_label(old_name)
    new_ts = _snap_ts_label(new_name)

    table_rows, add_count, del_count = _make_diff_rows(old_lines, new_lines)

    if not table_rows:
        return '<div class="no-preview">No differences found.</div>'

    return (
        f'<div class="diff-fheader">'
        f'<div><span class="fh-old">Old File ({old_ts})</span></div>'
        f'<div><span class="fh-new">New File ({new_ts})</span></div></div>'
        f'<div class="diff-meta" style="border-radius:0;border-top:none;margin:0">'
        f'<div></div>'
        f'<div class="diff-stats"><span class="add-count">+{add_count}</span>'
        f'<span class="del-count">\u2212{del_count}</span></div></div>'
        f'<div class="diff-wrap" style="border-top:none;border-radius:0 0 8px 8px">'
        f'<table class="diff-table">'
        f'<colgroup><col class="ln"><col class="mk"><col>'
        f'<col class="gt">'
        f'<col class="ln"><col class="mk"><col></colgroup>'
        f'{"".join(table_rows)}</table></div>')


def _page_diff_tool(entries, session_token):
    """Generate the interactive diff comparison tool page."""
    back_link = f'<a href="/list/{session_token}" data-i18n="back_list">\u2190 List</a>'
    theme_btn = '<button id="theme-toggle" class="outline-btn outline-btn--primary" onclick="toggleTheme()" title="Toggle theme">\u263D</button>'
    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')

    # Build file → snapshots data for JS
    file_map = defaultdict(list)
    for entry in entries:
        snap = entry.get("snapshot")
        if not snap:
            continue
        path = entry["path"]
        ts = entry.get("ts", "")
        ts_fmt = _format_ts(ts)
        op = entry.get("op", "edit")
        run_id = entry.get("run_id", 0)
        label = entry.get("run_label", "")
        file_map[path].append({
            "snap": snap, "ts": ts, "ts_fmt": ts_fmt,
            "op": op, "run_id": run_id, "label": label[:50],
        })

    # Filter to files with 2+ snapshots, sort snapshots by time
    diff_files = {}
    for path, snaps in file_map.items():
        if len(snaps) < 2:
            continue
        snaps.sort(key=lambda s: s["ts"])
        items = []
        for s in snaps:
            disp = f'{s["ts_fmt"]} [{_op_label_short(s["op"])}]'
            if s["run_id"]:
                disp += f' (#{s["run_id"]})'
            items.append({"snap": s["snap"], "label": disp})
        diff_files[path] = items

    diff_files_json = json.dumps(diff_files, ensure_ascii=False)

    # Build file options
    if diff_files:
        file_options = ['<option value="">--</option>']
        for path in sorted(diff_files.keys()):
            fname = os.path.basename(path)
            file_options.append(f'<option value="{html.escape(path)}">{html.escape(fname)}'
                                f' <span style="color:#8b949e">({html.escape(os.path.dirname(path))})</span></option>')
        file_select_html = "\n".join(file_options)
        body_html = f"""
<div class="dt-panel">
  <label data-i18n="dt_select">\ud30c\uc77c \uc120\ud0dd</label>
  <select id="dt-file" onchange="dtFileChange(this,'{session_token}')">
    {file_select_html}
  </select>
  <div class="dt-hint" data-i18n="dt_hint">\uc2a4\ub0c5\uc0f7\uc774 2\uac1c \uc774\uc0c1\uc778 \ud30c\uc77c\ub9cc \ud45c\uc2dc\ub429\ub2c8\ub2e4</div>
  <div class="dt-snap-row">
    <div>
      <label data-i18n="dt_left">\uc88c\uce21 (\uc774\uc804)</label>
      <select id="dt-snap-l" disabled onchange="dtSnapChange('{session_token}')">
        <option value="">--</option>
      </select>
    </div>
    <div class="dt-snap-divider">\u2192</div>
    <div>
      <label data-i18n="dt_right">\uc6b0\uce21 (\uc774\ud6c4)</label>
      <select id="dt-snap-r" disabled onchange="dtSnapChange('{session_token}')">
        <option value="">--</option>
      </select>
    </div>
  </div>
  <div class="dt-info" id="dt-info"></div>
</div>
<div class="dt-result diff-page" id="dt-result"></div>"""
    else:
        body_html = '<div class="no-preview" data-i18n="dt_no_files">No files for comparison.</div>'

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diff Tool</title><style>{_CSS}</style></head>
<body><div class="container diff-page">
<div class="topbar">{back_link}<span class="fname" data-i18n="dt_title">Diff \ube44\uad50 \ub3c4\uad6c</span>{theme_btn}{lang_sel}</div>
<hr class="separator">
{body_html}
</div>
<script>window._dtFiles={diff_files_json};</script>
{_JS}</body></html>"""


def _page_snapshot(snapshot_name, session_token):
    """Generate snapshot view page."""
    snapshot_path = os.path.join(_SNAPSHOTS_DIR, snapshot_name)
    if not os.path.isfile(snapshot_path):
        return None
    real = os.path.realpath(snapshot_path)
    if not real.startswith(os.path.realpath(_SNAPSHOTS_DIR)):
        return None

    fname = snapshot_name
    ftype = _file_type(snapshot_name)
    back_link = f'<a href="/list/{session_token}" data-i18n="back_list">\u2190 List</a>'

    ts_label = ""
    try:
        parts = snapshot_name.split("_")
        d, t = parts[0], parts[1]
        ts_label = f"{d[2:4]}.{d[4:6]}.{d[6:8]} {t[0:2]}:{t[2:4]}:{t[4:6]}"
    except Exception:
        pass

    content_html = ""
    if ftype == "code":
        try:
            with open(snapshot_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            # Use original file path for language detection
            from state import find_path_for_snapshot
            orig_path = find_path_for_snapshot(snapshot_name) or snapshot_name
            content_html = _render_code_block(text, orig_path)
        except Exception as e:
            content_html = f'<div class="no-preview">Cannot read snapshot: {html.escape(str(e))}</div>'
    elif ftype == "image":
        content_html = f'<img class="img-preview" src="/snapshot-raw/{session_token}/{snapshot_name}" alt="{html.escape(fname)}">'
    else:
        content_html = '<div class="no-preview" data-i18n="no_preview">Preview not available.</div>'

    snap_badge = f'<span class="snap-label">\U0001f4cb <span data-i18n="snapshot">Snapshot</span>: {ts_label}</span>' if ts_label else ""
    rb_btn = (f' <a href="javascript:void(0)" onclick="doRollbackFile(\'/rollback/{session_token}/{snapshot_name}\')"'
              f' style="color:#d29922;font-size:0.85em;margin-left:10px">\u21a9 <span data-i18n="rollback">Rollback</span></a>')
    theme_btn = '<button id="theme-toggle" class="outline-btn outline-btn--primary" onclick="toggleTheme()" title="Toggle theme">\u263D</button>'
    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Snapshot: {html.escape(fname)}</title><style>{_CSS}</style></head>
<body><div class="container">
<div class="topbar">{back_link}<span class="fname">{html.escape(fname)}{snap_badge}{rb_btn}</span>{theme_btn}{lang_sel}</div>
<hr class="separator">
{content_html}
</div>{_JS}</body></html>"""


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
def _page_settings(session_token):
    """Generate the Settings web page with all configurable options."""
    from config import settings, DEFAULT_SETTINGS, TOKEN_PERIODS, THEME_OPTIONS, AI_MODELS, LANG, WORK_DIR
    import json as _json

    theme_btn = '<button id="theme-toggle" class="outline-btn outline-btn--primary" onclick="toggleTheme()" title="Toggle theme">\u263D</button>'
    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')

    s = dict(settings)
    s["bot_lang"] = LANG
    s["work_dir"] = WORK_DIR
    settings_json = _json.dumps(s)
    timeout_seconds = int(s.get("settings_timeout_minutes", 15)) * 60
    ai_models_json = _json.dumps(AI_MODELS)
    cli_status_json = _json.dumps(state.cli_status)
    token_periods_json = _json.dumps(TOKEN_PERIODS)
    theme_options_json = _json.dumps(THEME_OPTIONS)

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sumone Settings</title><style>{_CSS}
.settings-section {{ margin-bottom: 24px; }}
.settings-section h2 {{ color: var(--accent-blue); font-size: 1em; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border-muted); }}
.setting-row {{ display: flex; align-items: center; justify-content: space-between; padding: 12px 16px;
  background: var(--bg-raised); border: 1px solid var(--border-muted); border-radius: var(--radius-md); margin-bottom: 8px; }}
.setting-label {{ flex: 1; }}
.setting-label .name {{ color: var(--text-primary); font-weight: 500; font-size: var(--text-sm); }}
.setting-label .desc {{ color: var(--text-secondary); font-size: var(--text-xs); margin-top: 2px; }}
.setting-control {{ flex-shrink: 0; margin-left: 16px; }}
/* Toggle switch */
.toggle {{ position: relative; display: inline-block; width: 44px; height: 24px; }}
.toggle input {{ opacity: 0; width: 0; height: 0; }}
.toggle .slider {{ position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
  background: #555; border-radius: 24px; transition: 0.2s; }}
.toggle .slider::before {{ content: ''; position: absolute; height: 18px; width: 18px; top: 3px; left: 3px;
  background: #fff; border-radius: 50%; transition: 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.4); }}
.toggle input:checked + .slider {{ background: var(--accent-blue); }}
.toggle input:checked + .slider::before {{ transform: translateX(20px); }}
/* Select in settings */
.setting-select {{ background: var(--bg-base); border: 1px solid var(--border-default); color: var(--text-primary);
  padding: 6px 10px; border-radius: var(--radius-md); font-size: var(--text-sm); outline: none; cursor: pointer; }}
.setting-select:focus {{ border-color: var(--accent-blue); }}
/* Number input */
.setting-number {{ background: var(--bg-base); border: 1px solid var(--border-default); color: var(--text-primary);
  padding: 6px 10px; border-radius: var(--radius-md); font-size: var(--text-sm); width: 80px; text-align: center; outline: none; }}
.setting-number:focus {{ border-color: var(--accent-blue); }}
/* AI Provider accordion */
.ai-provider {{ border: 1px solid var(--border-muted); border-radius: var(--radius-md); margin-bottom: 8px; overflow: hidden; }}
.ai-provider-header {{ display: flex; align-items: center; padding: 12px 16px; background: var(--bg-raised); cursor: pointer; user-select: none; gap: 10px; }}
.ai-provider-header.disconnected {{ cursor: default; }}
.ai-status-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.ai-status-dot.connected {{ background: #4caf50; }}
.ai-status-dot.disconnected {{ background: #666; }}
.ai-provider-name {{ flex: 1; font-weight: 500; font-size: var(--text-sm); color: var(--text-primary); }}
.ai-provider-arrow {{ font-size: 10px; color: var(--text-secondary); transition: transform 0.2s; }}
.ai-provider.open .ai-provider-arrow {{ transform: rotate(180deg); }}
.ai-connect-btn {{ padding: 5px 12px; border-radius: var(--radius-md); border: 1px solid var(--accent-blue); background: transparent;
  color: var(--accent-blue); font-size: var(--text-xs); cursor: pointer; }}
.ai-connect-btn:hover {{ background: var(--accent-blue); color: #fff; }}
.ai-provider-models {{ display: none; border-top: 1px solid var(--border-muted); }}
.ai-provider.open .ai-provider-models {{ display: block; }}
.ai-model-row {{ display: flex; align-items: center; padding: 10px 16px; gap: 12px; border-bottom: 1px solid var(--border-muted); }}
.ai-model-row:last-child {{ border-bottom: none; }}
.ai-model-name {{ flex: 1; font-size: var(--text-sm); color: var(--text-secondary); font-family: monospace; }}
.ai-model-row.active .ai-model-name {{ color: var(--text-primary); font-weight: 500; }}
.ai-model-btn {{ padding: 5px 12px; border-radius: var(--radius-md); font-size: var(--text-xs); cursor: pointer; border: 1px solid var(--border-default); background: transparent; color: var(--text-secondary); white-space: nowrap; }}
.ai-model-btn:hover {{ border-color: var(--accent-blue); color: var(--accent-blue); }}
.ai-model-btn.active {{ background: var(--accent-blue); color: #fff; border-color: var(--accent-blue); cursor: default; }}
.setting-number::-webkit-inner-spin-button,
.setting-number::-webkit-outer-spin-button {{ opacity: 1; filter: invert(1); }}
/* Save button */
.save-bar {{ position: sticky; bottom: 0; background: var(--bg-base); padding: 12px 0; border-top: 1px solid var(--border-muted);
  display: flex; gap: 8px; justify-content: flex-end; }}
.save-btn {{ background: var(--accent-blue); color: #fff; border: none; padding: 10px 24px;
  border-radius: var(--radius-md); cursor: pointer; font-size: var(--text-sm); font-weight: 500; }}
.save-btn:hover {{ filter: brightness(1.1); }}
.save-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.save-status {{ color: var(--color-add); font-size: var(--text-sm); align-self: center; display: none; }}
/* Text input in settings */
.setting-text {{ background: var(--bg-base); border: 1px solid var(--border-default); color: var(--text-primary);
  padding: 6px 10px; border-radius: var(--radius-md); font-size: var(--text-sm); width: 200px; outline: none; font-family: var(--font-mono); }}
.setting-text:focus {{ border-color: var(--accent-blue); }}
/* Sticky topbar */
.topbar {{ position: sticky; top: 0; z-index: 100; background: var(--bg-base); padding-bottom: 8px; }}
/* Session timer */
.session-timer {{ font-size: 11px; color: var(--text-secondary); font-family: var(--font-mono);
  margin-left: 8px; opacity: 0.6; vertical-align: middle; }}
.session-timer.warn {{ color: var(--color-del); opacity: 1; font-weight: bold; animation: blink 1s step-end infinite; }}
@keyframes blink {{ 50% {{ opacity: 0.3; }} }}
</style></head>
<body><div class="container">
<div class="topbar"><span class="fname">\u2699 Sumone Settings <span id="session-timer" class="session-timer">--:--</span></span>{theme_btn}{lang_sel}</div>
<hr class="separator">

<div class="settings-section">
  <h2>\U0001f4ca <span data-i18n="s_disp">Display</span></h2>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_cost">Show Cost</div><div class="desc" data-i18n="s_cost_d">Show API cost after each response</div></div>
    <div class="setting-control"><label class="toggle"><input type="checkbox" data-key="show_cost"><span class="slider"></span></label></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_status">Show Status</div><div class="desc" data-i18n="s_status_d">Show tool usage status during processing</div></div>
    <div class="setting-control"><label class="toggle"><input type="checkbox" data-key="show_status"><span class="slider"></span></label></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_global">Show Global Cost</div><div class="desc" data-i18n="s_global_d">Show total cost in /cost command</div></div>
    <div class="setting-control"><label class="toggle"><input type="checkbox" data-key="show_global_cost"><span class="slider"></span></label></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_remote">Show Remote Tokens</div><div class="desc" data-i18n="s_remote_d">Include remote bot tokens in footer</div></div>
    <div class="setting-control"><label class="toggle"><input type="checkbox" data-key="show_remote_tokens"><span class="slider"></span></label></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_tperiod">Token Display Period</div><div class="desc" data-i18n="s_tperiod_d">Token count period shown in footer</div></div>
    <div class="setting-control"><select class="setting-select" data-key="token_display" id="sel-token-display"></select></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_typing">Typing Indicator</div><div class="desc" data-i18n="s_typing_d">Show '···' message while processing</div></div>
    <div class="setting-control"><label class="toggle"><input type="checkbox" data-key="show_typing"><span class="slider"></span></label></div>
  </div>
</div>

<div class="settings-section">
  <h2>\U0001f3a8 <span data-i18n="s_appear">Appearance</span></h2>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_theme">Theme</div><div class="desc" data-i18n="s_theme_d">File viewer color theme</div></div>
    <div class="setting-control"><select class="setting-select" data-key="theme" id="sel-theme"></select></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_botlang">Bot Language</div><div class="desc" data-i18n="s_botlang_d">Bot response and message language (applied immediately)</div></div>
    <div class="setting-control"><select class="setting-select" id="sel-bot-lang">
      <option value="ko">한국어</option><option value="en">English</option>
    </select></div>
  </div>
</div>

<div class="settings-section">
  <h2>\U0001f4be <span data-i18n="s_storage">Storage</span></h2>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_snap">Snapshot Retention</div><div class="desc" data-i18n="s_snap_d">Days to keep file snapshots</div></div>
    <div class="setting-control"><input type="number" class="setting-number" data-key="snapshot_ttl_days" min="1" max="365"></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_tttl">Viewer Token TTL</div><div class="desc" data-i18n="s_tttl_d">File viewer link expiry</div></div>
    <div class="setting-control"><select class="setting-select" data-key="token_ttl" id="sel-token-ttl">
      <option value="session" data-i18n="s_ttl_sess">Session (bot lifetime)</option>
      <option value="unlimited" data-i18n="s_ttl_unltd">Unlimited</option>
    </select></div>
  </div>
  <div class="setting-row" id="ttl-minutes-row" style="display:none">
    <div class="setting-label"><div class="name" data-i18n="s_tmin">Token TTL (minutes)</div><div class="desc" data-i18n="s_tmin_d">1-60 minutes</div></div>
    <div class="setting-control"><input type="number" class="setting-number" id="ttl-minutes-input" min="1" max="60" value="30"></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_autolink">Auto Viewer Link</div><div class="desc" data-i18n="s_autolink_d">Send file viewer link automatically when files change</div></div>
    <div class="setting-control"><label class="toggle"><input type="checkbox" data-key="auto_viewer_link"><span class="slider"></span></label></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_fixedlink">Fixed Link</div><div class="desc" data-i18n="s_fixedlink_d">Reuse same URL every time (bookmarkable)</div></div>
    <div class="setting-control"><label class="toggle"><input type="checkbox" data-key="viewer_link_fixed"><span class="slider"></span></label></div>
  </div>
</div>

<div class="settings-section">
  <h2>\U0001f916 <span data-i18n="s_ai">AI Model</span></h2>
  <div id="ai-providers"></div>
</div>

<div class="settings-section">
  <h2>\u2699\ufe0f <span data-i18n="s_system">System</span></h2>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_workdir">Work Directory</div><div class="desc" data-i18n="s_workdir_d">Root directory for file browsing</div></div>
    <div class="setting-control"><input type="text" id="workdir-input" class="setting-text" placeholder="/home/user"></div>
  </div>
  <div class="setting-row">
    <div class="setting-label"><div class="name" data-i18n="s_stimeout">Settings Page Timeout</div><div class="desc" data-i18n="s_stimeout_d">Auto-expire after N minutes of inactivity</div></div>
    <div class="setting-control"><input type="number" class="setting-number" id="stimeout-input" min="1" max="120"></div>
  </div>
</div>

<div class="save-bar">
  <span class="save-status" id="save-status" data-i18n="s_saved">\u2714 Saved</span>
  <button class="save-btn" id="save-btn" onclick="saveSettings()" data-i18n="s_save">Save</button>
</div>

</div>
<script>
var _settings = {settings_json};
var _aiModels = {ai_models_json};
var _cliStatus = {cli_status_json};
var _tokenPeriods = {token_periods_json};
var _themeOptions = {theme_options_json};
var _sessionToken = '{session_token}';
var _timeoutSecs = {timeout_seconds};
var _timerRemaining = _timeoutSecs;
var _timerInterval = null;
var _initialSettings = JSON.parse(JSON.stringify(_settings));
var _modelDirty = false;

function _putIfChanged(out, key, value) {{
  if (_initialSettings[key] !== value) out[key] = value;
}}

function _fmtTime(s) {{ var m=Math.floor(s/60),ss=s%60; return m+':'+(ss<10?'0':'')+ss; }}
function _updateTimerDisplay() {{
  var el = document.getElementById('session-timer');
  if (!el) return;
  el.textContent = _fmtTime(_timerRemaining);
  if (_timerRemaining <= 120) el.classList.add('warn'); else el.classList.remove('warn');
}}
function _resetTimer() {{ _timerRemaining = _timeoutSecs; _updateTimerDisplay(); }}
function _startTimer() {{
  _timerRemaining = _timeoutSecs; _updateTimerDisplay();
  clearInterval(_timerInterval);
  _timerInterval = setInterval(function() {{
    _timerRemaining--;
    _updateTimerDisplay();
    if (_timerRemaining <= 0) {{
      clearInterval(_timerInterval);
      document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px;color:#8b949e;font-family:monospace">'
        + '<div style="font-size:2.5em">\u23f0</div>'
        + '<div>' + (T('s_expired') || '세션이 만료되었습니다.') + '</div></div>';
      setTimeout(function() {{ window.close(); }}, 2500);
    }}
  }}, 1000);
}}

function initSettings() {{
  // Toggles
  document.querySelectorAll('.toggle input[data-key]').forEach(function(cb) {{
    cb.checked = !!_settings[cb.dataset.key];
    cb.addEventListener('change', markDirty);
  }});
  // Token display period
  var tds = document.getElementById('sel-token-display');
  _tokenPeriods.forEach(function(p) {{ tds.innerHTML += '<option value="'+p+'">'+p+'</option>'; }});
  tds.value = _settings.token_display || 'month';
  tds.addEventListener('change', markDirty);
  // Theme
  var ts = document.getElementById('sel-theme');
  _themeOptions.forEach(function(t) {{ ts.innerHTML += '<option value="'+t+'">'+t+'</option>'; }});
  ts.value = _settings.theme || 'system';
  ts.addEventListener('change', function() {{ applyTheme(this.value); markDirty(); }});
  // Bot language
  var bls = document.getElementById('sel-bot-lang');
  if (bls) {{ bls.value = _settings.bot_lang || 'ko'; bls.addEventListener('change', markDirty); }}
  // Snapshot TTL
  var snap = document.querySelector('input[data-key="snapshot_ttl_days"]');
  if (snap) {{ snap.value = _settings.snapshot_ttl_days || 7; snap.addEventListener('change', markDirty); }}
  // Token TTL
  var ttlSel = document.getElementById('sel-token-ttl');
  var ttlRow = document.getElementById('ttl-minutes-row');
  var ttlInput = document.getElementById('ttl-minutes-input');
  var curTtl = _settings.token_ttl || 'session';
  if (typeof curTtl === 'number' || (typeof curTtl === 'string' && /^\\d+$/.test(curTtl))) {{
    ttlSel.value = 'minutes';
    if (!ttlSel.querySelector('option[value="minutes"]')) {{
      ttlSel.insertAdjacentHTML('afterbegin', '<option value="minutes">Minutes</option>');
    }}
    ttlSel.value = 'minutes';
    ttlInput.value = parseInt(curTtl);
    ttlRow.style.display = '';
  }} else {{
    ttlSel.value = curTtl;
  }}
  // Add minutes option if not present
  if (!ttlSel.querySelector('option[value="minutes"]')) {{
    ttlSel.insertAdjacentHTML('afterbegin', '<option value="minutes">Minutes</option>');
  }}
  ttlSel.addEventListener('change', function() {{
    ttlRow.style.display = this.value === 'minutes' ? '' : 'none';
    markDirty();
  }});
  ttlInput.addEventListener('change', markDirty);
  // AI Provider accordion
  _buildAiProviders();
  // Work directory
  var wdi = document.getElementById('workdir-input');
  if (wdi) {{ wdi.value = _settings.work_dir || ''; wdi.addEventListener('change', markDirty); }}
  // Settings timeout
  var sti = document.getElementById('stimeout-input');
  if (sti) {{ sti.value = _settings.settings_timeout_minutes || 15; sti.addEventListener('change', markDirty); }}
}}
function _buildAiProviders() {{
  var container = document.getElementById('ai-providers');
  container.innerHTML = '';
  var activeModel = _settings.default_model || 'claude';
  var activeSub = _settings.default_sub_model || 'sonnet';
  Object.keys(_aiModels).forEach(function(provKey) {{
    var info = _aiModels[provKey];
    var connected = !!_cliStatus[provKey];
    var isOpen = (provKey === activeModel);
    var div = document.createElement('div');
    div.className = 'ai-provider' + (isOpen && connected ? ' open' : '');
    div.dataset.provider = provKey;
    // Header
    var hdr = document.createElement('div');
    hdr.className = 'ai-provider-header' + (connected ? '' : ' disconnected');
    var dot = '<span class="ai-status-dot ' + (connected ? 'connected' : 'disconnected') + '"></span>';
    var name = '<span class="ai-provider-name">' + info.label + '</span>';
    if (connected) {{
      var arrow = '<span class="ai-provider-arrow">▼</span>';
      hdr.innerHTML = dot + name + arrow;
      hdr.onclick = function() {{ _toggleProvider(div); }};
    }} else {{
      var connectBtn = '<button class="ai-connect-btn" onclick="event.stopPropagation();_connectProvider(\\\''+provKey+'\\\')" data-i18n="s_ai_connect">연결하기</button>';
      hdr.innerHTML = dot + name + connectBtn;
    }}
    div.appendChild(hdr);
    // Models panel
    if (connected && info.sub_models) {{
      var panel = document.createElement('div');
      panel.className = 'ai-provider-models';
      Object.keys(info.sub_models).forEach(function(subKey) {{
        var modelId = info.sub_models[subKey];
        var isActive = (provKey === activeModel && subKey === activeSub);
        var row = document.createElement('div');
        row.className = 'ai-model-row' + (isActive ? ' active' : '');
        row.dataset.provider = provKey;
        row.dataset.sub = subKey;
        var btnLabel = isActive ? T('s_ai_active') || '설정됨' : T('s_ai_set') || '설정하기';
        var btnClass = 'ai-model-btn' + (isActive ? ' active' : '');
        row.innerHTML = '<span class="ai-model-name">'+modelId+'</span>'
          + '<button class="'+btnClass+'" '+(isActive?'disabled':'')+' onclick="_setModel(\\\''+provKey+'\\\',\\\''+subKey+'\\\')">'+btnLabel+'</button>';
        panel.appendChild(row);
      }});
      div.appendChild(panel);
    }}
    container.appendChild(div);
  }});
}}
function _toggleProvider(div) {{
  var wasOpen = div.classList.contains('open');
  document.querySelectorAll('.ai-provider').forEach(function(d) {{ d.classList.remove('open'); }});
  if (!wasOpen) div.classList.add('open');
}}
function _setModel(provKey, subKey) {{
  _settings.default_model = provKey;
  _settings.default_sub_model = subKey;
  _modelDirty = true;
  _buildAiProviders();
  markDirty();
}}
function _connectProvider(provKey) {{
  if (window.__connectPending) return;
  window.__connectPending = true;
  fetch('/settings-connect/'+_sessionToken+'?provider='+provKey, {{method:'POST'}})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      if (d.ok) alert((T('s_ai_connect_started')||'연결을 시작합니다. 텔레그램을 확인하세요.'));
      else alert(d.error || 'Error');
    }})
    .catch(function() {{ alert('Error'); }})
    .finally(function() {{ window.__connectPending = false; }});
}}
function markDirty() {{
  document.getElementById('save-status').style.display = 'none';
  _resetTimer();
}}
function gatherSettings() {{
  var s = {{}};
  document.querySelectorAll('.toggle input[data-key]').forEach(function(cb) {{
    _putIfChanged(s, cb.dataset.key, cb.checked);
  }});
  _putIfChanged(s, 'token_display', document.getElementById('sel-token-display').value);
  _putIfChanged(s, 'theme', document.getElementById('sel-theme').value);
  var snap = document.querySelector('input[data-key="snapshot_ttl_days"]');
  _putIfChanged(s, 'snapshot_ttl_days', parseInt(snap.value) || 7);
  var ttlSel = document.getElementById('sel-token-ttl');
  var tokenTtl = ttlSel.value;
  if (ttlSel.value === 'minutes') {{
    tokenTtl = parseInt(document.getElementById('ttl-minutes-input').value) || 30;
  }}
  _putIfChanged(s, 'token_ttl', tokenTtl);
  if (_modelDirty) {{
    _putIfChanged(s, 'default_model', _settings.default_model || 'claude');
    _putIfChanged(s, 'default_sub_model', _settings.default_sub_model || 'sonnet');
    s._model_dirty = true;
  }}
  var bls = document.getElementById('sel-bot-lang');
  if (bls) {{ _putIfChanged(s, 'bot_lang', bls.value); }}
  var wdi = document.getElementById('workdir-input');
  if (wdi) {{ _putIfChanged(s, 'work_dir', wdi.value.trim()); }}
  var sti = document.getElementById('stimeout-input');
  if (sti) {{ _putIfChanged(s, 'settings_timeout_minutes', parseInt(sti.value) || 15); }}
  return s;
}}
function saveSettings() {{
  var btn = document.getElementById('save-btn');
  btn.disabled = true;
  btn.textContent = T('s_saving');
  fetch('/settings-save/' + _sessionToken, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(gatherSettings())
  }}).then(function(r) {{
    if (r.ok) {{
      r.text().then(function(txt) {{
        if (txt === 'RESTART') {{
          btn.textContent = T('s_restarting') || '재시작 중...';
          clearInterval(_timerInterval);
          var timerEl = document.getElementById('session-timer');
          if (timerEl) timerEl.textContent = '';
          setTimeout(function() {{ window.close(); }}, 3000);
        }} else {{
          document.getElementById('save-status').style.display = '';
          btn.textContent = T('s_saved') || '✔ 저장됨';
          btn.disabled = true;
          _initialSettings = JSON.parse(JSON.stringify(_settings));
          _modelDirty = false;
          setTimeout(function() {{ window.close(); }}, 1500);
        }}
      }});
    }} else {{
      r.text().then(function(t) {{ alert('Save failed: ' + t); btn.textContent = T('s_save'); btn.disabled = false; }});
    }}
  }}).catch(function() {{ alert('Save failed'); btn.textContent = T('s_save'); btn.disabled = false; }});
}}
document.addEventListener('DOMContentLoaded', function() {{
  initSettings();
  _startTimer();
  document.addEventListener('mousemove', _resetTimer);
  document.addEventListener('keydown', _resetTimer);
  document.addEventListener('click', _resetTimer);
}});
</script>
{_JS}</body></html>"""


# ---------------------------------------------------------------------------
# Rollback logic
# ---------------------------------------------------------------------------
def _do_rollback_file(snapshot_name):
    """Rollback a single file to a snapshot. Backs up current file first.
    Returns (success: bool, message: str)."""
    from state import add_modified_file, find_path_for_snapshot
    snap_path = os.path.join(_SNAPSHOTS_DIR, snapshot_name)
    if not os.path.isfile(snap_path):
        return False, "Snapshot not found"
    real = os.path.realpath(snap_path)
    if not real.startswith(os.path.realpath(_SNAPSHOTS_DIR)):
        return False, "Invalid snapshot"

    original_path = find_path_for_snapshot(snapshot_name)
    if not original_path:
        return False, "Cannot find original file path for this snapshot"

    try:
        snap_content = open(snap_path, "rb").read()
    except Exception as e:
        return False, f"Cannot read snapshot: {e}"

    # Backup current file before rollback (if it exists)
    if os.path.isfile(original_path):
        try:
            with open(original_path, "rb") as f:
                cur_content = f.read()
            add_modified_file(original_path,
                              content=cur_content.decode("utf-8", errors="replace"),
                              op="rollback-backup")
        except Exception:
            pass

    # Restore snapshot to original path
    try:
        os.makedirs(os.path.dirname(original_path), exist_ok=True)
        with open(original_path, "wb") as f:
            f.write(snap_content)
    except Exception as e:
        return False, f"Cannot write file: {e}"

    # Record rollback
    add_modified_file(original_path,
                      content=snap_content.decode("utf-8", errors="replace"),
                      op="rollback")
    log.info("Rolled back %s to snapshot %s", original_path, snapshot_name)
    return True, "OK"


def _do_rollback_cycle(run_id, entries):
    """Restore all files to the state AT the end of the specified cycle.
    e.g. restoring to #6 keeps #6's changes, undoes #7, #8, etc.
    Returns (success: bool, message: str, count: int)."""
    from state import add_modified_file

    # Collect paths that may need restoring:
    # 1) Paths with original edits after the target cycle
    # 2) Paths that were affected by any rollback (state may differ from original)
    affected_paths = set()
    for e in entries:
        rid = e.get("run_id", 0)
        op = e.get("op", "")
        if rid > run_id and op not in ("rollback-backup", "rollback"):
            affected_paths.add(e["path"])
        if op == "rollback":
            affected_paths.add(e["path"])

    if not affected_paths:
        return False, "Nothing to restore", 0

    affected_paths = list(affected_paths)
    restored = 0
    for path in affected_paths:
        all_entries_for_path = [e for e in entries if e["path"] == path]
        all_entries_for_path.sort(key=lambda e: e["ts"])

        # Find the most recent ORIGINAL snapshot at or before the target cycle
        # (exclude rollback/rollback-backup entries to get the true original state)
        target_snapshot = None
        for e in reversed(all_entries_for_path):
            if (e.get("run_id", 0) <= run_id and e.get("snapshot")
                    and e.get("op") not in ("rollback", "rollback-backup")):
                target_snapshot = e["snapshot"]
                break

        # Skip if current file already matches target snapshot
        if target_snapshot:
            snap_path = os.path.join(_SNAPSHOTS_DIR, target_snapshot)
            try:
                with open(snap_path, "rb") as sf, open(path, "rb") as cf:
                    if sf.read() == cf.read():
                        continue  # Already at target state
            except Exception:
                pass

        # Backup current file
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    cur = f.read()
                add_modified_file(path, content=cur.decode("utf-8", errors="replace"),
                                  op="rollback-backup")
            except Exception:
                pass

        if target_snapshot:
            # Restore to the target cycle's snapshot
            snap_path = os.path.join(_SNAPSHOTS_DIR, target_snapshot)
            try:
                with open(snap_path, "rb") as f:
                    content = f.read()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(content)
                add_modified_file(path, content=content.decode("utf-8", errors="replace"),
                                  op="rollback")
                restored += 1
            except Exception as ex:
                log.warning("Failed to restore %s: %s", path, ex)
        else:
            # No snapshot at or before the target cycle — file was created after it.
            # Delete to match the state at the target cycle.
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    add_modified_file(path, content=None, op="delete")
                    restored += 1
                except Exception as ex:
                    log.warning("Failed to delete %s: %s", path, ex)

    if restored == 0:
        return True, "SAME_STATE", 0
    log.info("Restore to cycle #%d: %d/%d files restored", run_id, restored, len(affected_paths))
    return True, "OK", restored


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class _ViewerHandler(BaseHTTPRequestHandler):
    """Read-only file viewer HTTP handler."""

    modified_entries = []          # list of entry dicts
    session_tokens = {}            # file viewer session tokens
    settings_session_tokens = {}   # settings page session tokens (separate)
    settings_msg_id = None         # message ID of the settings link to delete on save

    def log_message(self, format, *args):
        log.debug("FileViewer: %s", format % args)

    def _send_html(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _send_error(self, code=403, msg="Access Denied"):
        self._send_html(code, f"""<!DOCTYPE html><html><head><meta charset="utf-8">
        <style>{_CSS}</style></head><body><div class="container">
        <h1>{code} {html.escape(msg)}</h1></div></body></html>""")

    def _send_text(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def _send_json(self, data):
        import json as _json
        body = _json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_unique_files(self):
        return _aggregate_files(self.modified_entries)

    def _validate_file_index(self, idx_str, allow_missing=False):
        unique = self._get_unique_files()
        try:
            idx = int(idx_str)
        except (ValueError, TypeError):
            return None
        if idx < 0 or idx >= len(unique):
            return None
        fpath = unique[idx]["path"]
        real = os.path.realpath(fpath)
        if not os.path.isfile(real) and not allow_missing:
            return None
        return idx, real

    def _validate_snapshot(self, snapshot_name):
        if "/" in snapshot_name or "\\" in snapshot_name or ".." in snapshot_name:
            return None
        snapshot_path = os.path.join(_SNAPSHOTS_DIR, snapshot_name)
        real = os.path.realpath(snapshot_path)
        if not real.startswith(os.path.realpath(_SNAPSHOTS_DIR)):
            return None
        if not os.path.isfile(real):
            return None
        return real

    def _check_session(self, path_parts):
        """Validate session token from path_parts. Returns session_token or None."""
        if len(path_parts) < 2:
            return None
        st = path_parts[1]
        if st in _ViewerHandler.session_tokens:
            return st
        if st in _ViewerHandler.settings_session_tokens:
            from config import settings
            timeout = settings.get("settings_timeout_minutes", 15) * 60
            last_activity = _ViewerHandler.settings_session_tokens[st]
            if _time.time() - last_activity > timeout:
                _ViewerHandler.settings_session_tokens.pop(st, None)
                return None
            _ViewerHandler.settings_session_tokens[st] = _time.time()
            return st
        return None

    def do_GET(self):
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip("/").split("/")

        if not path_parts or not path_parts[0]:
            params = parse_qs(parsed.query)
            token_list = params.get("token", [])
            if not token_list or not _validate_token(token_list[0]):
                self._send_error(403, "Invalid or expired token")
                return
            session_token = secrets.token_urlsafe(16)
            _ViewerHandler.session_tokens[session_token] = True
            self.send_response(302)
            self.send_header("Location", f"/list/{session_token}")
            self.end_headers()
            return

        action = path_parts[0]

        # Settings entry point: /settings?token=xxx → create settings session → redirect
        if action == "settings" and len(path_parts) == 1:
            params = parse_qs(parsed.query)
            token_list = params.get("token", [])
            if not token_list or not _validate_settings_token(token_list[0]):
                self._send_error(403, "Invalid or expired settings token")
                return
            session_token = secrets.token_urlsafe(16)
            _ViewerHandler.settings_session_tokens[session_token] = _time.time()
            self.send_response(302)
            self.send_header("Location", f"/settings/{session_token}")
            self.end_headers()
            return

        session_token = self._check_session(path_parts)
        if not session_token:
            self._send_error(403, "Session expired")
            return

        if action == "settings":
            body = _page_settings(session_token)
            self._send_html(200, body)
            return

        if action == "list":
            body = _page_list(self.modified_entries, session_token)
            self._send_html(200, body)
            return

        if action == "view" and len(path_parts) >= 3:
            result = self._validate_file_index(path_parts[2], allow_missing=True)
            if not result:
                self._send_error(404, "File not found")
                return
            idx, fpath = result
            if not os.path.isfile(fpath):
                body = _page_deleted(fpath, idx, session_token)
                self._send_html(200, body)
                return
            body = _page_view(fpath, idx, session_token)
            self._send_html(200, body)
            return

        if action == "diff" and len(path_parts) >= 4:
            snap_old = path_parts[2]
            snap_new = path_parts[3]
            old_text = _read_snapshot(snap_old)
            new_text = _read_snapshot(snap_new)
            if old_text is None or new_text is None:
                self._send_error(404, "Snapshot not found")
                return
            from state import find_path_for_snapshot
            real_path = find_path_for_snapshot(snap_new) or find_path_for_snapshot(snap_old)
            body = _page_diff(snap_old, old_text, snap_new, new_text, session_token, real_path=real_path)
            self._send_html(200, body)
            return

        if action == "diff-tool":
            body = _page_diff_tool(self.modified_entries, session_token)
            self._send_html(200, body)
            return

        if action == "diff-fragment" and len(path_parts) >= 4:
            snap_old = path_parts[2]
            snap_new = path_parts[3]
            old_text = _read_snapshot(snap_old)
            new_text = _read_snapshot(snap_new)
            if old_text is None or new_text is None:
                self._send_error(404, "Snapshot not found")
                return
            frag = _diff_fragment(snap_old, old_text, snap_new, new_text)
            self._send_html(200, frag)
            return

        if action == "snapshot" and len(path_parts) >= 3:
            snapshot_name = path_parts[2]
            body = _page_snapshot(snapshot_name, session_token)
            if not body:
                self._send_error(404, "Snapshot not found")
                return
            self._send_html(200, body)
            return

        if action == "snapshot-raw" and len(path_parts) >= 3:
            snapshot_name = path_parts[2]
            real = self._validate_snapshot(snapshot_name)
            if not real:
                self._send_error(404, "Snapshot not found")
                return
            if _file_type(real) != "image":
                self._send_error(403, "Not an image")
                return
            mime, _ = mimetypes.guess_type(real)
            mime = mime or "image/png"
            try:
                with open(real, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self._send_error(500, "Read error")
            return

        if action == "snapshot-dl" and len(path_parts) >= 3:
            snapshot_name = path_parts[2]
            real = self._validate_snapshot(snapshot_name)
            if not real:
                self._send_error(404, "Snapshot not found")
                return
            parts = snapshot_name.split("_", 2)
            fname = parts[2] if len(parts) >= 3 else snapshot_name
            mime, _ = mimetypes.guess_type(real)
            mime = mime or "application/octet-stream"
            try:
                with open(real, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self._send_error(500, "Read error")
            return

        if action == "download" and len(path_parts) >= 3:
            result = self._validate_file_index(path_parts[2])
            if not result:
                self._send_error(404, "File not found")
                return
            idx, fpath = result
            fname = os.path.basename(fpath)
            mime, _ = mimetypes.guess_type(fpath)
            mime = mime or "application/octet-stream"
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self._send_error(500, "Read error")
            return

        if action == "raw" and len(path_parts) >= 3:
            result = self._validate_file_index(path_parts[2])
            if not result:
                self._send_error(404, "File not found")
                return
            idx, fpath = result
            if _file_type(fpath) != "image":
                self._send_error(403, "Not an image")
                return
            mime, _ = mimetypes.guess_type(fpath)
            mime = mime or "image/png"
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self._send_error(500, "Read error")
            return

        self._send_error()

    def do_POST(self):
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            self._send_error(405)
            return
        session_token = self._check_session(path_parts)
        if not session_token:
            self._send_error(403, "Session expired")
            return

        action = path_parts[0]

        if action == "settings-save":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len).decode("utf-8")
                import json as _json
                new_settings = _json.loads(body)
                from config import settings, update_config, AI_MODELS, LANG
                # Snapshot old values for change summary
                old_settings = dict(settings)
                old_lang = LANG
                from config import WORK_DIR as _OLD_WORK_DIR
                old_work_dir = _OLD_WORK_DIR
                # Handle bot_lang separately (top-level config key, not in settings dict)
                new_lang = old_lang
                if "bot_lang" in new_settings:
                    new_lang = new_settings.pop("bot_lang")
                    update_config("lang", new_lang)
                    import config as _cfg; _cfg.LANG = new_lang
                    import i18n as _i18n; _i18n.load(new_lang)
                # Handle work_dir separately (top-level config key)
                new_work_dir = old_work_dir
                if "work_dir" in new_settings:
                    new_work_dir = new_settings.pop("work_dir")
                    if new_work_dir and new_work_dir != old_work_dir:
                        update_config("work_dir", new_work_dir)
                        import config as _cfg2; _cfg2.WORK_DIR = new_work_dir
                        # Clear session so Claude starts fresh in the new work directory
                        update_config("session_id", None)
                        from state import state as _st2; _st2.session_id = None
                # Apply default model only when user explicitly changed model in this page session.
                model_dirty = bool(new_settings.pop("_model_dirty", False))
                if not model_dirty:
                    new_settings.pop("default_model", None)
                    new_settings.pop("default_sub_model", None)
                for k, v in new_settings.items():
                    settings[k] = v
                update_config("settings", dict(settings))
                # Apply default_model/sub_model to state
                sub = new_settings.get("default_sub_model")
                ai = new_settings.get("default_model")
                from state import state as _st, switch_provider
                if ai:
                    switch_provider(ai)
                    ai_info = AI_MODELS.get(ai)
                    if ai_info and sub:
                        resolved = ai_info["sub_models"].get(sub)
                        if resolved:
                            _st.model = resolved
                        else:
                            _st.model = None  # use CLI default
                    else:
                        _st.model = None
                    _st._provider_models[_st.provider] = _st.model
                    update_config("model", _st.model)
                    update_config("provider_models", dict(_st._provider_models))
                # Invalidate session (one-time use)
                _ViewerHandler.settings_session_tokens.pop(session_token, None)
                # Build and send change notification to Telegram
                try:
                    from telegram import tg_api, escape_html
                    from config import CHAT_ID, LANG as _LANG
                    names = _SETTING_NAMES.get(_LANG, _SETTING_NAMES["en"])
                    def _fmt(v):
                        if isinstance(v, bool):
                            return ("ON" if v else "OFF") if _LANG == "en" else ("켜짐" if v else "꺼짐")
                        return str(v)
                    changes = []
                    for k, new_v in new_settings.items():
                        old_v = old_settings.get(k)
                        if old_v != new_v:
                            label = names.get(k, k)
                            changes.append(f"{label}: {_fmt(old_v)} → {_fmt(new_v)}")
                    if old_lang != new_lang:
                        label = names.get("bot_lang", "Bot Language")
                        changes.append(f"{label}: {old_lang} → {new_lang}")
                    if old_work_dir != new_work_dir:
                        label = names.get("work_dir", "Work Directory")
                        changes.append(f"{label}: {old_work_dir} → {new_work_dir}")
                    if changes:
                        heading = "설정이 변경되었습니다." if _LANG == "ko" else "Settings updated."
                        lines = "\n".join(f"{i+1}. {c}" for i, c in enumerate(changes))
                        msg = f"<b>{heading}</b>\n{lines}"
                        # Delete old settings link message
                        if _ViewerHandler.settings_msg_id:
                            tg_api("deleteMessage", {"chat_id": CHAT_ID, "message_id": _ViewerHandler.settings_msg_id})
                            _ViewerHandler.settings_msg_id = None
                        tg_api("sendMessage", {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
                except Exception as ne:
                    log.warning("Settings notification error: %s", ne)
                log.info("Settings saved via web UI: %s", new_settings)
                # Schedule restart if work_dir changed
                _needs_restart = (old_work_dir != new_work_dir)
                self._send_text(200, "RESTART" if _needs_restart else "OK")
                if _needs_restart:
                    import threading as _thr, sys as _sys
                    def _do_restart():
                        _time.sleep(1.5)
                        try:
                            from telegram import tg_api as _tga
                            from config import CHAT_ID as _CID, LANG as _RL
                            _rmsg = ("작업 디렉토리 변경으로 봇을 재시작합니다." if _RL == "ko"
                                     else "Restarting bot due to work directory change.")
                            _tga("sendMessage", {"chat_id": _CID, "text": _rmsg})
                        except Exception:
                            pass
                        # Flush pending Telegram updates before restart
                        try:
                            from telegram import tg_api as _tga2
                            _upd = _tga2("getUpdates", {"timeout": 0})
                            if _upd and _upd.get("ok"):
                                _ulist = _upd.get("result", [])
                                if _ulist:
                                    _mid = max(u["update_id"] for u in _ulist)
                                    _tga2("getUpdates", {"offset": _mid + 1, "timeout": 0})
                        except Exception:
                            pass
                        try:
                            from main import _stop_file_viewer
                            _stop_file_viewer()
                        except Exception:
                            pass
                        _main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
                        _sys.stdout.flush()
                        os.execv(_sys.executable, [_sys.executable, _main_py])
                    _thr.Thread(target=_do_restart, daemon=True).start()
            except Exception as e:
                log.error("Settings save error: %s", e)
                self._send_text(500, str(e))
            return

        if action == "settings-connect":
            import urllib.parse as _up
            from config import AI_MODELS
            qs = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            provider = qs.get("provider", [None])[0]
            import json as _json
            if not provider or provider not in AI_MODELS:
                self._send_json({"ok": False, "error": "Unknown provider"})
                return
            from ai.connect import is_connect_active
            if is_connect_active():
                self._send_json({"ok": False, "error": "이미 다른 연결 작업이 진행 중입니다."})
                return
            from telegram import send_html, CHAT_ID, tg_api as _tga
            import i18n as _i18n
            prov_label = AI_MODELS[provider].get("label", provider.title())
            send_html(f"🔌 <b>{prov_label}</b> 연결을 시작합니다.\n잠시 후 안내 메시지가 전송됩니다.")
            import threading as _thr
            def _run_connect():
                from ai.connect import run_connect_flow
                run_connect_flow(provider)
            _thr.Thread(target=_run_connect, daemon=True).start()
            self._send_json({"ok": True})
            return

        if action == "clear":
            from state import clear_modified_files
            clear_modified_files()
            _ViewerHandler.modified_entries = []
            log.info("File viewer history cleared via web UI")
            self._send_text(200, "OK")
            return

        if action == "rollback" and len(path_parts) >= 3:
            snapshot_name = path_parts[2]
            ok, msg = _do_rollback_file(snapshot_name)
            if ok:
                from state import state
                _ViewerHandler.modified_entries = list(state.modified_files)
                self._send_text(200, "OK")
            else:
                self._send_text(400, msg)
            return

        if action == "rollback-cycle" and len(path_parts) >= 3:
            try:
                run_id = int(path_parts[2])
            except ValueError:
                self._send_text(400, "Invalid cycle ID")
                return
            ok, msg, count = _do_rollback_cycle(run_id, self.modified_entries)
            if ok and msg == "SAME_STATE":
                self._send_text(200, "SAME_STATE")
            elif ok:
                from state import state
                _ViewerHandler.modified_entries = list(state.modified_files)
                self._send_text(200, f"OK: {count} files restored")
            else:
                self._send_text(400, msg)
            return

        self._send_error(405, "Method Not Allowed")

    def do_PUT(self):
        self._send_error(405, "Method Not Allowed")

    def do_DELETE(self):
        self._send_error(405, "Method Not Allowed")

    def do_PATCH(self):
        self._send_error(405, "Method Not Allowed")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def _find_free_port():
    """Find a random available port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FileViewerServer:
    """Threaded read-only HTTP file viewer."""

    def __init__(self):
        self._server = None
        self._thread = None
        self.port = None

    def start(self, modified_entries=None):
        """Start the HTTP server on a random port."""
        if modified_entries is not None:
            _ViewerHandler.modified_entries = list(modified_entries)
        self.port = _find_free_port()
        self._server = HTTPServer(("127.0.0.1", self.port), _ViewerHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("File viewer server started on port %d", self.port)
        return self.port

    def update_files(self, modified_entries):
        """Update the list of modified file entries."""
        _ViewerHandler.modified_entries = list(modified_entries)

    def stop(self):
        """Shut down the server."""
        if self._server:
            self._server.shutdown()
            self._server = None
            log.info("File viewer server stopped")
