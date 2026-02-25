"""Codex CLI runner."""
import os

from ai import BaseRunner, ParsedEvent
from ai.claude import _parse_deleted_paths
from config import log
import config as _cfg
from state import state


class CodexRunner(BaseRunner):
    """Codex CLI runner."""

    PROVIDER = "codex"
    RESUME_MODE = "last_only"

    def _build_cmd(self, message, session_id):
        cmd_name = self._find_cli_cmd(["codex", "codex.cmd"])
        cmd = [cmd_name, "exec", "--json",
               "--dangerously-bypass-approvals-and-sandbox"]
        if state.model:
            cmd += ["-m", state.model]
        cmd.append(message)
        return cmd

    def _build_env(self):
        env = super()._build_env()
        return env

    def _parse_event(self, event):
        """Parse Codex JSONL event. Returns list[ParsedEvent]."""
        etype = event.get("type", "")

        if etype == "thread.started":
            sid = event.get("thread_id", "")
            return [ParsedEvent(session_id=sid)]

        if etype == "item.completed":
            item = event.get("item", {})
            itype = item.get("type", "")

            if itype == "agent_message":
                text = item.get("text", "").strip()
                if text:
                    return [ParsedEvent(kind="text", text=text)]
                return [ParsedEvent()]

            if itype == "command_execution":
                command = item.get("command", "")
                parsed = ParsedEvent(
                    kind="tool_use",
                    tool_name="shell",
                    tool_input={"command": command},
                )
                # Detect file deletions from shell commands
                deleted = _parse_deleted_paths(command, cwd=_cfg.WORK_DIR)
                if deleted:
                    parsed.file_paths = deleted
                    parsed.file_op = "delete"
                return [parsed]

            if itype == "reasoning":
                return [ParsedEvent()]

            if itype == "error":
                text = item.get("message", "")
                if text:
                    return [ParsedEvent(kind="text", text=text)]
                return [ParsedEvent()]

            return [ParsedEvent()]

        if etype == "turn.completed":
            usage = event.get("usage", {})
            return [ParsedEvent(
                kind="result",
                tokens_in=usage.get("input_tokens", 0),
                tokens_out=usage.get("output_tokens", 0),
                tokens_cached=usage.get("cached_input_tokens", 0),
            )]

        if etype == "error":
            text = event.get("message", "")
            if text:
                return [ParsedEvent(kind="text", text=text)]
            return [ParsedEvent()]

        # turn.started, item.started, etc. — ignore
        return [ParsedEvent()]
