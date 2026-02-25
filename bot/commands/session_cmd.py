"""Session commands: /session, /clear, selection, and question answering."""
import re

from commands import command
from i18n import t
from config import update_config, AI_MODELS, log
from state import state, switch_provider
from telegram import escape_html, send_html
from sessions import get_sessions, get_provider_sessions, get_session_model, get_session_provider, find_project_dirs
import os


def _save_session_id(sid):
    update_config("session_id", sid)


@command("/session", aliases=["/sessions"])
def handle_session(text):
    sessions = get_provider_sessions(state.provider, 10)
    state.session_list = sessions
    state.selecting = True
    if not sessions:
        send_html(f"<b>{t('session.none')}</b>"); return
    lines = []
    for i, (sid, ts, preview) in enumerate(sessions, 1):
        p = preview[:50] + "..." if len(preview) > 50 else preview
        lines.append(f"<b>{i}.</b> <code>{sid[:8]}</code> {escape_html(ts)}\n    {escape_html(p)}")
    provider_label = AI_MODELS.get(state.provider, {}).get("label", state.provider.title())
    current = ""
    if state.session_id: current = f"\n{t('session.current')}: <code>{state.session_id[:8]}</code>"
    msg = (f"<b>{t('session.title')}</b> ({provider_label}){current}\n{'━'*25}\n"
           + "\n".join(lines)
           + f"\n{'━'*25}\n{t('session.prompt')}")
    send_html(msg)


@command("/clear", aliases=["/new"])
def handle_clear(text):
    state._provider_sessions.pop(state.provider, None)
    state.session_id = None; state.selecting = False
    state.answering = False; state.pending_question = None
    _save_session_id(None)
    send_html(f"<b>{t('session.cleared')}</b>\n{t('session.cleared_desc')}")


def show_questions(questions, sid):
    """Display Claude's AskUserQuestion to the user."""
    lines = []; all_options = []
    for qi, q in enumerate(questions):
        header = q.get("header", ""); question = q.get("question", "")
        options = q.get("options", []); multi = q.get("multiSelect", False)
        icon = "\U0001f4cb" if multi else "\u2753"
        line = f"{icon} {escape_html(question)}"
        if header: line = f"<b>[{escape_html(header)}]</b> {line}"
        lines.append(line)
        for oi, opt in enumerate(options):
            num = len(all_options) + 1
            label = opt.get("label", ""); desc = opt.get("description", "")
            entry = f"  <b>{num}.</b> {escape_html(label)}"
            if desc: entry += f" — {escape_html(desc)}"
            lines.append(entry)
            all_options.append({"label": label, "q_idx": qi, "opt_idx": oi})
    body = "\n".join(lines)
    msg = (f"<b>{t('question.title')}</b>\n{'━'*25}\n{body}\n{'━'*25}\n"
           f"{t('question.prompt')}")
    send_html(msg)
    state.pending_question = {"session_id": sid, "questions": questions, "options_map": all_options}
    state.answering = True
    log.info("Entered answering mode: %d options", len(all_options))


def handle_answer(text):
    """Handle answer to Claude's question. Called from main."""
    text = text.strip()
    pq = state.pending_question
    if not pq:
        state.answering = False
        from main import handle_message
        handle_message(text); return
    options_map = pq["options_map"]; sid = pq["session_id"]
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(options_map):
            chosen = options_map[idx]; label = chosen["label"]
            state.answering = False; state.pending_question = None
            if sid: state.session_id = sid; _save_session_id(sid)
            answer_text = t("question.selected", label=label)
            log.info("Answer: %s (option %d)", label, idx + 1)
            from main import handle_message
            handle_message(answer_text); return
        else:
            send_html(t("error.invalid_number", max=len(options_map))); return
    state.answering = False; state.pending_question = None
    if sid: state.session_id = sid; _save_session_id(sid)
    from main import handle_message
    handle_message(text)


def _connect_session(sid):
    """Connect to a session, auto-switching provider if needed."""
    # Check if session belongs to a different provider
    sess_provider = get_session_provider(sid)
    if sess_provider and sess_provider != state.provider:
        switch_provider(sess_provider)
    state.session_id = sid
    state._provider_sessions[state.provider] = sid
    _save_session_id(sid)
    sess_model = get_session_model(sid)
    if sess_model:
        state.model = sess_model


def handle_selection(text):
    """Handle session selection. Called from main."""
    text = text.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(state.session_list):
            sid, ts, preview = state.session_list[idx]
            state.selecting = False
            _connect_session(sid)
            p = preview[:60] + "..." if len(preview) > 60 else preview
            provider_label = AI_MODELS.get(state.provider, {}).get("label", state.provider.title())
            model_line = f"\n{t('status.model_label')}: {provider_label} - <code>{escape_html(state.model or 'default')}</code>"
            send_html(
                f"<b>{t('session.connected')}</b>\nID: <code>{sid[:8]}</code>\n"
                f"{escape_html(ts)}\n{escape_html(p)}{model_line}\n"
                f"{'━'*25}\n{t('session.continue_hint')}")
        else:
            send_html(t("error.invalid_number", max=len(state.session_list)))
        return
    uuid_pat = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    if uuid_pat.match(text):
        # Check Claude JSONL dirs
        found = False
        for proj_dir in find_project_dirs():
            if os.path.exists(os.path.join(proj_dir, f"{text}.jsonl")):
                found = True; break
        # Also check sumone sessions
        if not found:
            from sessions import _SUMONE_SESSIONS
            if os.path.isfile(os.path.join(_SUMONE_SESSIONS, f"{text}.json")):
                found = True
        if found:
            state.selecting = False
            _connect_session(text)
            provider_label = AI_MODELS.get(state.provider, {}).get("label", state.provider.title())
            model_info = f" | {provider_label} - {escape_html(state.model)}" if state.model else ""
            send_html(f"<b>{t('session.connected')}</b> <code>{text[:8]}</code>{model_info}")
        else:
            send_html(t("session.not_found"))
        return
    state.selecting = False
    from main import handle_message
    handle_message(text)
