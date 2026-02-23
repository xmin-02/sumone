"""Token aggregation: /total_tokens with remote PC management."""
import json
import re
import threading
import time

from commands import command, callback
from i18n import t
import config
from config import BOT_TOKEN, CHAT_ID, log
from state import state
from telegram import escape_html, send_html, tg_api, tg_api_raw, delete_msg, schedule_auto_dismiss, reset_auto_dismiss, cancel_auto_dismiss
from tokens import publish_token_data, compute_all_period_tokens, fetch_remote_tokens, get_remote_bot_info


_submenu_msg_ids = []


def _clear_submenus():
    """Delete all tracked sub-menu messages."""
    for mid in _submenu_msg_ids:
        cancel_auto_dismiss(mid)
        delete_msg(mid)
    _submenu_msg_ids.clear()


def _track_submenu(msg_id):
    """Track a sub-menu message for later cleanup."""
    if msg_id:
        _submenu_msg_ids.append(msg_id)


def _save_remote_bots():
    config.update_config("remote_bots", config.REMOTE_BOTS)


@command("/total_tokens", aliases=["/totaltokens"])
def handle_total_tokens(text):
    my_info = tg_api_raw(BOT_TOKEN, "getMe")
    my_name = ""
    if my_info and my_info.get("ok"):
        my_name = f"@{my_info['result'].get('username', 'unknown')}"
    remote_count = len(config.REMOTE_BOTS)
    if remote_count > 0:
        remote_info = f"\n{t('total_tokens.connected_pcs')}: {t('total_tokens.connected_count', count=remote_count)}"
    else:
        remote_info = f"\n{t('total_tokens.connected_pcs')}: {t('total_tokens.connected_none')}"
    msg = (f"<b>{t('total_tokens.title')}</b>\n{'━'*25}\n"
           f"{t('total_tokens.current_bot')}: <code>{escape_html(my_name)}</code>"
           f"{remote_info}\n{'━'*25}")
    buttons = [
        [{"text": t("total_tokens.btn_aggregate"), "callback_data": "tt:aggregate"}],
        [{"text": t("total_tokens.btn_connect"), "callback_data": "tt:connect"},
         {"text": t("total_tokens.btn_manage"), "callback_data": "tt:manage"}],
        [{"text": t("total_tokens.btn_close"), "callback_data": "tt:close"}],
    ]
    result = tg_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": buttons}),
    })
    if result and result.get("ok"):
        schedule_auto_dismiss(result["result"]["message_id"])


def _handle_aggregate():
    _track_submenu(send_html(f"<i>{t('total_tokens.aggregating')}</i>"))
    publish_token_data()
    local_data = compute_all_period_tokens()
    my_info = tg_api_raw(BOT_TOKEN, "getMe")
    my_name = f"@{my_info['result'].get('username', '')}" if my_info and my_info.get("ok") else t("total_tokens.this_pc")
    period_labels = t("total_tokens.period_labels")
    if not isinstance(period_labels, dict):
        period_labels = {"d": "d", "m": "m", "y": "y", "t": "t"}
    lines = [f"<b>{t('total_tokens.aggregate_title')}</b>\n{'━'*25}"]
    lines.append(f"\n<b>{escape_html(my_name)}</b> ({t('total_tokens.this_pc')})")
    for p, label in period_labels.items():
        lines.append(f"  {label}: {local_data.get(p, 0):,}")
    lines.append(f"  {t('total_tokens.sessions_count', count=local_data.get('s', 0))}")
    totals = {p: local_data.get(p, 0) for p in period_labels}
    total_sessions = local_data.get("s", 0)
    for bot in config.REMOTE_BOTS:
        token = bot.get("token", "")
        name = bot.get("username", bot.get("name", t("total_tokens.unknown")))
        remote_data = fetch_remote_tokens(token)
        if remote_data:
            lines.append(f"\n<b>@{escape_html(name)}</b>")
            for p, label in period_labels.items():
                val = remote_data.get(p, 0)
                lines.append(f"  {label}: {val:,}")
                totals[p] += val
            rs = remote_data.get("s", 0)
            lines.append(f"  {t('total_tokens.sessions_count', count=rs)}")
            total_sessions += rs
            ts = remote_data.get("ts", 0)
            if ts:
                updated = time.strftime("%m/%d %H:%M", time.localtime(ts))
                lines.append(f"  <i>{t('total_tokens.last_updated', time=updated)}</i>")
        else:
            lines.append(f"\n<b>@{escape_html(name)}</b>")
            lines.append(f"  <i>{t('total_tokens.no_data')}</i>")
    if config.REMOTE_BOTS:
        lines.append(f"\n{'━'*25}\n<b>{t('total_tokens.total')}</b>")
        for p, label in period_labels.items():
            lines.append(f"  {label}: {totals[p]:,}")
        lines.append(f"  {t('total_tokens.sessions_count', count=total_sessions)}")
    _track_submenu(send_html("\n".join(lines)))


