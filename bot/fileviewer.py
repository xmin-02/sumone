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

from config import SCRIPT_DIR, log

_SNAPSHOTS_DIR = os.path.join(SCRIPT_DIR, ".snapshots")

# ---------------------------------------------------------------------------
# Token management (session-scoped: valid until cleared)
# ---------------------------------------------------------------------------
_tokens = {}         # token -> True
_token_lock = threading.Lock()


def generate_token():
    """Generate a session-scoped access token."""
    token = secrets.token_urlsafe(32)
    with _token_lock:
        _tokens[token] = True
    return token


def _validate_token(token):
    """Validate token. Returns True if valid."""
    with _token_lock:
        return token in _tokens


def clear_tokens():
    """Invalidate all access tokens."""
    with _token_lock:
        _tokens.clear()
    _ViewerHandler.session_tokens.clear()


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
    """Aggregate entries by path. Returns list of {path, latest_ts, history}."""
    file_map = defaultdict(list)
    for entry in entries:
        file_map[entry["path"]].append(entry)
    result = []
    for path, hist in file_map.items():
        hist_sorted = sorted(hist, key=lambda e: e["ts"], reverse=True)
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
        "snapshot": "스냅샷", "cycle_rb": "사이클 롤백",
        "cycle_desc": "롤백할 사이클을 선택하세요. 해당 사이클에서 수정된 모든 파일이 이전 상태로 복원됩니다.",
        "cancel": "취소", "files_unit": "파일",
        "lines_hidden": "줄 숨김", "no_diff": "차이가 없습니다.",
        "del_title": "삭제됨", "del_msg": "이 파일은 삭제되었습니다.",
        "del_hint": "파일 목록의 히스토리 드롭다운에서 이전 스냅샷을 확인하세요.",
        "no_preview": "이 파일 유형은 미리보기를 지원하지 않습니다.\n위의 다운로드 버튼을 사용하세요.",
        "cfm_clear": "모든 파일 히스토리를 삭제하시겠습니까?\\n이 작업은 되돌릴 수 없습니다.",
        "cfm_rb": "이 스냅샷으로 파일을 롤백하시겠습니까?\\n현재 파일은 먼저 백업됩니다.",
        "cfm_cycle": "이 사이클의 모든 파일을 이전 상태로 롤백하시겠습니까?\\n현재 파일은 먼저 백업됩니다.",
        "rb_done": "롤백 완료!", "cycle_done": "사이클 롤백 완료!",
        "failed": "실패: ", "req_fail": "요청 실패.",
        "dt_title": "Diff 비교 도구", "dt_select": "파일 선택",
        "dt_hint": "스냅샷이 2개 이상인 파일만 표시됩니다",
        "dt_left": "좌측 (이전)", "dt_right": "우측 (이후)",
        "dt_ordered": "자동 시간순 정렬됨", "dt_no_files": "비교 가능한 파일이 없습니다.",
        "dt_select_both": "양쪽 스냅샷을 선택하세요.",
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
        "snapshot": "snapshot", "cycle_rb": "Cycle Rollback",
        "cycle_desc": "Select a cycle to rollback. All files modified in that cycle will be restored.",
        "cancel": "Cancel", "files_unit": "files",
        "lines_hidden": "lines hidden", "no_diff": "No differences found.",
        "del_title": "Deleted", "del_msg": "This file has been deleted.",
        "del_hint": "Check the history dropdown on the file list for previous snapshots.",
        "no_preview": "Preview not available for this file type.\nUse the download button above.",
        "cfm_clear": "Clear all file history?\\nThis cannot be undone.",
        "cfm_rb": "Rollback to this snapshot?\\nCurrent file will be backed up first.",
        "cfm_cycle": "Rollback ALL files in this cycle?\\nAll current files will be backed up first.",
        "rb_done": "Rollback complete!", "cycle_done": "Cycle rollback complete!",
        "failed": "Failed: ", "req_fail": "Request failed.",
        "dt_title": "Diff Compare Tool", "dt_select": "Select file",
        "dt_hint": "Only files with 2+ snapshots shown",
        "dt_left": "Left (older)", "dt_right": "Right (newer)",
        "dt_ordered": "Auto time-ordered", "dt_no_files": "No files for comparison.",
        "dt_select_both": "Select both snapshots to compare.",
    },
}

_VIEWER_I18N_JSON = json.dumps(_VIEWER_I18N, ensure_ascii=False)


# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------
_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
       background: #0d1117; color: #c9d1d9; line-height: 1.6; }
