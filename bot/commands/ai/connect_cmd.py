"""Connect command: /connect [provider]."""
import threading

from commands import command
from i18n import t
from config import AI_MODELS
from telegram import send_html


@command("/connect")
def handle_connect(text):
    args = text.split()[1:]  # everything after /connect
    if not args:
        send_html(t("ai_connect.connect_usage"))
        return

    provider = args[0].lower()
    if provider not in AI_MODELS:
        send_html(t("ai_connect.unknown_provider", name=f"<code>{provider}</code>"))
        return

    from ai.connect import is_connect_active, run_connect_flow
    if is_connect_active():
        send_html(t("ai_connect.already_active"))
        return

    prov_label = AI_MODELS[provider].get("label", provider.title())
    send_html(f"🔌 <b>{prov_label}</b> — {t('ai_connect.started')}")
    threading.Thread(target=run_connect_flow, args=(provider,), daemon=True).start()
