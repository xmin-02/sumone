"""Clear command: /clear, /new."""
from commands import command
from i18n import t
from config import AI_MODELS, update_config
from state import state
from telegram import send_html


@command("/clear", aliases=["/new"])
def handle_clear(text):
    prov_label = AI_MODELS.get(state.provider, {}).get("label", state.provider.title())
    state._provider_sessions.pop(state.provider, None)
    state.session_id = None; state.selecting = False
    state.answering = False; state.pending_question = None
    update_config("session_id", None)
    send_html(f"<b>{t('session.cleared')}</b> ({prov_label})\n{t('session.cleared_desc')}")
