"""Basic commands: /help, /status, /cost, /model, /cancel, /restart_bot."""
import os
import platform
import sys

from commands import command
from i18n import t
from config import IS_WINDOWS, AI_MODELS, MODEL_ALIASES, resolve_model, settings, log
from state import state
from telegram import escape_html, send_html, CHAT_ID
from tokens import get_global_usage


@command("/help", aliases=["/start"])
def handle_help(text):
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else t("status.no_session").split("(")[0].strip()
    model_info = escape_html(state.model) if state.model else t("model.default_name")
    msg = (
        f"<b>{t('help.title')}</b>\n" + '━'*25 + "\n\n"
        f"<b>{t('help.usage_title')}</b>\n{t('help.usage_body')}\n\n"
        f"<b>{t('help.session_title')}</b>\n{t('help.session_body')}\n\n"
        f"<b>{t('help.model_title')}</b>\n{t('help.model_body')}\n\n"
        f"<b>{t('help.commands_title')}</b>\n{t('help.commands_body')}\n\n"
        f"<b>{t('help.examples_title')}</b>\n<code>{t('help.examples')}</code>\n\n"
        + '━'*25 + f"\n{t('status.session')}: {session_info} | {t('status.model_label')}: <code>{model_info}</code>\n"
    )
    send_html(msg)


@command("/status")
def handle_status(text):
    session_info = f"<code>{state.session_id[:8]}</code>" if state.session_id else t("status.no_session")
    model_info = f"<code>{escape_html(state.model)}</code>" if state.model else t("model.default_name")
    busy_info = t("status.processing") if state.busy else t("status.idle")
    os_info = f"Windows ({platform.version()})" if IS_WINDOWS else platform.platform()
    msg = (f"<b>{t('status.title')}</b>\n{'━'*25}\n"
           f"{t('status.session')}: {session_info}\n{t('status.model_label')}: {model_info}\n"
           f"{t('status.state')}: {busy_info}\n{t('status.os')}: {escape_html(os_info)}\n")
    send_html(msg)


@command("/cost")
def handle_cost(text):
    msg = (f"<b>{t('cost.title')}</b>\n{'━'*25}\n"
           f"{t('cost.last')}: ${state.last_cost:.4f}\n"
           f"{t('cost.session_total')}: ${state.total_cost:.4f}\n")
    # Provider breakdown (current bot session)
    provider_lines = []
    for prov, stats in state.provider_stats.items():
        if stats["tokens_in"] == 0 and stats["tokens_out"] == 0:
            continue
        label = AI_MODELS.get(prov, {}).get("label", prov.title())
        total_tok = stats["tokens_in"] + stats["tokens_out"]
        cost_str = f"${stats['cost']:.4f}" if stats["cost"] > 0 else "\u2014"
        provider_lines.append(f"  {label}: {cost_str} | {total_tok:,} tokens")
    if provider_lines:
        msg += f"\n<b>{t('cost.provider_title')}</b>\n{'━'*25}\n"
        msg += "\n".join(provider_lines) + "\n"
    if settings["show_global_cost"]:
        try:
            g_cost, g_in, g_out, g_sessions = get_global_usage()
            msg += (f"\n<b>{t('cost.global_title')}</b>\n{'━'*25}\n"
                    f"{t('cost.total_cost')}: ${g_cost:.4f}\n"
                    f"{t('cost.total_sessions')}: {g_sessions}\n"
                    f"{t('cost.input_tokens')}: {g_in:,}\n"
                    f"{t('cost.output_tokens')}: {g_out:,}\n"
                    f"{t('cost.total_tokens')}: {g_in + g_out:,}\n")
        except Exception:
            pass
    send_html(msg)