def _handle_connect():
    state.waiting_token_input = True
    prompt_id = send_html(
        f"<b>{t('total_tokens.connect_title')}</b>\n{'━'*25}\n"
        f"{t('total_tokens.connect_prompt')}\n\n"
        f"<i>{t('total_tokens.connect_cancel')}</i>")
    state.connect_prompt_msg_id = prompt_id
    _track_submenu(prompt_id)


def handle_token_input(text, user_msg_id=None):
    """Process bot token input for remote PC connection. Called from main."""
    state.waiting_token_input = False
    # Delete user's message containing the token for security
    if user_msg_id:
        delete_msg(user_msg_id)
    # Delete the connect prompt message
    if state.connect_prompt_msg_id:
        delete_msg(state.connect_prompt_msg_id)
        state.connect_prompt_msg_id = None
    token = text.strip()
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        send_html(f"<b>{t('error.invalid_token')}</b>")
        return
    for bot in config.REMOTE_BOTS:
        if bot.get("token") == token:
            send_html(t("error.already_connected", name=bot.get('username', '')))
            return
    if token == BOT_TOKEN:
        send_html(f"<b>{t('error.self_token')}</b>")
        return
    info = get_remote_bot_info(token)
    if not info:
        send_html(f"<b>{t('error.invalid_token_verify')}</b>")
        return
    new_bot = {"token": token, "name": info["name"], "username": info["username"]}
    config.REMOTE_BOTS.append(new_bot)
    _save_remote_bots()
    _track_submenu(send_html(
        f"<b>{t('total_tokens.connect_done')}</b>\n{'━'*25}\n"
        f"{t('total_tokens.bot_name')}: {escape_html(info['name'])}\n"
        f"{t('total_tokens.username')}: @{escape_html(info['username'])}\n"
        f"{'━'*25}\n"
        f"<i>{t('total_tokens.connect_note')}</i>"))
    log.info("Remote bot connected: @%s", info["username"])


def _handle_manage():
    if not config.REMOTE_BOTS:
        send_html(f"<b>{t('total_tokens.manage_empty')}</b>")
        return
    lines = [f"<b>{t('total_tokens.manage_title')}</b>\n{'━'*25}"]
    buttons = []
    for i, bot in enumerate(config.REMOTE_BOTS):
        name = bot.get("username", bot.get("name", t("total_tokens.unknown")))
        lines.append(f"  <b>{i+1}.</b> @{escape_html(name)}")
        buttons.append([{"text": f"{i+1}. @{name} {t('total_tokens.delete_label')}", "callback_data": f"tt:del:{i}"}])
    buttons.append([{"text": t("total_tokens.btn_close"), "callback_data": "tt:close"}])
    result = tg_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": buttons}),
    })
    if result and result.get("ok"):
        mid = result["result"]["message_id"]
        schedule_auto_dismiss(mid)
        _track_submenu(mid)


def _handle_delete_remote(index):
    if 0 <= index < len(config.REMOTE_BOTS):
        removed = config.REMOTE_BOTS.pop(index)
        _save_remote_bots()
        name = removed.get("username", removed.get("name", ""))
        return t("total_tokens.disconnected", name=name)
    return ""


@callback("tt:")
def handle_total_tokens_callback(callback_id, msg_id, data):
    action = data.split(":", 1)[1] if ":" in data else ""
    if action == "close":
        _clear_submenus()
        cancel_auto_dismiss(msg_id)
        tg_api("deleteMessage", {"chat_id": CHAT_ID, "message_id": msg_id})
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        return
    if action == "aggregate":
        _clear_submenus()
        reset_auto_dismiss(msg_id)
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": t("total_tokens.aggregating")})
        threading.Thread(target=_handle_aggregate, daemon=True).start()
        return
    if action == "connect":
        _clear_submenus()
        reset_auto_dismiss(msg_id)
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        _handle_connect()
        return
    if action == "manage":
        _clear_submenus()
        reset_auto_dismiss(msg_id)
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        _handle_manage()
        return
    if action.startswith("del:"):
        try:
            _clear_submenus()
            index = int(action.split(":")[1])
            result_text = _handle_delete_remote(index)
            tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": result_text})
            _handle_manage()
        except (ValueError, IndexError):
            tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
        return
    tg_api("answerCallbackQuery", {"callback_query_id": callback_id})
