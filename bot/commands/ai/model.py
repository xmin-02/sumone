"""Model command: /model."""
from commands import command
from i18n import t
from config import AI_MODELS, MODEL_ALIASES, resolve_model, update_config
from state import state, switch_provider
from telegram import escape_html, send_html


@command("/model")
def handle_model(text):
    args = text.split()[1:]  # everything after /model
    if not args:
        provider_label = AI_MODELS.get(state.provider, {}).get("label", state.provider)
        if state.model:
            current_display = f"{provider_label} - <code>{escape_html(state.model)}</code>"
        else:
            current_display = f"{provider_label} ({t('model.cli_default')})"
        send_html(
            f"<b>{t('model.current')}:</b> {current_display}\n{'━'*25}\n"
            f"<b>{t('model.usage')}:</b> /model [provider] [model]\n"
            f"<b>{t('model.examples')}:</b>\n"
            f"  /model claude\n  /model codex\n  /model gemini\n"
            f"  /model claude opus\n  /model codex gpt-5.3-codex\n  /model gemini flash\n"
            f"  /model {t('model.restore_default')}")
        return
    name = args[0].lower()
    reset_kw = t("model.reset_keywords")
    if isinstance(reset_kw, list) and name in reset_kw:
        switch_provider("claude")
        prov_info = AI_MODELS["claude"]
        default_sub = prov_info.get("default", "sonnet")
        state.model = prov_info["sub_models"].get(default_sub)
        state._provider_models[state.provider] = state.model
        update_config("model", state.model)
        update_config("provider_models", dict(state._provider_models))
        send_html(f"<b>{t('model.reset_done')}:</b> Claude - <code>{escape_html(state.model)}</code>"); return
    # Two-part command: /model [provider] [model]
    if name in AI_MODELS and len(args) >= 2:
        model_arg = args[1].lower()
        prov_info = AI_MODELS[name]
        # Try sub_model alias within this provider
        resolved = prov_info.get("sub_models", {}).get(model_arg)
        if resolved:
            switch_provider(name)
            state.model = resolved
        else:
            # Try as raw model name
            switch_provider(name)
            state.model = model_arg
            resolved = model_arg
        state._provider_models[state.provider] = state.model
        label = prov_info.get("label", name.title())
        update_config("model", state.model)
        update_config("provider_models", dict(state._provider_models))
        send_html(f"<b>{t('model.changed')}:</b> {label} - <code>{escape_html(resolved)}</code>"); return
    # Provider-level switch: /model codex, /model gemini, /model claude
    if name in AI_MODELS:
        switch_provider(name)
        prov_info = AI_MODELS[name]
        if not state.model:
            default_sub = prov_info.get("default")
            state.model = prov_info["sub_models"].get(default_sub) if default_sub else None
        state._provider_models[state.provider] = state.model
        label = prov_info.get("label", name.title())
        model_display = escape_html(state.model) if state.model else t('model.cli_default')
        update_config("model", state.model)
        update_config("provider_models", dict(state._provider_models))
        send_html(f"<b>{t('model.changed')}:</b> {label} - <code>{model_display}</code>"); return
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
    switch_provider(provider)
    state.model = resolved
    state._provider_models[state.provider] = state.model
    update_config("model", state.model)
    update_config("provider_models", dict(state._provider_models))
    provider_label = AI_MODELS.get(provider, {}).get("label", provider)
    send_html(f"<b>{t('model.changed')}:</b> {provider_label} - <code>{escape_html(resolved)}</code>")
