"""Claude CLI runner."""
import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request

from ai import BaseRunner, ParsedEvent
from config import IS_WINDOWS, log
import config as _cfg
from state import state, get_provider_auth, get_provider_env, set_provider_auth


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

    def _refresh_oauth_token(self):
        """Refresh stored Claude OAuth access token if a refresh token exists."""
        auth = get_provider_auth("claude")
        refresh = auth.get("oauth_refresh_token")
        if not refresh:
            return

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        }
        req = urllib.request.Request(
            "https://platform.claude.com/v1/oauth/token",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "sumone/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                data = json.loads(body or "{}")
        except Exception as e:
            log.warning("Claude OAuth refresh failed: %s", e)
            return

        access_token = data.get("access_token")
        new_refresh = data.get("refresh_token") or refresh
        if not access_token:
            log.warning("Claude OAuth refresh returned no access_token")
            return

        updated = dict(auth)
        updated["oauth_token"] = access_token
        updated["oauth_refresh_token"] = new_refresh
        account = data.get("account") or {}
        organization = data.get("organization") or {}
        if account.get("uuid"):
            updated["account_uuid"] = account["uuid"]
        if account.get("email_address"):
            updated["user_email"] = account["email_address"]
        if organization.get("uuid"):
            updated["organization_uuid"] = organization["uuid"]
        set_provider_auth("claude", updated)
        log.info("Claude OAuth token refreshed")

    def _build_env(self):
        self._refresh_oauth_token()
        env = super()._build_env()
        env["CLAUDE_TELEGRAM_BOT"] = "1"
        env.update(get_provider_env("claude"))
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
            errors = event.get("errors", [])
            result_text = event.get("result", "")
            if not result_text and isinstance(errors, list):
                result_text = "\n".join(
                    err for err in errors if isinstance(err, str) and err.strip()
                )
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
                is_error=bool(event.get("is_error")),
                errors=errors if isinstance(errors, list) else [],
            )]

        return [ParsedEvent(session_id=sid)]
