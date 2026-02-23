"""Skills and builtin command listings."""
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


@command("/skills")
def handle_skills(text):
    msg = (
        f"<b>{t('skills.title')}</b>\n" + '━'*25 + "\n"
        f"<b>{t('skills.exec_modes')}</b>\n{t('skills.exec_modes_list')}\n"
        f"\n<b>{t('skills.planning')}</b>\n{t('skills.planning_list')}\n"
        f"\n<b>{t('skills.quality')}</b>\n{t('skills.quality_list')}\n"
        f"\n<b>{t('skills.utilities')}</b>\n{t('skills.utilities_list')}\n"
        f"\n<b>{t('skills.config')}</b>\n{t('skills.config_list')}\n"
        f"\n<b>{t('skills.bot_mgmt')}</b>\n{t('skills.bot_mgmt_list')}\n"
        + '━'*25 + f"\n<i>{t('skills.example')}</i>")
    send_html(msg)
