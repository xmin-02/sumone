"""Bot configuration and constants."""
import json
import logging
import os
import platform

IS_WINDOWS = platform.system() == "Windows"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

_config = load_config()
BOT_TOKEN = _config["bot_token"]
CHAT_ID = str(_config["chat_id"])
WORK_DIR = _config.get("work_dir", os.path.expanduser("~"))
LANG = _config.get("lang", "ko")
GITHUB_REPO = _config.get("github_repo", "xmin-02/sumone")
REMOTE_BOTS = _config.get("remote_bots", [])

DEFAULT_SETTINGS = {
    "show_cost": False,
    "show_status": True,
    "show_global_cost": True,
    "token_display": "month",
    "show_remote_tokens": True,
    "theme": "system",
    "snapshot_ttl_days": 7,
    "token_ttl": "session",
    "default_model": "claude",
    "default_sub_model": "sonnet",
    "auto_viewer_link": True,
    "viewer_link_fixed": False,
    "show_typing": True,
    "settings_timeout_minutes": 15,
}
TOKEN_PERIODS = ["none", "session", "day", "month", "year", "total"]
TOKEN_TTL_OPTIONS = ["session", "unlimited"]  # + integer 1-60 (minutes)
THEME_OPTIONS = ["system", "dark", "light"]
AI_MODELS = {
    "claude": {
        "label": "Claude",
        "cli_cmd": "claude",
        "install_cmd": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
        "auth_cmd": ["claude"],
        "default": "sonnet",
        "sub_models": {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6",
        },
    },
    "codex": {
        "label": "Codex",
        "cli_cmd": "codex",
        "install_cmd": ["brew", "install", "codex"],
        "auth_cmd": ["codex", "login", "--device-auth"],
        "default": "codex",
        "sub_models": {
            "codex": "gpt-5.3-codex",
            "codex-max": "gpt-5.1-codex-max",
            "codex-mini": "gpt-5.1-codex-mini",
        },
    },
    "gemini": {
        "label": "Gemini",
        "cli_cmd": "gemini",
        "install_cmd": ["brew", "install", "gemini-cli"],
        "auth_cmd": ["gemini", "-p", "hello"],
        "default": "flash",
        "sub_models": {
            "flash": "gemini-2.5-flash",
            "pro": "gemini-2.5-pro",
        },
    },
}
settings = {**DEFAULT_SETTINGS, **_config.get("settings", {})}

MAX_MSG_LEN = 3900
MAX_PARTS = 20
POLL_TIMEOUT = 30

MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "o4": "claude-opus-4-6",
    "s4": "claude-sonnet-4-6",
    "h4": "claude-haiku-4-5-20251001",
}


def resolve_model(name):
    """Resolve model alias to (model_id, provider) or (None, None).

    Searches all providers' sub_models and MODEL_ALIASES.
    """
    name_lower = name.lower()
    # Check sub_models across all providers
    for prov, info in AI_MODELS.items():
        for alias, model_id in info.get("sub_models", {}).items():
            if alias == name_lower or model_id.lower() == name_lower:
                return model_id, prov
    # Check legacy aliases
    resolved = MODEL_ALIASES.get(name_lower)
    if resolved:
        # Find which provider owns this model
        for prov, info in AI_MODELS.items():
            if resolved in info.get("sub_models", {}).values():
                return resolved, prov
        return resolved, "claude"
    return None, None

LOG_FILE = os.path.join(SCRIPT_DIR, "bot.log")
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
)
log = logging.getLogger("tg-bot")


def update_config(key, value):
    """Unified config.json updater."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg[key] = value
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        log.warning("Failed to update config key=%s: %s", key, e)
