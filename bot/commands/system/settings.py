"""Settings command with inline keyboard UI."""
import json

from commands import command, callback
from i18n import t
from config import TOKEN_PERIODS, settings, update_config, log
from telegram import escape_html, send_html, tg_api, CHAT_ID


def _save_settings():
    update_config("settings", settings)


def _settings_keyboard():
    token_labels = t("token_labels")
    if not isinstance(token_labels, dict):
        token_labels = {}
    settings_keys = t("settings.keys")
    if not isinstance(settings_keys, list):
        settings_keys = []
    rows = []
    for item in settings_keys:
        key = item["key"]; label = item["label"]
        mark = "ON" if settings.get(key) else "OFF"
        rows.append([{"text": f"[{mark}]  {label}", "callback_data": f"stg:{key}"}])
    cur = settings.get("token_display", "month")
    token_row = []
    for k in TOKEN_PERIODS:
        lbl = token_labels.get(k, k)
        display = f"[{lbl}]" if k == cur else lbl
        token_row.append({"text": display, "callback_data": f"stg:td:{k}"})
    rows.append(token_row)
    rows.append([{"text": t("settings.close"), "callback_data": "stg:close"}])
    return json.dumps({"inline_keyboard": rows})


def _settings_text():
    token_labels = t("token_labels")
    if not isinstance(token_labels, dict):
        token_labels = {}
    settings_keys = t("settings.keys")
    if not isinstance(settings_keys, list):
        settings_keys = []
    lines = []
    for item in settings_keys:
        key = item["key"]; label = item["label"]; desc = item["desc"]
        mark = "ON " if settings.get(key) else "OFF"
        lines.append(f"  <code>[{mark}]</code> <b>{escape_html(label)}</b>\n          <i>{escape_html(desc)}</i>")
    cur = settings.get("token_display", "month")
    period_str = " / ".join(f"<b>{v}</b>" if k == cur else v for k, v in token_labels.items())
    cur_label = token_labels.get(cur, cur)
    lines.append(f"  <code>[{cur_label:^3}]</code> <b>{t('settings.token_range')}</b>\n          <i>{period_str}</i>")
    body = "\n\n".join(lines)
    return f"<b>{t('settings.title')}</b>\n{'━'*25}\n\n{body}\n\n{'━'*25}\n<i>{t('settings.tap_toggle')}</i>"


@command("/settings")
def handle_settings(text):
    from state import state
    # Web Settings UI (via Cloudflare Tunnel) - preferred
    if state.file_viewer_url:
        try:
            from fileviewer import generate_settings_token, _ViewerHandler
            token = generate_settings_token()
            url = f"{state.file_viewer_url}/settings?token={token}"
            result = tg_api("sendMessage", {
                "chat_id": CHAT_ID,
                "text": f'<b>\u2699 Settings</b>\n<a href="{url}">{escape_html(t("settings.open_web"))}</a>',
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            try:
                _ViewerHandler.settings_msg_id = result["result"]["message_id"]
            except Exception:
                pass
            return
        except Exception as e:
            log.warning("Web settings failed, falling back to inline keyboard: %s", e)
    # Fallback: inline keyboard
    tg_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": _settings_text(),
        "parse_mode": "HTML",
        "reply_markup": _settings_keyboard(),
    })


@callback("stg:")
def handle_settings_callback(callback_id, msg_id, data):
    token_labels = t("token_labels")
    if not isinstance(token_labels, dict):
        token_labels = {}
    settings_keys = t("settings.keys")
    if not isinstance(settings_keys, list):
        settings_keys = []
    key = data.split(":", 1)[1]
    if key == "close":
        tg_api("deleteMessage", {"chat_id": CHAT_ID, "message_id": msg_id})
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        return
    if key.startswith("td:"):
        new_period = key.split(":", 1)[1]
        if new_period in TOKEN_PERIODS:
            settings["token_display"] = new_period
            _save_settings()
            tg_api("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": f"{t('settings.token_prefix')}: {token_labels.get(new_period, new_period)}"
            })
    elif key in settings:
        settings[key] = not settings[key]
        _save_settings()
        status = "ON" if settings[key] else "OFF"
        label = next((item["label"] for item in settings_keys if item["key"] == key), key)
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": f"{label}: {status}"})
    else:
        return
    tg_api("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "text": _settings_text(),
        "parse_mode": "HTML",
        "reply_markup": _settings_keyboard(),
    })
