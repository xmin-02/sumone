"""Model command: /model."""
from commands import command
from i18n import t
from config import AI_MODELS, MODEL_ALIASES, resolve_model, update_config, settings
from state import state, switch_provider
from telegram import escape_html, send_html


def _enabled_providers():
    """Return set of enabled provider keys from onboarding settings."""
    enabled = settings.get("enabled_providers")
    if enabled and isinstance(enabled, list):
        return set(enabled)
    return set(AI_MODELS.keys())


def _reverse_sub_alias(provider, model_id):
    """Reverse-lookup sub-model alias (e.g. 'opus') from model ID."""
    info = AI_MODELS.get(provider, {})
    for alias, mid in info.get("sub_models", {}).items():
        if mid == model_id:
            return alias
    return None


def _sync_settings(provider, model_id):
    """Keep settings.default_model / default_sub_model in sync with /model."""
    settings["default_model"] = provider
    sub = _reverse_sub_alias(provider, model_id)
    if sub:
        settings["default_sub_model"] = sub
    update_config("settings", settings)


@command("/model")
def handle_model(text):
    enabled = _enabled_providers()
    args = text.split()[1:]  # everything after /model
    if not args:
        provider_label = AI_MODELS.get(state.provider, {}).get("label", state.provider)
        if state.model:
            current_display = f"{provider_label} - <code>{escape_html(state.model)}</code>"
        else:
            current_display = f"{provider_label} ({t('model.cli_default')})"
        # Build examples from enabled providers only
        ex_prov = []
        ex_full = []
        for pkey in enabled:
            info = AI_MODELS.get(pkey, {})
            ex_prov.append(f"  /model {pkey}")
            subs = info.get("sub_models", {})
            if subs:
                last_alias = list(subs.keys())[-1]
                ex_full.append(f"  /model {pkey} {last_alias}")
        examples = "\n".join(ex_prov + ex_full)
        send_html(
            f"<b>{t('model.current')}:</b> {current_display}\n{'━'*25}\n"
            f"<b>{t('model.usage')}:</b> /model [provider] [model]\n"
            f"<b>{t('model.examples')}:</b>\n"
            f"{examples}\n"
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
        _sync_settings("claude", state.model)
        send_html(f"<b>{t('model.reset_done')}:</b> Claude - <code>{escape_html(state.model)}</code>"); return
    # Block disabled providers
    if name in AI_MODELS and name not in enabled:
        send_html(t("error.provider_disabled", name=f"<code>{name}</code>"))
        return
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
        _sync_settings(name, state.model)
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
        _sync_settings(name, state.model)
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
            # Only show enabled aliases
            all_aliases = set()
            for prov_name in enabled:
                all_aliases.add(prov_name)
                info = AI_MODELS.get(prov_name, {})
                all_aliases.update(info.get("sub_models", {}).keys())
            for alias, mid in MODEL_ALIASES.items():
                for prov_name in enabled:
                    if mid in AI_MODELS.get(prov_name, {}).get("sub_models", {}).values():
                        all_aliases.add(alias)
            aliases = ", ".join(sorted(all_aliases))
            send_html(t("error.unknown_model", name=f"<code>{escape_html(name)}</code>", aliases=escape_html(aliases))); return
    # Block disabled provider even when resolved by alias
    if provider not in enabled:
        send_html(t("error.provider_disabled", name=f"<code>{provider}</code>"))
        return
    switch_provider(provider)
    state.model = resolved
    state._provider_models[state.provider] = state.model
    update_config("model", state.model)
    update_config("provider_models", dict(state._provider_models))
    _sync_settings(provider, state.model)
    provider_label = AI_MODELS.get(provider, {}).get("label", provider)
    send_html(f"<b>{t('model.changed')}:</b> {provider_label} - <code>{escape_html(resolved)}</code>")