@command("/model")
def handle_model(text):
    args = text.split()[1:]  # everything after /model
    if not args:
        provider_label = AI_MODELS.get(state.provider, {}).get("label", state.provider)
        if state.model:
            current_display = f"{provider_label} - <code>{escape_html(state.model)}</code>"
        else:
            current_display = f"{provider_label} ({t('model.cli_default')})"
        # Collect all aliases
        all_aliases = set(MODEL_ALIASES.keys())
        for prov_name in AI_MODELS:
            all_aliases.add(prov_name)
        for info in AI_MODELS.values():
            all_aliases.update(info.get("sub_models", {}).keys())
        aliases = ", ".join(sorted(all_aliases))
        send_html(
            f"<b>{t('model.current')}:</b> {current_display}\n{'━'*25}\n"
            f"<b>{t('model.usage')}:</b> /model [name] or /model [provider] [model]\n"
            f"<b>{t('model.aliases')}:</b> {escape_html(aliases)}\n"
            f"<b>{t('model.examples')}:</b>\n"
            f"  /model claude\n  /model codex\n  /model gemini\n"
            f"  /model claude opus\n  /model codex gpt-5.3-codex\n  /model gemini flash\n"
            f"  /model {t('model.restore_default')}")
        return
    name = args[0].lower()
    reset_kw = t("model.reset_keywords")
    if isinstance(reset_kw, list) and name in reset_kw:
        state.model = None
        state.provider = "claude"
        send_html(f"<b>{t('model.reset_done')}:</b> {t('model.reset_to')}"); return
    # Two-part command: /model [provider] [model]
    if name in AI_MODELS and len(args) >= 2:
        model_arg = args[1].lower()
        prov_info = AI_MODELS[name]
        # Try sub_model alias within this provider
        resolved = prov_info.get("sub_models", {}).get(model_arg)
        if resolved:
            state.provider = name
            state.model = resolved
        else:
            # Try as raw model name
            state.provider = name
            state.model = model_arg
            resolved = model_arg
        label = prov_info.get("label", name.title())
        send_html(f"<b>{t('model.changed')}:</b> {label} - <code>{escape_html(resolved)}</code>"); return
    # Provider-level switch: /model codex, /model gemini, /model claude
    if name in AI_MODELS:
        state.provider = name
        state.model = None
        label = AI_MODELS[name].get("label", name.title())
        send_html(f"<b>{t('model.changed')}:</b> {label} ({t('model.cli_default')})"); return
    # Resolve across all providers
    resolved, provider = resolve_model(name)
    if not resolved:
        if name.startswith("claude-"):
            resolved, provider = name, "claude"
        elif name.startswith(("gpt-", "o3", "o4")):
            resolved, provider = name, "codex"
        elif name.startswith("gemini-"):
            resolved, provider = name, "gemini"
        else:
            all_aliases = set(MODEL_ALIASES.keys())
            for prov_name in AI_MODELS:
                all_aliases.add(prov_name)
            for info in AI_MODELS.values():
                all_aliases.update(info.get("sub_models", {}).keys())
            aliases = ", ".join(sorted(all_aliases))
            send_html(t("error.unknown_model", name=f"<code>{escape_html(name)}</code>", aliases=escape_html(aliases))); return
    state.model = resolved
    state.provider = provider
    provider_label = AI_MODELS.get(provider, {}).get("label", provider)
    send_html(f"<b>{t('model.changed')}:</b> {provider_label} - <code>{escape_html(resolved)}</code>")


@command("/cancel")
def handle_cancel(text):
    with state.lock: proc = state.ai_proc; was_busy = state.busy
    if proc and proc.poll() is None:
        if IS_WINDOWS:
            proc.terminate()
        else:
            proc.kill()
        with state.lock: state.ai_proc = None; state.busy = False
        send_html(f"<b>{t('cancel.done')}</b> {t('cancel.killed')}")
    elif was_busy:
        with state.lock: state.busy = False
        send_html(f"<b>{t('cancel.reset')}</b> {t('cancel.cleared')}")
    else:
        send_html(t("cancel.nothing"))


@command("/restart_bot")
def handle_restart_bot(text):
    from telegram import tg_api
    send_html(f"<b>{t('restart.shutting_down')}</b>")
    log.info("Restart requested via /restart_bot")

    # Flush pending Telegram updates so /restart_bot won't be re-processed
    try:
        result = tg_api("getUpdates", {"timeout": 0})
        if result and result.get("ok"):
            updates = result.get("result", [])
            if updates:
                max_id = max(u["update_id"] for u in updates)
                tg_api("getUpdates", {"offset": max_id + 1, "timeout": 0})
                log.info("Flushed Telegram updates up to %d", max_id)
    except Exception as e:
        log.warning("Failed to flush updates: %s", e)

    # Stop file viewer
    try:
        from main import _stop_file_viewer
        _stop_file_viewer()
    except Exception as e:
        log.warning("Failed to stop file viewer: %s", e)

    # Kill AI process if running
    with state.lock:
        proc = state.ai_proc
        if proc and proc.poll() is None:
            try:
                if IS_WINDOWS:
                    proc.terminate()
                else:
                    proc.kill()
            except Exception:
                pass
            state.ai_proc = None
        state.busy = False

    # Re-exec the current process (replaces current process image)
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    if not os.path.isfile(main_py):
        main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    log.info("Re-executing: %s %s", sys.executable, main_py)
    os.execv(sys.executable, [sys.executable, main_py])