.container { max-width: 900px; margin: 0 auto; padding: 20px; }
h1 { color: #58a6ff; margin-bottom: 5px; font-size: 1.3em; }
.subtitle { color: #8b949e; font-size: 0.85em; margin-bottom: 20px; }
.separator { border: none; border-top: 1px solid #21262d; margin: 15px 0; }
.footer { color: #484f58; font-size: 0.75em; margin-top: 25px; text-align: center; }

/* Header row */
.header-row { display: flex; align-items: center; justify-content: space-between; }
.header-btns { display: flex; gap: 8px; }
.clear-btn { background: transparent; border: 1px solid #da3633; color: #da3633;
             padding: 5px 12px; border-radius: 6px; font-size: 0.8em; cursor: pointer;
             transition: background 0.2s, color 0.2s; }
.clear-btn:hover { background: #da3633; color: #fff; }
.rollback-btn { background: transparent; border: 1px solid #d29922; color: #d29922;
                padding: 5px 12px; border-radius: 6px; font-size: 0.8em; cursor: pointer;
                transition: background 0.2s, color 0.2s; }
.rollback-btn:hover { background: #d29922; color: #fff; }

/* Collapsible date group (outer) */
details.date-group { margin-bottom: 14px; }
details.date-group > summary { cursor: pointer; color: #58a6ff; font-size: 0.9em;
    padding: 6px 8px; border-radius: 6px; list-style: none; user-select: none; font-weight: 500; }
details.date-group > summary::-webkit-details-marker { display: none; }
details.date-group > summary::before { content: '\\25B6 '; font-size: 0.7em; margin-right: 6px;
    display: inline-block; transition: transform 0.2s; }
details.date-group[open] > summary::before { transform: rotate(90deg); }
details.date-group > summary:hover { background: #161b22; }

/* Collapsible directory group (inner) */
details.dir-group { margin: 4px 0 8px 12px; }
details.dir-group > summary { cursor: pointer; color: #8b949e; font-size: 0.82em;
    padding: 4px 6px; border-radius: 4px; list-style: none; user-select: none; }
details.dir-group > summary::-webkit-details-marker { display: none; }
details.dir-group > summary::before { content: '\\25B6 '; font-size: 0.6em; margin-right: 5px;
    display: inline-block; transition: transform 0.2s; }
details.dir-group[open] > summary::before { transform: rotate(90deg); }
details.dir-group > summary:hover { background: #161b22; }

/* File row */
.file-row { display: flex; align-items: center; padding: 8px 12px;
            border: 1px solid #21262d; border-radius: 6px; margin: 3px 0 0 0;
            background: #161b22; transition: border-color 0.2s; cursor: pointer; }
.file-row:hover { border-color: #58a6ff; }
.file-icon { margin-right: 10px; font-size: 1.1em; flex-shrink: 0; }
.file-name { flex: 1; color: #c9d1d9; font-weight: 500; }
.file-ts { color: #7d8590; font-size: 0.75em; margin-right: 12px; white-space: nowrap; }
.file-size { color: #8b949e; font-size: 0.8em; margin-right: 12px; white-space: nowrap; }
.download-btn { color: #58a6ff; text-decoration: none; font-size: 1.1em; padding: 4px;
                flex-shrink: 0; }
.download-btn:hover { color: #79c0ff; }

/* History dropdown */
.history-dropdown { display: none; margin: 0 0 6px 32px; padding: 6px 0;
                    border: 1px solid #21262d; border-radius: 6px; background: #0d1117; }
.history-dropdown.open { display: block; }
.history-item { display: flex; align-items: center; padding: 5px 14px; gap: 10px; }
.history-item a { color: #58a6ff; text-decoration: none; font-size: 0.82em; }
.history-item a:hover { color: #79c0ff; text-decoration: underline; }
.history-item .snap-badge { color: #3fb950; font-size: 0.7em; }
.history-item .no-snap { color: #484f58; font-size: 0.7em; }
.history-item .op-write { color: #3fb950; font-size: 0.7em; font-weight: 500; }
.history-item .op-edit { color: #d29922; font-size: 0.7em; font-weight: 500; }
.history-item .op-delete { color: #f85149; font-size: 0.7em; font-weight: 500; }
.history-item .op-rollback { color: #a371f7; font-size: 0.7em; font-weight: 500; }
.hist-action { font-size: 0.7em; }
.hist-action a { font-size: 1em; }

/* Deleted file row */
.file-row.deleted { opacity: 0.6; border-color: #da363380; }
.file-row.deleted .file-name { text-decoration: line-through; color: #f85149; }
.file-row.deleted .file-ts { color: #f8514980; }
.history-header { color: #8b949e; font-size: 0.75em; padding: 4px 14px; border-bottom: 1px solid #21262d;
                  margin-bottom: 4px; }

/* View page */
.topbar { display: flex; align-items: center; gap: 15px; margin-bottom: 15px; flex-wrap: wrap; }
.topbar a { color: #58a6ff; text-decoration: none; font-size: 0.9em; }
.topbar .fname { flex: 1; color: #c9d1d9; font-weight: bold; }
pre.code { background: #161b22; border: 1px solid #21262d; border-radius: 6px;
           padding: 16px; overflow-x: auto; font-size: 0.85em; line-height: 1.5;
           white-space: pre; }
.line-num { color: #484f58; display: inline-block; width: 45px; text-align: right;
            margin-right: 16px; user-select: none; }
.img-preview { max-width: 100%; border: 1px solid #21262d; border-radius: 6px;
               margin: 15px 0; }
.no-preview { color: #8b949e; padding: 40px; text-align: center;
              border: 1px dashed #21262d; border-radius: 6px; margin: 15px 0; }
.snap-label { color: #3fb950; font-size: 0.8em; margin-left: 10px; }

/* VS Code-style side-by-side diff */
.diff-page { max-width: 1400px; }
.diff-meta { display: flex; align-items: center; justify-content: space-between;
             padding: 10px 16px; background: #1c2128; border: 1px solid #30363d;
             border-radius: 8px; margin-bottom: 12px; }
.diff-stats { font-size: 0.85em; white-space: nowrap; display: flex; gap: 12px; }
.diff-stats .add-count { color: #3fb950; font-weight: 600; }
.diff-stats .del-count { color: #f85149; font-weight: 600; }
.diff-fheader { display: flex; border: 1px solid #30363d; border-bottom: none;
                border-radius: 8px 8px 0 0; overflow: hidden; }
.diff-fheader div { flex: 1; padding: 10px 16px; font-size: 0.82em; background: #1c2128;
                    color: #8b949e; font-family: 'Consolas','Monaco','Courier New',monospace; }
.diff-fheader div:first-child { border-right: 1px solid #30363d; }
.diff-fheader .fh-old::before { content: '\2212 '; color: #f85149; font-weight: 700; }
.diff-fheader .fh-new::before { content: '+ '; color: #3fb950; font-weight: 700; }
.diff-fheader .fh-old { color: #f0a8a8; }
.diff-fheader .fh-new { color: #a8f0c0; }
.diff-wrap { width: 100%; overflow-x: auto; border: 1px solid #30363d;
             border-top: none; border-radius: 0 0 8px 8px; background: #0d1117; }
.diff-table { width: 100%; border-collapse: collapse; table-layout: fixed;
              font-family: 'Consolas','Monaco','Courier New',monospace;
              font-size: 0.82em; line-height: 1.7; }
.diff-table col.ln { width: 52px; }
.diff-table col.mk { width: 20px; }
.diff-table col.gt { width: 2px; }
.diff-table td { padding: 0; vertical-align: top; white-space: pre;
                 overflow: hidden; text-overflow: ellipsis; }
.diff-table .ln { text-align: right; padding-right: 8px; color: rgba(139,148,158,0.5);
                  user-select: none; font-size: 0.9em; background: rgba(13,17,23,0.6);
                  border-right: 1px solid #21262d; }
.diff-table .mk { text-align: center; color: rgba(139,148,158,0.4);
                   user-select: none; font-size: 0.85em; width: 20px; }
.diff-table .code { padding: 0 16px; color: #c9d1d9; }
.diff-table .gt { background: #30363d; padding: 0; }
/* Delete line (left) */
.diff-table .dl { background: rgba(248,81,73,0.13); }
.diff-table td.dl.ln { background: rgba(248,81,73,0.10); color: rgba(248,81,73,0.7); }
.diff-table td.dl.mk { background: rgba(248,81,73,0.13); color: #f85149; }
.diff-table td.dl.code { color: #e6b0aa; }
/* Add line (right) */
.diff-table .al { background: rgba(63,185,80,0.13); }
.diff-table td.al.ln { background: rgba(63,185,80,0.10); color: rgba(63,185,80,0.7); }
.diff-table td.al.mk { background: rgba(63,185,80,0.13); color: #3fb950; }
.diff-table td.al.code { color: #a8e6b0; }
/* Empty placeholder */
.diff-table td.el { background: #161b22; }
.diff-table td.el.ln { background: rgba(22,27,34,0.8); border-right-color: #1c2128; }
/* Word-level highlights */
.diff-table .wd { background: rgba(248,81,73,0.4); border-radius: 3px; padding: 1px 2px; }
.diff-table .wa { background: rgba(63,185,80,0.4); border-radius: 3px; padding: 1px 2px; }
/* Fold separator */
.diff-table .fold-row td { background: #1c2128; height: 32px; text-align: center;
                           color: #58a6ff; font-size: 0.78em; letter-spacing: 0.5px;
                           border-top: 1px solid #21262d; border-bottom: 1px solid #21262d; }
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
.rb-confirm { background: #161b22; border: 1px solid #21262d; border-radius: 6px;
              padding: 20px; margin: 15px 0; }
.rb-confirm h3 { color: #d29922; margin-bottom: 12px; font-size: 1em; }
.rb-info { color: #8b949e; font-size: 0.85em; margin-bottom: 6px; }
.rb-btns { display: flex; gap: 10px; margin-top: 16px; }
.rb-btns .btn-rollback { background: #d29922; color: #fff; border: none; padding: 8px 20px;
                         border-radius: 6px; cursor: pointer; font-size: 0.85em; }
.rb-btns .btn-rollback:hover { background: #e3b341; }
.rb-btns .btn-cancel { background: transparent; border: 1px solid #30363d; color: #8b949e;
                       padding: 8px 20px; border-radius: 6px; cursor: pointer; font-size: 0.85em; }
.rb-btns .btn-cancel:hover { background: #21262d; }

/* Toolbar: search + sort + lang + diff button */
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
.toolbar input[type="text"] { flex: 1; min-width: 120px; background: #161b22; border: 1px solid #30363d;
    color: #c9d1d9; padding: 6px 12px; border-radius: 6px; font-size: 0.85em; outline: none; }
.toolbar input[type="text"]:focus { border-color: #58a6ff; }
.toolbar input[type="text"]::placeholder { color: #484f58; }
.toolbar select { background: #161b22; border: 1px solid #30363d; color: #8b949e; padding: 6px 8px;
    border-radius: 6px; font-size: 0.8em; cursor: pointer; outline: none; }
.toolbar select:focus { border-color: #58a6ff; }
.diff-tool-btn { background: transparent; border: 1px solid #58a6ff; color: #58a6ff;
    padding: 5px 12px; border-radius: 6px; font-size: 0.8em; cursor: pointer;
    transition: background 0.2s, color 0.2s; text-decoration: none; white-space: nowrap; }
.diff-tool-btn:hover { background: #58a6ff; color: #fff; }

/* Code view: highlight.js + line numbers */
.code-wrap { position: relative; border: 1px solid #21262d; border-radius: 6px;
             overflow: hidden; margin: 15px 0; }
.code-wrap .copy-btn { position: absolute; top: 8px; right: 8px; background: #21262d;
    border: 1px solid #30363d; color: #8b949e; padding: 4px 10px; border-radius: 4px;
    font-size: 0.75em; cursor: pointer; z-index: 2; transition: background 0.2s, color 0.2s; }
.code-wrap .copy-btn:hover { background: #30363d; color: #c9d1d9; }
.code-wrap .copy-btn.ok { background: #238636; color: #fff; border-color: #238636; }
.code-container { display: flex; overflow-x: auto; }
.code-container .line-nums { padding: 16px 0; background: #0d1117; border-right: 1px solid #21262d;
    user-select: none; flex-shrink: 0; text-align: right; }
.code-container .line-nums span { display: block; padding: 0 12px 0 16px; color: #484f58;
    font-family: 'Consolas','Monaco','Courier New',monospace; font-size: 0.82em; line-height: 1.55; }
.code-container pre { flex: 1; margin: 0; padding: 16px; background: #161b22; overflow-x: visible; }
.code-container pre code { font-size: 0.85em; line-height: 1.55; background: transparent !important;
    padding: 0 !important; }

/* Diff tool page */
.dt-panel { background: #161b22; border: 1px solid #21262d; border-radius: 8px;
            padding: 20px; margin-bottom: 16px; }
.dt-panel label { color: #8b949e; font-size: 0.85em; display: block; margin-bottom: 6px; }
.dt-panel select { width: 100%; background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
    padding: 8px 12px; border-radius: 6px; font-size: 0.85em; outline: none; cursor: pointer; }
.dt-panel select:focus { border-color: #58a6ff; }
.dt-panel select:disabled { opacity: 0.4; cursor: not-allowed; }
.dt-snap-row { display: flex; gap: 16px; margin-top: 12px; }
.dt-snap-row > div { flex: 1; }
.dt-info { color: #3fb950; font-size: 0.78em; margin-top: 6px; text-align: center; }
.dt-hint { color: #484f58; font-size: 0.82em; }
.dt-result { margin-top: 16px; }

/* Language selector */
.lang-sel { background: #161b22; border: 1px solid #30363d; color: #8b949e; padding: 4px 6px;
    border-radius: 4px; font-size: 0.75em; cursor: pointer; outline: none; }
.lang-sel:focus { border-color: #58a6ff; }

/* Cycle rollback modal */
.modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                 background: rgba(0,0,0,0.7); z-index: 100; justify-content: center; align-items: center; }
.modal-overlay.open { display: flex; }
.modal { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
         max-width: 600px; width: 90%; max-height: 80vh; overflow-y: auto; padding: 24px; }
.modal h2 { color: #d29922; font-size: 1.1em; margin-bottom: 16px; }
.cycle-item { display: flex; align-items: center; padding: 10px 14px; margin: 4px 0;
              border: 1px solid #21262d; border-radius: 6px; cursor: pointer;
              transition: border-color 0.2s, background 0.2s; }
.cycle-item:hover { border-color: #d29922; background: #1c1a15; }
.cycle-info { flex: 1; }
.cycle-id { color: #d29922; font-weight: 500; font-size: 0.85em; }
.cycle-label { color: #c9d1d9; font-size: 0.82em; margin-top: 3px;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 500px; }
.cycle-ts { color: #8b949e; font-size: 0.78em; margin-top: 2px; }
.cycle-files { color: #8b949e; font-size: 0.75em; margin-top: 2px; }
.modal-close { background: transparent; border: 1px solid #30363d; color: #8b949e;
               padding: 6px 16px; border-radius: 6px; cursor: pointer; margin-top: 12px; }
.modal-close:hover { background: #21262d; }
"""

_JS = ("""
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js"></script>
<script>
var VI18N=""" + _VIEWER_I18N_JSON + """;
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
}
function switchLang(lang){
  _lang=lang;localStorage.setItem('fv_lang',lang);applyI18n();
}
// Existing functions
function toggleHistory(btn, id) {
  var el = document.getElementById(id);
  if (!el) return;
  btn.parentElement.classList.toggle('expanded');
  el.classList.toggle('open');
}
function clearHistory(url) {
  if (!confirm(T('cfm_clear'))) return;
  fetch(url, {method:'POST'}).then(function(r){
    if (r.ok) location.reload();
    else alert(T('req_fail'));
  }).catch(function(){alert(T('req_fail'));});
}
function doRollbackFile(url) {
  if (!confirm(T('cfm_rb'))) return;
  fetch(url, {method:'POST'}).then(function(r){
    if (r.ok) { alert(T('rb_done')); location.reload(); }
    else r.text().then(function(t){alert(T('failed') + t);});
  }).catch(function(){alert(T('req_fail'));});
}
function showCycleModal() {
  document.getElementById('cycle-modal').classList.add('open');
}
function closeCycleModal() {
  document.getElementById('cycle-modal').classList.remove('open');
}
function doRollbackCycle(url) {
  if (!confirm(T('cfm_cycle'))) return;
  fetch(url, {method:'POST'}).then(function(r){
    if (r.ok) { alert(T('cycle_done')); location.reload(); }
    else r.text().then(function(t){alert(T('failed') + t);});
  }).catch(function(){alert(T('req_fail'));});
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
  var container=document.getElementById('file-list-container');
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
document.addEventListener('DOMContentLoaded',function(){initHL();applyI18n();});
</script>
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
        if rid > 0:
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
                            f' title="Download last snapshot" onclick="event.stopPropagation()">\u2b07</a>')
                    else:
                        dl_btn = ""
                else:
                    dl_btn = (
                        f'<a class="download-btn" href="/download/{session_token}/{idx}"'
                        f' title="Download" onclick="event.stopPropagation()">\u2b07</a>')
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
    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(fname)}</title><style>{_CSS}</style></head>
<body><div class="container">
<div class="topbar">{back_link}<span class="fname">{html.escape(fname)}{suffix_html}</span>{lang_sel}{download_link}</div>
<hr class="separator">
{content_html}
</div>{_JS}</body></html>"""


def _page_deleted(fpath, idx, session_token):
    """Generate a page for deleted files."""
    fname = os.path.basename(fpath)
    back_link = f'<a href="/list/{session_token}">\u2190 List</a>'
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(fname)} (deleted)</title><style>{_CSS}</style></head>
<body><div class="container">
<div class="topbar">{back_link}<span class="fname" style="color:#f85149;text-decoration:line-through">{html.escape(fname)}</span>
<span style="color:#f85149;font-size:0.85em">\U0001f5d1 Deleted</span></div>
<hr class="separator">
<div class="no-preview" style="border-color:#f8514940">
  <p style="font-size:1.2em;margin-bottom:10px">\U0001f5d1</p>
  <p>This file has been deleted.</p>
  <p style="color:#484f58;font-size:0.85em;margin-top:8px">{html.escape(fpath)}</p>
  <p style="color:#484f58;font-size:0.85em;margin-top:4px">Check the history dropdown on the file list for previous snapshots.</p>
</div>
</div></body></html>"""


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


def _page_diff(old_name, old_text, new_name, new_text, session_token):
    """Generate a VS Code-style side-by-side diff page."""
    back_link = f'<a href="/list/{session_token}">\u2190 List</a>'
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    table_rows = []
    add_count = 0
    del_count = 0
    CONTEXT = 3

    def _ctx_row(i, j, text):
        esc = html.escape(text)
        return (f'<tr><td class="ln">{i}</td><td class="mk"></td><td class="code">{esc}</td>'
                f'<td class="gt"></td>'
                f'<td class="ln">{j}</td><td class="mk"></td><td class="code">{esc}</td></tr>')

    def _del_row(i, text):
        return (f'<tr><td class="ln dl">{i}</td><td class="mk dl">\u2212</td><td class="code dl">{text}</td>'
                f'<td class="gt"></td>'
                f'<td class="ln el"></td><td class="mk el"></td><td class="code el"></td></tr>')

    def _add_row(j, text):
        return (f'<tr><td class="ln el"></td><td class="mk el"></td><td class="code el"></td>'
                f'<td class="gt"></td>'
                f'<td class="ln al">{j}</td><td class="mk al">+</td><td class="code al">{text}</td></tr>')

    def _replace_row(i, old_html, j, new_html):
        return (f'<tr><td class="ln dl">{i}</td><td class="mk dl">\u2212</td><td class="code dl">{old_html}</td>'
                f'<td class="gt"></td>'
                f'<td class="ln al">{j}</td><td class="mk al">+</td><td class="code al">{new_html}</td></tr>')

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            n = i2 - i1
            if n > CONTEXT * 2 + 1:
                for k in range(CONTEXT):
                    table_rows.append(_ctx_row(i1+k+1, j1+k+1, old_lines[i1+k]))
                folded = n - CONTEXT * 2
                table_rows.append(
                    f'<tr class="fold-row"><td colspan="3"></td><td class="gt"></td>'
                    f'<td colspan="3"><span class="fold-icon">\u2195</span>{folded} lines hidden</td></tr>')
                for k in range(CONTEXT):
                    ii, jj = i2 - CONTEXT + k, j2 - CONTEXT + k
                    table_rows.append(_ctx_row(ii+1, jj+1, old_lines[ii]))
            else:
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    table_rows.append(_ctx_row(i+1, j+1, old_lines[i]))

        elif tag == "replace":
            old_n = i2 - i1
            new_n = j2 - j1
            del_count += old_n
            add_count += new_n
            # If sizes differ a lot, render as separate delete + insert blocks
            # for clearer visual alignment (avoids confusing N:1 pairing)
            if max(old_n, new_n) > 2 * min(old_n, new_n):
                for i in range(i1, i2):
                    table_rows.append(_del_row(i+1, html.escape(old_lines[i])))
                for j in range(j1, j2):
                    table_rows.append(_add_row(j+1, html.escape(new_lines[j])))
            else:
                # Paired with word-level highlighting
                max_len = max(old_n, new_n)
                for k in range(max_len):
                    has_old = k < old_n
                    has_new = k < new_n
                    if has_old and has_new:
                        oh, nh = _word_highlight(old_lines[i1+k], new_lines[j1+k])
                        table_rows.append(_replace_row(i1+k+1, oh, j1+k+1, nh))
                    elif has_old:
                        table_rows.append(_del_row(i1+k+1, html.escape(old_lines[i1+k])))
                    else:
                        table_rows.append(_add_row(j1+k+1, html.escape(new_lines[j1+k])))

        elif tag == "delete":
            del_count += i2 - i1
            for i in range(i1, i2):
                table_rows.append(_del_row(i+1, html.escape(old_lines[i])))

        elif tag == "insert":
            add_count += j2 - j1
            for j in range(j1, j2):
                table_rows.append(_add_row(j+1, html.escape(new_lines[j])))

    if not table_rows:
        diff_html = '<div class="no-preview">No differences found.</div>'
    else:
        diff_html = (
            f'<div class="diff-fheader">'
            f'<div><span class="fh-old">{html.escape(old_name)}</span></div>'
            f'<div><span class="fh-new">{html.escape(new_name)}</span></div></div>'
            f'<div class="diff-wrap"><table class="diff-table">'
            f'<colgroup><col class="ln"><col class="mk"><col>'
            f'<col class="gt">'
            f'<col class="ln"><col class="mk"><col></colgroup>'
            f'{"".join(table_rows)}</table></div>')

    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diff</title><style>{_CSS}</style></head>
<body><div class="container diff-page">
<div class="topbar">{back_link}<span class="fname">Diff</span>{lang_sel}</div>
<div class="diff-meta">
  <div style="color:#8b949e;font-size:0.85em">{html.escape(old_name)} \u2192 {html.escape(new_name)}</div>
  <div class="diff-stats"><span class="add-count">+{add_count}</span><span class="del-count">\u2212{del_count}</span></div>
</div>
<hr class="separator">
{diff_html}
</div>{_JS}</body></html>"""


def _diff_fragment(old_name, old_text, new_name, new_text):
    """Generate just the diff HTML fragment (no full page wrapper). For AJAX loading."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    table_rows = []
    add_count = 0
    del_count = 0
    CONTEXT = 3

    def _ctx_row(i, j, text):
        esc = html.escape(text)
        return (f'<tr><td class="ln">{i}</td><td class="mk"></td><td class="code">{esc}</td>'
                f'<td class="gt"></td>'
                f'<td class="ln">{j}</td><td class="mk"></td><td class="code">{esc}</td></tr>')

    def _del_row(i, text):
        return (f'<tr><td class="ln dl">{i}</td><td class="mk dl">\u2212</td><td class="code dl">{text}</td>'
                f'<td class="gt"></td>'
                f'<td class="ln el"></td><td class="mk el"></td><td class="code el"></td></tr>')

    def _add_row(j, text):
        return (f'<tr><td class="ln el"></td><td class="mk el"></td><td class="code el"></td>'
                f'<td class="gt"></td>'
                f'<td class="ln al">{j}</td><td class="mk al">+</td><td class="code al">{text}</td></tr>')

    def _replace_row(i, old_html, j, new_html):
        return (f'<tr><td class="ln dl">{i}</td><td class="mk dl">\u2212</td><td class="code dl">{old_html}</td>'
                f'<td class="gt"></td>'
                f'<td class="ln al">{j}</td><td class="mk al">+</td><td class="code al">{new_html}</td></tr>')

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            n = i2 - i1
            if n > CONTEXT * 2 + 1:
                for k in range(CONTEXT):
                    table_rows.append(_ctx_row(i1+k+1, j1+k+1, old_lines[i1+k]))
                folded = n - CONTEXT * 2
                table_rows.append(
                    f'<tr class="fold-row"><td colspan="3"></td><td class="gt"></td>'
                    f'<td colspan="3"><span class="fold-icon">\u2195</span>{folded} lines hidden</td></tr>')
                for k in range(CONTEXT):
                    ii, jj = i2 - CONTEXT + k, j2 - CONTEXT + k
                    table_rows.append(_ctx_row(ii+1, jj+1, old_lines[ii]))
            else:
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    table_rows.append(_ctx_row(i+1, j+1, old_lines[i]))
        elif tag == "replace":
            old_n = i2 - i1
            new_n = j2 - j1
            del_count += old_n
            add_count += new_n
            if max(old_n, new_n) > 2 * min(old_n, new_n):
                for i in range(i1, i2):
                    table_rows.append(_del_row(i+1, html.escape(old_lines[i])))
                for j in range(j1, j2):
                    table_rows.append(_add_row(j+1, html.escape(new_lines[j])))
            else:
                max_len = max(old_n, new_n)
                for k in range(max_len):
                    has_old = k < old_n
                    has_new = k < new_n
                    if has_old and has_new:
                        oh, nh = _word_highlight(old_lines[i1+k], new_lines[j1+k])
                        table_rows.append(_replace_row(i1+k+1, oh, j1+k+1, nh))
                    elif has_old:
                        table_rows.append(_del_row(i1+k+1, html.escape(old_lines[i1+k])))
                    else:
                        table_rows.append(_add_row(j1+k+1, html.escape(new_lines[j1+k])))
        elif tag == "delete":
            del_count += i2 - i1
            for i in range(i1, i2):
                table_rows.append(_del_row(i+1, html.escape(old_lines[i])))
        elif tag == "insert":
            add_count += j2 - j1
            for j in range(j1, j2):
                table_rows.append(_add_row(j+1, html.escape(new_lines[j])))

    if not table_rows:
        return '<div class="no-preview">No differences found.</div>'

    return (
        f'<div class="diff-fheader">'
        f'<div><span class="fh-old">{html.escape(old_name)}</span></div>'
        f'<div><span class="fh-new">{html.escape(new_name)}</span></div></div>'
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
<div class="topbar">{back_link}<span class="fname" data-i18n="dt_title">Diff \ube44\uad50 \ub3c4\uad6c</span>{lang_sel}</div>
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
    lang_sel = ('<select id="lang-sel" class="lang-sel" onchange="switchLang(this.value)">'
                '<option value="ko">\ud55c\uad6d\uc5b4</option><option value="en">English</option></select>')
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Snapshot: {html.escape(fname)}</title><style>{_CSS}</style></head>
<body><div class="container">
<div class="topbar">{back_link}<span class="fname">{html.escape(fname)}{snap_badge}{rb_btn}</span>{lang_sel}</div>
<hr class="separator">
{content_html}
</div>{_JS}</body></html>"""


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
    """Rollback all files from a cycle ONWARDS to their pre-cycle state.
    e.g. rolling back #9 also undoes #10, #11, etc.
    Returns (success: bool, message: str, count: int)."""
    from state import add_modified_file

    # Find entries belonging to the target cycle
    target_entries = [e for e in entries if e.get("run_id") == run_id]
    if not target_entries:
        return False, "Cycle not found", 0

    # Global cutoff: earliest timestamp in the target cycle
    global_cutoff_ts = min(e["ts"] for e in target_entries)

    # Collect ALL entries from target cycle onwards (includes later cycles)
    affected_entries = [e for e in entries if e.get("run_id", 0) >= run_id]
    # Get unique paths across all affected cycles
    affected_paths = list(dict.fromkeys(e["path"] for e in affected_entries))

    restored = 0
    for path in affected_paths:
        all_entries_for_path = [e for e in entries if e["path"] == path]
        all_entries_for_path.sort(key=lambda e: e["ts"])

        # Find the most recent snapshot BEFORE the target cycle's start
        prev_snapshot = None
        for e in reversed(all_entries_for_path):
            if e["ts"] < global_cutoff_ts and e.get("snapshot"):
                prev_snapshot = e["snapshot"]
                break

        # Backup current file
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    cur = f.read()
                add_modified_file(path, content=cur.decode("utf-8", errors="replace"),
                                  op="rollback-backup")
            except Exception:
                pass

        if prev_snapshot:
            # Restore to previous snapshot
            snap_path = os.path.join(_SNAPSHOTS_DIR, prev_snapshot)
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
            # No prior snapshot found for this path.
            # Only delete if the first real op (not rollback) in the affected range
            # is "write" (new file). If "edit", the file pre-existed — leave it.
            real_ops = [e for e in all_entries_for_path
                        if e.get("run_id", 0) >= run_id
                        and e.get("op") not in ("rollback-backup", "rollback")]
            real_ops.sort(key=lambda e: e["ts"])
            first_op = real_ops[0].get("op", "edit") if real_ops else "edit"
            if first_op == "write" and os.path.isfile(path):
                try:
                    os.remove(path)
                    add_modified_file(path, content=None, op="delete")
                    restored += 1
                except Exception as ex:
                    log.warning("Failed to delete %s: %s", path, ex)
            else:
                log.info("Skipped rollback for %s: no prior snapshot, file pre-existed cycle", path)

    log.info("Cycle #%d+ rollback: %d/%d files restored", run_id, restored, len(affected_paths))
    return True, "OK", restored


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class _ViewerHandler(BaseHTTPRequestHandler):
    """Read-only file viewer HTTP handler."""

    modified_entries = []     # list of entry dicts
    session_tokens = {}       # token -> True

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
        if st not in _ViewerHandler.session_tokens:
            return None
        return st

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
        session_token = self._check_session(path_parts)
        if not session_token:
            self._send_error(403, "Session expired")
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
            body = _page_diff(snap_old, old_text, snap_new, new_text, session_token)
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
            if ok:
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
