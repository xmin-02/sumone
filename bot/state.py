"""Global bot state."""
import collections
import hashlib
import json
import os
import threading
import time
from datetime import datetime
from config import _config, SCRIPT_DIR, log

_MODIFIED_FILES_PATH = os.path.join(SCRIPT_DIR, "modified_files.json")
_SNAPSHOTS_DIR = os.path.join(SCRIPT_DIR, ".snapshots")


def _load_modified_files():
    """Load persisted modified files list from disk."""
    try:
        with open(_MODIFIED_FILES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # Migration: old format was a plain list of path strings
            if data and isinstance(data[0], str):
                entries = []
                for path in data:
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        mtime = time.time()
                    entries.append({
                        "path": path,
                        "ts": datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S"),
                        "snapshot": None,
                    })
                return entries
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def save_modified_files(entries):
    """Save modified files list to disk."""
    try:
        with open(_MODIFIED_FILES_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Failed to save modified_files: %s", e)


_current_run_id = 0  # incremented each run_claude() call
_current_run_label = ""  # user message for current run


def next_run_id(label=""):
    """Increment and return the next run_id. Label is the user message summary."""
    global _current_run_id, _current_run_label
    # Resume from the highest run_id in existing data
    if _current_run_id == 0 and state.modified_files:
        _current_run_id = max(e.get("run_id", 0) for e in state.modified_files)
    _current_run_id += 1
    _current_run_label = (label or "")[:100]
    return _current_run_id


def get_current_run_id():
    return _current_run_id


_last_cleanup_ts = 0.0


def _get_snapshot_ttl_days():
    """Read snapshot TTL from settings, fallback to 7."""
    from config import settings as _settings
    try:
        return int(_settings.get("snapshot_ttl_days", 7))
    except (ValueError, TypeError):
        return 7


def cleanup_old_snapshots():
    """Delete snapshot files older than configured TTL. Keeps history entries but clears snapshot ref."""
    global _last_cleanup_ts
    now = time.time()
    # Run at most once per hour
    if now - _last_cleanup_ts < 3600:
        return
    _last_cleanup_ts = now
    ttl_days = _get_snapshot_ttl_days()
    cutoff = datetime.now().timestamp() - ttl_days * 86400
    changed = False
    for entry in state.modified_files:
        snap = entry.get("snapshot")
        if not snap:
            continue
        snap_path = os.path.join(_SNAPSHOTS_DIR, snap)
        if not os.path.isfile(snap_path):
            continue
        try:
            mtime = os.path.getmtime(snap_path)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                os.remove(snap_path)
                log.info("Cleaned up old snapshot: %s", snap)
            except OSError as e:
                log.warning("Failed to remove old snapshot %s: %s", snap, e)
                continue
            entry["snapshot"] = None
            changed = True
    if changed:
        save_modified_files(state.modified_files)
        log.info("Snapshot cleanup complete (TTL=%d days)", ttl_days)


def add_modified_file(path, content=None, op="write"):
    """Add a file modification entry with optional snapshot. op: 'write', 'edit', 'delete', 'rollback'."""
    cleanup_old_snapshots()
    os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)
    now = datetime.now()
    ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    snapshot_name = None
    if content is not None:
        ext = os.path.splitext(path)[1] or ".txt"
        cbytes = content.encode("utf-8", errors="replace") if isinstance(content, str) else content
        hash8 = hashlib.md5(cbytes).hexdigest()[:8]
        snapshot_name = f"{now.strftime('%Y%m%d_%H%M%S')}_{hash8}{ext}"
        snapshot_full = os.path.join(_SNAPSHOTS_DIR, snapshot_name)
        try:
            if isinstance(content, str):
                with open(snapshot_full, "w", encoding="utf-8") as f:
                    f.write(content)
            else:
                with open(snapshot_full, "wb") as f:
                    f.write(content)
        except Exception as e:
            log.warning("Failed to save snapshot %s: %s", snapshot_name, e)
            snapshot_name = None
    entry = {"path": path, "ts": ts, "snapshot": snapshot_name, "op": op,
             "run_id": _current_run_id, "run_label": _current_run_label}
    state.modified_files.append(entry)
    save_modified_files(state.modified_files)
    return entry


def find_path_for_snapshot(snapshot_name):
    """Find the original file path for a given snapshot name."""
    for entry in state.modified_files:
        if entry.get("snapshot") == snapshot_name:
            return entry.get("path")
    return None


def clear_modified_files():
    """Clear all modified files and snapshots."""
    import shutil
    state.modified_files.clear()
    save_modified_files(state.modified_files)
    if os.path.isdir(_SNAPSHOTS_DIR):
        shutil.rmtree(_SNAPSHOTS_DIR, ignore_errors=True)


class State:
    session_id = _config.get("session_id")
    selecting = False
    answering = False
    session_list = []
    pending_question = None
    claude_proc = None
    busy = False
    model = None
    total_cost = 0.0
    last_cost = 0.0
    global_tokens = 0
    waiting_token_input = False
    message_queue = collections.deque()
    lock = threading.Lock()
    # File viewer
    modified_files = _load_modified_files()
    file_viewer_url = None       # cloudflared tunnel public URL
    _file_server = None          # FileViewerServer instance
    _tunnel_proc = None          # cloudflared subprocess
    _viewer_msg_ids = []         # sent viewer link message IDs (for deletion)

state = State()
