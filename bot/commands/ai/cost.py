"""Cost command: /cost."""
from commands import command
from i18n import t
from config import AI_MODELS, settings
from state import state
from telegram import escape_html, send_html
from tokens import get_global_usage


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
