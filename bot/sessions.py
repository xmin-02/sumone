"""Session management."""
import json
import os
import re
import time

from config import IS_WINDOWS, log
from state import state


def _discover_claude_roots():
    """Auto-discover all .claude/projects directories on the system."""
    roots = []
    # Current user
    own = os.path.expanduser("~/.claude/projects")
    if os.path.isdir(own):
        roots.append(own)
    # Other users: /home/* and /root
    candidates = []
    try:
        candidates = [os.path.join("/home", d) for d in os.listdir("/home")]
    except Exception:
        pass
    candidates.append("/root")
    for home in candidates:
        cp = os.path.join(home, ".claude", "projects")
        if cp == own or not os.path.isdir(cp):
            continue
        try:
            os.listdir(cp)
            roots.append(cp)
        except PermissionError:
            continue
    return roots


def find_project_dirs():
    if IS_WINDOWS:
        claude_proj = os.path.join(os.environ.get("APPDATA", ""), "claude", "projects")
        if not os.path.isdir(claude_proj):
            claude_proj = os.path.expanduser("~/.claude/projects")
        if not os.path.isdir(claude_proj): return []
        dirs = []
        for name in os.listdir(claude_proj):
            full = os.path.join(claude_proj, name)
            if os.path.isdir(full): dirs.append(full)
        return dirs
    dirs = []
    seen = set()
    for root in _discover_claude_roots():
        try:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if os.path.isdir(full) and full not in seen:
                    seen.add(full)
                    dirs.append(full)
        except Exception:
            continue
    return dirs


def get_sessions(limit=10):
    import glob as g
    all_files = []
    for proj_dir in find_project_dirs():
        all_files.extend(g.glob(os.path.join(proj_dir, "*.jsonl")))
    files = sorted(all_files, key=os.path.getmtime, reverse=True)[:limit]
    sessions = []
    for fpath in files:
        sid = os.path.basename(fpath).replace(".jsonl", "")
        mtime = os.path.getmtime(fpath)
        ts = time.strftime("%m/%d %H:%M", time.localtime(mtime))
        preview = _get_first_user_message(fpath)
        sessions.append((sid, ts, preview))
    return sessions


def _get_first_user_message(fpath):
    from i18n import t
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: e = json.loads(line)
                except Exception: continue
                if e.get("type") != "user": continue
                content = e.get("message", {}).get("content", "")
                text = _extract_text(content)
                if text:
                    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
                    if text: return text[:80]
    except Exception: pass
    return t("session.no_preview")


def _extract_text(content):
    if isinstance(content, str) and content.strip(): return content.strip()
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t_val = c.get("text", "").strip()
                if t_val: return t_val
    return ""


def get_session_model(session_id):
    for proj_dir in find_project_dirs():
        fpath = os.path.join(proj_dir, f"{session_id}.jsonl")
        if not os.path.exists(fpath): continue
        try:
            with open(fpath, "rb") as f:
                f.seek(0, 2); size = f.tell()
                f.seek(max(0, size - 50000))
                tail = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail.strip().split("\n")):
                line = line.strip()
                if not line: continue
                try: e = json.loads(line)
                except Exception: continue
                if e.get("type") == "assistant":
                    m = e.get("message", {}).get("model", "")
                    if m: return m
        except Exception: pass
    return None
