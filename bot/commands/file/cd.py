"""Cd command: /cd."""
import os

from commands import command
from i18n import t
import config
from state import state
from telegram import escape_html, send_html


@command("/cd")
def handle_cd(text):
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        send_html(f"<b>{t('cd.current')}:</b> <code>{escape_html(config.WORK_DIR)}</code>\n<b>{t('cd.usage')}</b>")
        return
    target = parts[1].strip()
    if target == "~":
        target = os.path.expanduser("~")
    elif target == "-":
        target = getattr(state, "prev_dir", config.WORK_DIR)
    elif target == "..":
        target = os.path.dirname(config.WORK_DIR)
    elif not os.path.isabs(target):
        target = os.path.join(config.WORK_DIR, target)
    target = os.path.normpath(target)
    if not os.path.isdir(target):
        send_html(f"<b>{t('error.dir_not_found')}</b>\n<code>{escape_html(target)}</code>")
        return
    state.prev_dir = config.WORK_DIR
    config.WORK_DIR = target
    send_html(f"<b>{t('cd.done')}</b>\n<code>{escape_html(config.WORK_DIR)}</code>")
