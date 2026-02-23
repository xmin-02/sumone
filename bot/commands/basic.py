"""Basic commands: /help, /status, /cost, /model, /cancel."""
import platform

from commands import command
from i18n import t
from config import IS_WINDOWS, MODEL_ALIASES, settings, log
from state import state
from telegram import escape_html, send_html, CHAT_ID
from tokens import get_global_usage


@command("/help", aliases=["/start"])
def handle_help(text):
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else t("status.no_session").split("(")[0].strip()
    model_info = escape_html(state.model) if state.model else t("model.default_name")
    msg = (
        f"<b>{t('help.title')}</b>\n" + '━'*25 + "\n\n"
        f"<b>{t('help.usage_title')}</b>\n{t('help.usage_body')}\n\n"
        f"<b>{t('help.session_title')}</b>\n{t('help.session_body')}\n\n"
        f"<b>{t('help.model_title')}</b>\n{t('help.model_body')}\n\n"
        f"<b>{t('help.commands_title')}</b>\n{t('help.commands_body')}\n\n"
        f"<b>{t('help.examples_title')}</b>\n<code>{t('help.examples')}</code>\n\n"
        + '━'*25 + f"\n{t('status.session')}: {session_info} | {t('status.model_label')}: <code>{model_info}</code>\n"
    )
    send_html(msg)


@command("/status")
def handle_status(text):
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else t("status.no_session")
    model_info = f"<code>{escape_html(state.model)}</code>" if state.model else t("model.default_name")
    busy_info = t("status.processing") if state.busy else t("status.idle")
    os_info = f"Windows ({platform.version()})" if IS_WINDOWS else platform.platform()
    msg = (f"<b>{t('status.title')}</b>\n{'━'*25}\n"
           f"{t('status.session')}: {session_info}\n{t('status.model_label')}: {model_info}\n"
           f"{t('status.state')}: {busy_info}\n{t('status.os')}: {escape_html(os_info)}\n")
    send_html(msg)


@command("/cost")
def handle_cost(text):
    msg = (f"<b>{t('cost.title')}</b>\n{'━'*25}\n"
           f"{t('cost.last')}: ${state.last_cost:.4f}\n"
           f"{t('cost.session_total')}: ${state.total_cost:.4f}\n")
    if settings["show_global_cost"]:
        try:
            g_cost, g_in, g_out, g_sessions = get_global_usage()
            msg += (f"\n<b>{t('cost.global_title')}</b>\n{'━'*25}\n"
                    f"{t('cost.total_cost')}: ${g_cost:.4f}\n"
                    f"{t('cost.total_sessions')}: {g_sessions}\n"
                    f"{t('cost.input_tokens')}: {g_in:,}\n"
                    f"{t('cost.output_tokens')}: {g_out:,}\n"
                    f"{t('cost.total_tokens')}: {g_in + g_out:,}\n")
        except Exception:
            pass
    send_html(msg)


@command("/model")
def handle_model(text):
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() == "":
        current = state.model or t("model.default_name")
        aliases = ", ".join(sorted(MODEL_ALIASES.keys()))
        send_html(
            f"<b>{t('model.current')}:</b> <code>{escape_html(current)}</code>\n{'━'*25}\n"
            f"<b>{t('model.usage')}:</b> /model [name]\n<b>{t('model.aliases')}:</b> {escape_html(aliases)}\n"
            f"<b>{t('model.examples')}:</b>\n  /model opus\n  /model sonnet\n  /model haiku\n"
            f"  /model {t('model.restore_default')}")
        return
    name = parts[1].strip().lower()
    reset_kw = t("model.reset_keywords")
    if isinstance(reset_kw, list) and name in reset_kw:
        state.model = None
        send_html(f"<b>{t('model.reset_done')}:</b> {t('model.reset_to')}"); return
    resolved = MODEL_ALIASES.get(name)
    if not resolved:
        if name.startswith("claude-"): resolved = name
        else:
            aliases = ", ".join(sorted(MODEL_ALIASES.keys()))
            send_html(t("error.unknown_model", name=f"<code>{escape_html(name)}</code>", aliases=escape_html(aliases))); return
    state.model = resolved
    send_html(f"<b>{t('model.changed')}:</b> <code>{escape_html(resolved)}</code>")


@command("/cancel")
def handle_cancel(text):
    with state.lock: proc = state.claude_proc; was_busy = state.busy
    if proc and proc.poll() is None:
        if IS_WINDOWS:
            proc.terminate()
        else:
            proc.kill()
        with state.lock: state.claude_proc = None; state.busy = False
        send_html(f"<b>{t('cancel.done')}</b> {t('cancel.killed')}")
    elif was_busy:
        with state.lock: state.busy = False
        send_html(f"<b>{t('cancel.reset')}</b> {t('cancel.cleared')}")
    else:
        send_html(t("cancel.nothing"))
