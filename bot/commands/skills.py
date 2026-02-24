"""Skills and builtin command listings + dynamic plugin menus."""
from commands import command, _handlers
from i18n import t
from config import log
from telegram import escape_html, send_html

# ---------------------------------------------------------------------------
# Static command listings
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Dynamic per-plugin text menus (click-to-copy commands)
# ---------------------------------------------------------------------------

_plugin_skills = {}   # {plugin_name: [(cmd, desc), ...]}


def register_plugin_menus(plugin_groups):
    """Register per-plugin menu commands dynamically.

    Called from main._sync_bot_commands() after skill discovery.
    For each plugin, registers a /<plugin_name> command that shows
    a text list of skills with click-to-copy command names.
    """
    global _plugin_skills
    _plugin_skills = dict(plugin_groups)

    for plugin_name in plugin_groups:
        menu_cmd = "/" + plugin_name.lower().replace("-", "_")
        if menu_cmd not in _handlers:
            def _make_handler(pname):
                def handler(text):
                    _show_plugin_menu(pname)
                return handler
            _handlers[menu_cmd] = _make_handler(plugin_name)
            log.info("Registered plugin menu command: %s (%d skills)",
                     menu_cmd, len(plugin_groups[plugin_name]))


def _show_plugin_menu(plugin_name):
    """Send a text list of plugin skills with click-to-copy commands."""
    skills = _plugin_skills.get(plugin_name, [])
    if not skills:
        return

    title = t("plugin_menu.title", plugin=plugin_name, count=len(skills))
    hint = t("plugin_menu.hint")

    lines = [f"<b>{title}</b>", '━' * 25]
    for cmd, desc in skills:
        # <code>/cmd</code> makes it tappable to copy in Telegram
        lines.append(f"<code>/{cmd}</code> — {escape_html(desc)}")
    lines.append('━' * 25)
    lines.append(f"<i>{hint}</i>")

    send_html("\n".join(lines))
