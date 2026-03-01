"""AI runner abstraction layer.

Provides BaseRunner (common JSONL streaming loop), ParsedEvent, RunnerCallbacks,
and get_runner() factory for multi-provider AI support.
"""
import json
import os
import subprocess
import threading
import time

import i18n
import config as _cfg
from config import IS_WINDOWS, settings, log
from state import state

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ParsedEvent:
    """Normalized event from any AI provider's JSONL stream."""
    kind: str = "ignore"          # "text" | "tool_use" | "result" | "session" | "ignore"
    text: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    file_paths: list = field(default_factory=list)
    file_op: str = ""             # "write" | "edit" | "delete" | ""
    file_content: str = ""
    is_edit_deferred: bool = False
    questions: list = None
    session_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    is_error: bool = False
    errors: list = field(default_factory=list)


@dataclass
class RunnerCallbacks:
    """Messenger abstraction -- Runner never imports messenger directly."""
    on_text: object = None        # (str) -> None
    on_status: object = None      # (label: str, elapsed_secs: int) -> None
    on_typing: object = None      # () -> None
    on_cost: object = None        # (ParsedEvent) -> None
    on_file_link: object = None   # (had_new_files: bool) -> None


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def format_time(mins, secs):
    """Format elapsed time using i18n."""
    if mins > 0:
        return i18n.t("time.format_ms", mins=mins, secs=secs)
    return i18n.t("time.format_s", secs=secs)


# ---------------------------------------------------------------------------
# Base Runner
# ---------------------------------------------------------------------------

