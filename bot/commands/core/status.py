"""Status command: /status."""
import platform

from commands import command
from i18n import t
from config import IS_WINDOWS, AI_MODELS
from state import state
from telegram import escape_html, send_html


@command("/status")
def handle_status(text):
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else t("status.no_session")
    _prov_label2 = AI_MODELS.get(state.provider, {}).get("label", state.provider.title())
    _model_name2 = escape_html(state.model) if state.model else t('model.cli_default')
    model_info = f"{_prov_label2} - <code>{_model_name2}</code>"
    busy_info = t("status.processing") if state.busy else t("status.idle")
    os_info = f"Windows ({platform.version()})" if IS_WINDOWS else platform.platform()
    msg = (f"<b>{t('status.title')}</b>\n{'━'*25}\n"
           f"{t('status.session')}: {session_info}\n{t('status.model_label')}: {model_info}\n"
           f"{t('status.state')}: {busy_info}\n{t('status.os')}: {escape_html(os_info)}\n")
    send_html(msg)
