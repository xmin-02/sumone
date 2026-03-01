"""Help command: /help, /start."""
from commands import command
from i18n import t
from config import AI_MODELS
from state import state
from telegram import escape_html, send_html


@command("/help", aliases=["/start"])
def handle_help(text):
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else t("status.no_session").split("(")[0].strip()
    _prov_label = AI_MODELS.get(state.provider, {}).get("label", state.provider.title())
    _model_name = escape_html(state.model) if state.model else t('model.cli_default')
    model_info = f"{_prov_label} - {_model_name}"
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