class BaseRunner:
    """Common interface for all AI runners."""

    PROVIDER = "base"
    RESUME_MODE = "none"          # "session_id" | "last_only" | "none"
    _cli_cmd_cache = {}

    def __init__(self, callbacks=None):
        self.cb = callbacks or RunnerCallbacks()
        self._proc = None
        self._pending_edit_snapshots = []
        self._final_text = []
        self._sent_text_count = 0
        self._start_time = 0
        self._captured_session_id = None
        self._pending_questions = None
        self._result_event = None
        self._last_status_time = 0

    def run(self, message, session_id=None):
        """Execute AI CLI and return (output, session_id, questions)."""
        from state import next_run_id

        message = self._maybe_inject_context(message, session_id)
        cmd = self._build_cmd(message, session_id)
        env = self._build_env()

        log.info("Running [%s]: %s", self.PROVIDER, " ".join(cmd[:6]) + "...")
        run_id = next_run_id(label=message)
        log.info("Run cycle #%d: %s", run_id, message[:50])

        files_count_before = len(state.modified_files)

        # Reset per-run state
        self._final_text = []
        self._sent_text_count = 0
        self._captured_session_id = None
        self._pending_questions = None
        self._pending_edit_snapshots = []
        self._result_event = None
        self._last_status_time = 0

        try:
            popen_kwargs = dict(
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=_cfg.WORK_DIR, env=env,
            )
            if IS_WINDOWS:
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            with state.lock:
                proc = subprocess.Popen(cmd, **popen_kwargs)
                self._proc = proc
                state.ai_proc = proc

            self._start_time = time.time()

            # Typing indicator thread (sends Telegram "typing..." action)
            if self.cb.on_typing:
                def _typing_loop():
                    while proc.poll() is None:
                        self.cb.on_typing()
                        time.sleep(5)
                threading.Thread(target=_typing_loop, daemon=True).start()

            for raw_line in proc.stdout:
                # Process deferred Edit snapshots from previous iteration
                self._flush_deferred_edits()

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                parsed_list = self._parse_event(event)
                should_break = False
                for parsed in parsed_list:
                    self._handle_parsed(parsed)

                    if not self._captured_session_id and parsed.session_id:
                        self._captured_session_id = parsed.session_id
                        log.info("Captured session_id: %s", parsed.session_id)

                    if parsed.questions:
                        self._pending_questions = parsed.questions
                        log.info("Questions detected: %d, killing proc",
                                 len(parsed.questions))
                        proc.kill()
                        should_break = True
                        break

                    # Status display (throttled to 5s)
                    now = time.time()
                    if (settings["show_status"] and parsed.kind == "tool_use"
                            and now - self._last_status_time >= 5):
                        label = self._make_status_description(parsed)
                        if label and self.cb.on_status:
                            elapsed = int(now - self._start_time)
                            self.cb.on_status(label, elapsed)
                            self._last_status_time = now
                            log.info("Status: %s", label)

                if should_break:
                    break

            # Flush remaining deferred edits
            self._flush_deferred_edits()

            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            try:
                stderr_out = proc.stderr.read().decode(
                    "utf-8", errors="replace").strip()
            except Exception:
                stderr_out = ""

            with state.lock:
                if state.ai_proc is proc:
                    state.ai_proc = None
                self._proc = None

            unsent = self._final_text[self._sent_text_count:]
            output = "\n\n".join(unsent).strip()

            if self._should_retry_without_session(session_id, proc.returncode):
                self._clear_stale_session(session_id)
                log.warning(
                    "Retrying [%s] without stale session_id: %s",
                    self.PROVIDER, session_id,
                )
                return self.run(message, session_id=None)

            # File viewer link
            had_new_files = len(state.modified_files) > files_count_before
            if self.cb.on_file_link:
                self.cb.on_file_link(had_new_files)

            # Save session summary + token log
            # Prefer original session_id (context-injected resume) over CLI's new one
            sid_for_save = session_id or self._captured_session_id
            self._save_session_summary(sid_for_save, message, output)
            if self._result_event:
                self._append_token_log(self._result_event, sid_for_save)

            if self._pending_questions:
                return (output or "", self._captured_session_id,
                        self._pending_questions)

            if proc.returncode != 0 and not output and self._sent_text_count == 0:
                if stderr_out:
                    err_msg = i18n.t("error.code", code=proc.returncode,
                                     detail=stderr_out[:500])
                else:
                    err_msg = i18n.t("error.code_short", code=proc.returncode)
                return err_msg, self._captured_session_id, None

            return (output or "", self._captured_session_id,
                    self._pending_questions)

        except subprocess.TimeoutExpired:
            with state.lock:
                if self._proc:
                    self._proc.kill()
                if state.ai_proc is self._proc:
                    state.ai_proc = None
                self._proc = None
            return i18n.t("error.timeout"), None, None

        except Exception as e:
            with state.lock:
                if state.ai_proc is self._proc:
                    state.ai_proc = None
                self._proc = None
            return i18n.t("error.generic", msg=str(e)), None, None

    def _build_cmd(self, message, session_id):
        raise NotImplementedError

    def _parse_event(self, event):
        """Parse raw JSONL event. Returns list[ParsedEvent]."""
        raise NotImplementedError

    def _build_env(self):
        """Common PATH environment. Runners can override to add more."""
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        if IS_WINDOWS:
            npm_prefix = os.path.join(env.get("APPDATA", ""), "npm")
            if os.path.isdir(npm_prefix):
                env["PATH"] = npm_prefix + ";" + env.get("PATH", "")
            py_scripts = os.path.join(
                os.path.expanduser("~"), "AppData", "Local",
                "Programs", "Python", "Scripts",
            )
            if os.path.isdir(py_scripts):
                env["PATH"] = py_scripts + ";" + env.get("PATH", "")
            # Auto-detect git-bash for Claude Code on Windows
            if "CLAUDE_CODE_GIT_BASH_PATH" not in env:
                for candidate in [
                    r"C:\Program Files\Git\bin\bash.exe",
                    r"D:\Git\bin\bash.exe",
                    r"C:\Git\bin\bash.exe",
                    os.path.join(env.get("ProgramFiles", ""), "Git", "bin", "bash.exe"),
                ]:
                    if os.path.isfile(candidate):
                        env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                        break
        else:
            env["HOME"] = os.path.expanduser("~")
            extra = ":".join(p for p in [
                os.path.expanduser("~/.local/bin"),
                os.path.expanduser("~/.npm-global/bin"),
                "/opt/homebrew/bin",
                "/opt/homebrew/sbin",
                "/usr/local/bin",
            ] if os.path.isdir(p))
            env["PATH"] = extra + ":/usr/bin:/bin:" + env.get("PATH", "")
            goroot = os.path.join(_cfg.WORK_DIR, "goroot")
            gopath = os.path.join(_cfg.WORK_DIR, "gopath")
            if os.path.isdir(goroot):
                env["GOROOT"] = goroot
                env["PATH"] = f"{gopath}/bin:{goroot}/bin:{env['PATH']}"
            if os.path.isdir(gopath):
                env["GOPATH"] = gopath
        return env

    def cancel(self):
        """Kill the running AI process."""
        if self._proc and self._proc.poll() is None:
            if IS_WINDOWS:
                self._proc.terminate()
            else:
                self._proc.kill()

    # --- Common event handling ---

    def _handle_parsed(self, parsed):
        """Collect text, track files, send callbacks."""
        # Text collection (from text events and tool_use events with embedded text)
        if parsed.text and parsed.kind in ("text", "tool_use"):
            self._final_text.append(parsed.text)

        # File tracking
        if parsed.file_paths and parsed.file_op:
            from state import add_modified_file
            if parsed.is_edit_deferred:
                self._pending_edit_snapshots.extend(parsed.file_paths)
            else:
                for fp in parsed.file_paths:
                    add_modified_file(
                        fp, content=parsed.file_content or None,
                        op=parsed.file_op,
                    )

        # Intermediate text on tool_use
        if parsed.kind == "tool_use":
            unsent = self._final_text[self._sent_text_count:]
            if unsent and self.cb.on_text:
                combined = "\n\n".join(unsent)
                if len(combined) > 30:
                    self.cb.on_text(combined)
                    log.info("Intermediate text: %d chars", len(combined))
                self._sent_text_count = len(self._final_text)

        # Result: cost + stats
        if parsed.kind == "result":
            self._result_event = parsed
            # Fallback text
            if parsed.text and not self._final_text:
                self._final_text.append(parsed.text)
            # Cost/stats update
            if parsed.cost_usd:
                state.last_cost = parsed.cost_usd
                state.total_cost += parsed.cost_usd
            stats = state.provider_stats.get(self.PROVIDER)
            if stats:
                stats["cost"] += parsed.cost_usd
                stats["tokens_in"] += parsed.tokens_in
                stats["tokens_out"] += parsed.tokens_out
            if self.cb.on_cost and (parsed.cost_usd or parsed.tokens_in):
                self.cb.on_cost(parsed)

    def _flush_deferred_edits(self):
        """Process Edit deferred snapshots -- read files from previous iteration."""
        if not self._pending_edit_snapshots:
            return
        from state import add_modified_file
        for fp in self._pending_edit_snapshots:
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                add_modified_file(fp, content=content, op="edit")
            except Exception:
                add_modified_file(fp, content=None, op="edit")
        self._pending_edit_snapshots.clear()

    def _make_status_description(self, parsed):
        """Generate status message from ParsedEvent (provider-agnostic)."""
        if not parsed.tool_name:
            return None
        tool_labels = i18n.t("tool_labels")
        if not isinstance(tool_labels, dict):
            tool_labels = {}
        label = tool_labels.get(parsed.tool_name, parsed.tool_name)
        if parsed.tool_name in ("Write", "Edit", "Read"):
            if parsed.file_paths:
                label += f": {os.path.basename(parsed.file_paths[0])}"
        elif parsed.tool_name in ("Bash", "shell"):
            cmd = parsed.tool_input.get("command", "")
            if cmd:
                label += f": {cmd[:40]}"
        elif parsed.tool_name in ("Grep", "Glob"):
            pat = parsed.tool_input.get("pattern", "")
            if pat:
                label += f": {pat[:30]}"
        elif parsed.tool_name == "TodoWrite":
            todos = parsed.tool_input.get("todos", [])
            in_prog = [t for t in todos if t.get("status") == "in_progress"]
            if in_prog:
                label += f": {in_prog[0].get('activeForm', '')[:30]}"
            else:
                label += f" {i18n.t('todo_count', count=len(todos))}"
        return label

    def _should_retry_without_session(self, session_id, returncode):
        """Retry once without resume if the saved native session no longer exists."""
        if not session_id or self.RESUME_MODE != "session_id" or returncode == 0:
            return False
        if not self._result_event or not self._result_event.is_error:
            return False
        for err in self._result_event.errors:
            if isinstance(err, str) and "No conversation found with session ID" in err:
                return True
        return False

    def _clear_stale_session(self, session_id):
        """Drop a dead persisted session id so future runs start cleanly."""
        from config import update_config

        if state._provider_sessions.get(self.PROVIDER) == session_id:
            state._provider_sessions.pop(self.PROVIDER, None)
        if state.provider == self.PROVIDER and state.session_id == session_id:
            state.session_id = None
            update_config("session_id", None)
        update_config("provider_sessions", dict(state._provider_sessions))

    # --- Session context ---

    def _maybe_inject_context(self, message, session_id):
        """Inject previous conversation context when native resume is unavailable."""
        if not session_id:
            return message
        if self.RESUME_MODE == "session_id":
            return message
        if self.RESUME_MODE == "last_only" and self._is_last_session(session_id):
            return message

        data = self._load_session(session_id)
        if not data or not data.get("exchanges"):
            return message

        recent = data["exchanges"][-3:]
        lines = []
        for ex in recent:
            lines.append(f"User: {ex['user']}")
            out = ex.get("output", "")
            if len(out) > 500:
                out = out[:500] + "..."
            lines.append(f"Assistant: {out}")

        context = "\n".join(lines)
        return (f"[Previous conversation]\n{context}\n\n"
                f"[Current request]\n{message}")

    def _is_last_session(self, session_id):
        return session_id == state.session_id

    def _load_session(self, session_id):
        from config import DATA_DIR
        path = os.path.join(DATA_DIR, "sessions", f"{session_id}.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_session_summary(self, session_id, user_message, output):
        """Save raw conversation exchange for context injection."""
        if not session_id:
            return
        from config import DATA_DIR
        session_dir = os.path.join(DATA_DIR, "sessions")
        os.makedirs(session_dir, exist_ok=True)
        path = os.path.join(session_dir, f"{session_id}.json")

        data = self._load_session(session_id) or {
            "provider": self.PROVIDER,
            "model": state.model or "",
            "exchanges": [],
        }
        data["provider"] = self.PROVIDER
        data["model"] = state.model or ""

        modified = ([e["path"] for e in state.modified_files[-10:]]
                    if state.modified_files else [])
        # Strip context injection prefix if present
        _CTX_MARKER = "[Current request]\n"
        if _CTX_MARKER in user_message:
            user_message = user_message.split(_CTX_MARKER, 1)[1]
        data["exchanges"].append({
            "user": user_message[:1000],
            "output": (output or "")[:2000],
            "files_modified": modified,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        data["exchanges"] = data["exchanges"][-20:]

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("Failed to save session summary: %s", e)

    # --- Token log ---

    def _append_token_log(self, parsed, session_id=None):
        """Append token usage to ~/.sumone/token_log.jsonl with file locking."""
        if not parsed or parsed.kind != "result":
            return
        if not parsed.tokens_in and not parsed.tokens_out:
            return
        from config import DATA_DIR
        log_path = os.path.join(DATA_DIR, "token_log.jsonl")
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "provider": self.PROVIDER,
            "model": state.model or "",
            "in": parsed.tokens_in,
            "out": parsed.tokens_out,
            "cached": parsed.tokens_cached,
            "cost": parsed.cost_usd if parsed.cost_usd else None,
            "session": session_id or self._captured_session_id or "",
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                if IS_WINDOWS:
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    f.write(line)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX)
                    f.write(line)
                    fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            log.warning("Failed to append token log: %s", e)

    # --- CLI command discovery ---

    @classmethod
    def _find_cli_cmd(cls, candidates):
        """Find CLI command (cached). Prevents repeated lookups per instance."""
        cache_key = cls.PROVIDER
        if cache_key in cls._cli_cmd_cache:
            return cls._cli_cmd_cache[cache_key]
        for cmd in candidates:
            try:
                kw = {}
                if IS_WINDOWS:
                    kw["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(
                    [cmd, "--version"], capture_output=True, timeout=10, **kw,
                )
                if result.returncode == 0:
                    cls._cli_cmd_cache[cache_key] = cmd
                    return cmd
            except Exception:
                continue
        fallback = candidates[0]
        cls._cli_cmd_cache[cache_key] = fallback
        return fallback


# ---------------------------------------------------------------------------
# Runner cache + factory
# ---------------------------------------------------------------------------

_runner_cache = {}


def get_runner(callbacks=None):
    """Return the appropriate Runner based on state.provider."""
    ai = state.provider or settings.get("default_model", "claude")

    cached = _runner_cache.get(ai)
    if cached and cached.cb is callbacks:
        return cached

    from ai.claude import ClaudeRunner
    from ai.codex import CodexRunner
    from ai.gemini import GeminiRunner
    runners = {"claude": ClaudeRunner, "codex": CodexRunner, "gemini": GeminiRunner}

    runner_cls = runners.get(ai, ClaudeRunner)
    runner = runner_cls(callbacks=callbacks)
    _runner_cache[ai] = runner
    return runner
