"""Ls command: /ls."""
import os

from commands import command
from i18n import t
import config
from telegram import escape_html, send_html


@command("/ls")
def handle_ls(text):
    args = text.split()[1:]
    show_all = False; target = config.WORK_DIR
    for arg in args:
        if arg.startswith("-"):
            if "a" in arg: show_all = True
        else:
            target = arg
    if not os.path.isabs(target):
        target = os.path.join(config.WORK_DIR, target)
    target = os.path.normpath(target)
    if not os.path.isdir(target):
        send_html(f"<b>{t('error.dir_not_found')}</b>\n<code>{escape_html(target)}</code>")
        return
    try:
        entries = os.listdir(target)
    except PermissionError:
        send_html(f"<b>{t('error.permission_denied')}</b>\n<code>{escape_html(target)}</code>")
        return
    if not show_all:
        entries = [e for e in entries if not e.startswith(".")]
    dirs = []; files = []
    for name in sorted(entries, key=str.lower):
        full = os.path.join(target, name)
        if os.path.isdir(full):
            dirs.append(f"\U0001f4c1 {name}/")
        else:
            try:
                size = os.path.getsize(full)
                if size < 1024: s = f"{size}B"
                elif size < 1048576: s = f"{size/1024:.1f}K"
                else: s = f"{size/1048576:.1f}M"
                files.append(f"\U0001f4c4 {name}  ({s})")
            except Exception:
                files.append(f"\U0001f4c4 {name}")
    if not dirs and not files:
        send_html(f"<b>{escape_html(os.path.basename(target))}/</b>\n{t('ls.empty')}")
        return
    lines = dirs + files
    total = len(lines)
    if total > 50:
        lines = lines[:50]
        lines.append(t("ls.more", count=total - 50))
    body = "\n".join(escape_html(l) for l in lines)
    send_html(f"<b>{escape_html(target)}</b>\n<pre>{body}</pre>\n<i>{t('ls.summary', dirs=len(dirs), files=len(files))}</i>")
