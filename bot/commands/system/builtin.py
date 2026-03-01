"""Builtin command: /builtin."""
from commands import command
from i18n import t
from telegram import send_html


@command("/builtin")
def handle_builtin(text):
    msg = (
        f"<b>{t('builtin.title')}</b>\n" + '━'*25 + "\n"
        f"<b>{t('builtin.handled')}</b>\n{t('builtin.handled_list')}\n"
        f"\n<b>{t('builtin.passed')}</b>\n{t('builtin.passed_list')}\n"
        f"\n<b>{t('builtin.cli_only')}</b>\n{t('builtin.cli_only_list')}\n")
    send_html(msg)
