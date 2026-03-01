"""Pwd command: /pwd."""
from commands import command
from i18n import t
import config
from telegram import escape_html, send_html


@command("/pwd")
def handle_pwd(text):
    send_html(f"<b>{t('pwd.title')}</b>\n<code>{escape_html(config.WORK_DIR)}</code>")
