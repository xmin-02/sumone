"""Claude CLI runner."""
import os
import re
import shlex
import subprocess

from ai import BaseRunner, ParsedEvent
from config import IS_WINDOWS, log
import config as _cfg
from state import state


def _parse_deleted_paths(command, cwd=None):
    """Parse file paths from deletion commands (cross-platform).

    Detects: rm, rm -f, rm -rf, rmdir (Unix/Git Bash on Windows)
             del, erase, rmdir, rd (Windows CMD)
             Remove-Item, ri, rm (PowerShell)
    Returns list of absolute file paths.
    """
    if not command or not command.strip():
        return []
    cwd = cwd or _cfg.WORK_DIR
    paths = []

    for segment in re.split(r'\s*(?:&&|\|\||;)\s*', command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue

        cmd_base = os.path.basename(tokens[0]).lower()
        if cmd_base.endswith(".exe"):
            cmd_base = cmd_base[:-4]

        is_delete_cmd = False
        if cmd_base in ("rm", "rmdir"):
            is_delete_cmd = True
        elif cmd_base in ("del", "erase", "rd"):
            is_delete_cmd = True
        elif cmd_base in ("remove-item", "ri"):
            is_delete_cmd = True

        if not is_delete_cmd:
            continue

        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            if (token.startswith("/") and len(token) > 1
                    and token[1:2].isalpha() and IS_WINDOWS):
                if len(token) <= 3:
                    continue
            p = token.strip("'\"")
            if not p or p in (".", ".."):
                continue
            if IS_WINDOWS and re.match(r'^/[a-zA-Z]/', p):
                p = p[1].upper() + ":" + p[2:]
            if not os.path.isabs(p):
                p = os.path.join(cwd, p)
            p = os.path.normpath(p)
            paths.append(p)

    return paths


class ClaudeRunner(BaseRunner):
    """Claude Code CLI runner."""

    PROVIDER = "claude"
    RESUME_MODE = "session_id"

    def _build_cmd(self, message, session_id):
        cmd_name = self._find_cli_cmd(["claude", "claude.cmd"])
        cmd = [cmd_name]
        if session_id:
            cmd += ["-r", session_id]
        cmd += ["-p", message, "--output-format", "stream-json",
                "--verbose", "--dangerously-skip-permissions"]
        if state.model:
            cmd += ["--model", state.model]
        return cmd

    def _build_env(self):
        env = super()._build_env()
        env["CLAUDE_TELEGRAM_BOT"] = "1"
        return env

    def _parse_event(self, event):
        """Parse Claude JSONL event. Returns list[ParsedEvent]."""
        etype = event.get("type", "")
        sid = event.get("session_id", "")

        if etype == "assistant":
            content = event.get("message", {}).get("content", [])
            if not isinstance(content, list):
                return [ParsedEvent(session_id=sid)]

            texts = []
            tool_events = []
            questions = None

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "text":
                    t_val = block.get("text", "").strip()
                    if t_val:
                        texts.append(t_val)

                elif btype == "tool_use":
                    t_name = block.get("name", "")
                    inp = block.get("input", {})

                    if t_name == "AskUserQuestion":
                        qs = inp.get("questions", [])
                        if qs:
                            questions = qs
                            break

                    parsed = ParsedEvent(
                        kind="tool_use",
                        tool_name=t_name,
                        tool_input=inp,
                        session_id=sid,
                    )

                    # File tracking
                    if t_name == "Write":
                        fp = inp.get("file_path", "")
                        if fp:
                            parsed.file_paths = [fp]
                            parsed.file_op = "write"
                            parsed.file_content = inp.get("content", "")
                    elif t_name == "Edit":
                        fp = inp.get("file_path", "")
                        if fp:
                            parsed.file_paths = [fp]
                            parsed.file_op = "edit"
                            parsed.is_edit_deferred = True
                    elif t_name == "Bash":
                        bash_cmd = inp.get("command", "")
                        deleted = _parse_deleted_paths(bash_cmd, cwd=_cfg.WORK_DIR)
                        if deleted:
                            parsed.file_paths = deleted
                            parsed.file_op = "delete"

                    tool_events.append(parsed)

            if questions:
                p = ParsedEvent(session_id=sid, questions=questions)
                if texts:
                    p.kind = "text"
                    p.text = "\n\n".join(texts)
                return [p]

            results = []
            if texts and not tool_events:
                results.append(ParsedEvent(
                    kind="text", text="\n\n".join(texts), session_id=sid))
            elif tool_events:
                # Attach collected text to first tool event
                if texts:
                    tool_events[0].text = "\n\n".join(texts)
                results.extend(tool_events)
            else:
                results.append(ParsedEvent(session_id=sid))

            return results

        if etype == "result":
            result_text = event.get("result", "")
            usage = event.get("usage", {})
            return [ParsedEvent(
                kind="result",
                text=result_text,
                session_id=sid or event.get("session_id", ""),
                cost_usd=event.get("total_cost_usd", 0) or 0,
                duration_ms=event.get("duration_ms", 0) or 0,
                num_turns=event.get("num_turns", 0) or 0,
                tokens_in=(usage.get("input_tokens", 0)
                           + usage.get("cache_read_input_tokens", 0)),
                tokens_out=usage.get("output_tokens", 0),
                tokens_cached=usage.get("cache_read_input_tokens", 0),
            )]

        return [ParsedEvent(session_id=sid)]
