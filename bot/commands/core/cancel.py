"""Cancel command: /cancel."""
from commands import command
from i18n import t
from config import IS_WINDOWS
from state import state
from telegram import send_html


@command("/cancel")
def handle_cancel(text):
    with state.lock:
        proc = state.ai_proc
        was_busy = state.busy
    if proc and proc.poll() is None:
        if IS_WINDOWS:
            proc.terminate()
        else:
            proc.kill()
        # Don't touch busy/queue — let _run's finally block handle naturally
        send_html(f"<b>{t('cancel.done')}</b> {t('cancel.killed')}")
    elif was_busy:
        # No proc but busy — safety reset (thread may have crashed)
        with state.lock:
            state.busy = False
        send_html(f"<b>{t('cancel.reset')}</b> {t('cancel.cleared')}")
    else:
        send_html(t("cancel.nothing"))
