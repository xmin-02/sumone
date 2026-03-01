"""Gemini CLI runner."""
import os

from ai import BaseRunner, ParsedEvent
from ai.claude import _parse_deleted_paths
import config as _cfg
from state import state, get_provider_env


class GeminiRunner(BaseRunner):
    """Gemini CLI runner."""

    PROVIDER = "gemini"
    RESUME_MODE = "none"

    def _build_cmd(self, message, session_id):
        cmd_name = self._find_cli_cmd(["gemini", "gemini.cmd"])
        cmd = [cmd_name, "-p", message, "-o", "stream-json",
               "--approval-mode", "yolo"]
        if state.model:
            cmd += ["-m", state.model]
        return cmd

    def _build_env(self):
        env = super()._build_env()
        env.update(get_provider_env("gemini"))
        return env

    def _parse_event(self, event):
        """Parse Gemini JSONL event. Returns list[ParsedEvent]."""
        etype = event.get("type", "")

        if etype == "init":
            sid = event.get("session_id", "")
            return [ParsedEvent(session_id=sid)]

        if etype == "message":
            role = event.get("role", "")
            if role == "assistant":
                text = event.get("content", "").strip()
                if text:
                    return [ParsedEvent(kind="text", text=text)]
            return [ParsedEvent()]

        if etype == "tool_use":
            tool_name = event.get("tool_name", "")
            params = event.get("parameters", {})
            parsed = ParsedEvent(
                kind="tool_use",
                tool_input=params,
            )

            if tool_name == "write_file":
                fp = params.get("file_path", "")
                if fp:
                    if not os.path.isabs(fp):
                        fp = os.path.join(_cfg.WORK_DIR, fp)
                    parsed.tool_name = "Write"
                    parsed.file_paths = [fp]
                    parsed.file_op = "write"
                    parsed.file_content = params.get("content", "")
            elif tool_name == "edit_file":
                fp = params.get("file_path", "")
                if fp:
                    if not os.path.isabs(fp):
                        fp = os.path.join(_cfg.WORK_DIR, fp)
                    parsed.tool_name = "Edit"
                    parsed.file_paths = [fp]
                    parsed.file_op = "edit"
                    parsed.is_edit_deferred = True
            elif tool_name == "read_file":
                fp = params.get("file_path", "")
                if fp:
                    if not os.path.isabs(fp):
                        fp = os.path.join(_cfg.WORK_DIR, fp)
                    parsed.tool_name = "Read"
                    parsed.file_paths = [fp]
            elif tool_name in ("run_shell_command", "shell",
                               "execute_command"):
                cmd = params.get("command", "")
                parsed.tool_name = "Bash"
                parsed.tool_input = {"command": cmd}
                deleted = _parse_deleted_paths(cmd, cwd=_cfg.WORK_DIR)
                if deleted:
                    parsed.file_paths = deleted
                    parsed.file_op = "delete"
            else:
                parsed.tool_name = tool_name

            return [parsed]

        if etype == "tool_result":
            return [ParsedEvent()]

        if etype == "error":
            text = event.get("message", "") or event.get("error", "")
            if text:
                return [ParsedEvent(kind="text", text=text, is_error=True)]
            return [ParsedEvent()]

        if etype == "result":
            stats = event.get("stats", {})
            return [ParsedEvent(
                kind="result",
                tokens_in=stats.get("input_tokens", 0),
                tokens_out=stats.get("output_tokens", 0),
                tokens_cached=stats.get("cached", 0),
                duration_ms=stats.get("duration_ms", 0),
            )]

        return [ParsedEvent()]
