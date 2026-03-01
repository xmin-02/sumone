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


def _default_provider_stats():
    return {
        "claude": {"cost": 0.0, "tokens_in": 0, "tokens_out": 0},
        "codex": {"cost": 0.0, "tokens_in": 0, "tokens_out": 0},
        "gemini": {"cost": 0.0, "tokens_in": 0, "tokens_out": 0},
    }


def _load_provider_stats():
    stats = _default_provider_stats()
    raw = _config.get("provider_stats")
    if not isinstance(raw, dict):
        return stats
    for provider, default in stats.items():
        row = raw.get(provider)
        if not isinstance(row, dict):
            continue
        try:
            default["cost"] = float(row.get("cost", 0.0) or 0.0)
            default["tokens_in"] = int(row.get("tokens_in", 0) or 0)
            default["tokens_out"] = int(row.get("tokens_out", 0) or 0)
        except (TypeError, ValueError):
            continue
    return stats


def _load_float(name, default=0.0):
    try:
        return float(_config.get(name, default) or 0.0)
    except (TypeError, ValueError):
        return default


def _load_int(name, default=0):
    try:
        return int(_config.get(name, default) or 0)
    except (TypeError, ValueError):
        return default


_current_run_id = 0  # incremented each AI run() call
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
    ai_proc = None
    provider = "claude"
    _provider_sessions = _config.get("provider_sessions", {})   # {provider: session_id}
    _provider_models = _config.get("provider_models", {})     # {provider: model}
    _provider_auth = _config.get("provider_auth", {})         # {provider: {auth config}}
    _run_gen = 0              # generation counter — bumped by /cancel to detect stale runs
    cli_status = {}           # {provider: bool} — True if CLI installed & runnable
    provider_stats = _load_provider_stats()
    busy = False
    model = None
    total_cost = _load_float("total_cost", 0.0)
    last_cost = _load_float("last_cost", 0.0)
    global_tokens = _load_int("monthly_tokens", 0)
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


def switch_provider(new_provider):
    """Save current provider's session/model and switch to new_provider, restoring its state."""
    if state.provider == new_provider:
        return
    with state.lock:
        # Save current provider's session and model
        if state.session_id:
            state._provider_sessions[state.provider] = state.session_id
        if state.model:
            state._provider_models[state.provider] = state.model
        # Switch provider and restore target's state
        state.provider = new_provider
        state.session_id = state._provider_sessions.get(new_provider)
        state.model = state._provider_models.get(new_provider)
    from config import update_config
    update_config("session_id", state.session_id)
    update_config("provider", new_provider)
    update_config("provider_sessions", dict(state._provider_sessions))
    update_config("provider_models", dict(state._provider_models))


def get_provider_auth(provider):
    """Return provider auth config as a dict."""
    auth = state._provider_auth.get(provider)
    return auth if isinstance(auth, dict) else {}


def set_provider_auth(provider, auth):
    """Persist auth config for a provider."""
    if auth:
        state._provider_auth[provider] = dict(auth)
    else:
        state._provider_auth.pop(provider, None)
    from config import update_config
    update_config("provider_auth", dict(state._provider_auth))


def get_provider_env(provider):
    """Build environment variables for provider-specific auth."""
    auth = get_provider_auth(provider)
    env = {}
    if provider == "claude":
        token = auth.get("oauth_token")
        refresh = auth.get("oauth_refresh_token")
        api_key = auth.get("api_key")
        auth_token = auth.get("auth_token")
        account_uuid = auth.get("account_uuid")
        user_email = auth.get("user_email")
        organization_uuid = auth.get("organization_uuid")
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        if refresh:
            env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = refresh
        if account_uuid:
            env["CLAUDE_CODE_ACCOUNT_UUID"] = account_uuid
        if user_email:
            env["CLAUDE_CODE_USER_EMAIL"] = user_email
        if organization_uuid:
            env["CLAUDE_CODE_ORGANIZATION_UUID"] = organization_uuid
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        if auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    return env
