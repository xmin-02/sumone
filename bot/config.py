"""Bot configuration and constants."""
import json
import logging
import os
import platform
import shutil

IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Directory structure (DDD)
# ---------------------------------------------------------------------------
ROOT_DIR = os.path.expanduser("~/.sumone")
BOT_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_DIR = os.path.join(ROOT_DIR, "config")
DATA_DIR = os.path.join(ROOT_DIR, "data")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
BIN_DIR = os.path.join(ROOT_DIR, "bin")

# Create directory structure on first run
for _d in [CONFIG_DIR, DATA_DIR, LOG_DIR, BIN_DIR,
           os.path.join(DATA_DIR, "sessions"),
           os.path.join(DATA_DIR, "downloads"),
           os.path.join(DATA_DIR, "snapshots")]:
    os.makedirs(_d, exist_ok=True)

# ---------------------------------------------------------------------------
# Config file (with migration from old locations)
# ---------------------------------------------------------------------------
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
if not os.path.isfile(CONFIG_FILE):
    _OLD_CONFIG_PATHS = [
        os.path.join(BOT_DIR, "config.json"),
        os.path.expanduser("~/.claude-telegram-bot/config.json"),
    ]
    for _old in _OLD_CONFIG_PATHS:
        if os.path.isfile(_old):
            shutil.copy2(_old, CONFIG_FILE)
            break

# Backward compat alias
SCRIPT_DIR = BOT_DIR


def load_config():
    if not os.path.isfile(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

_config = load_config()
BOT_TOKEN = _config.get("bot_token", "")
CHAT_ID = str(_config.get("chat_id", ""))
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
        "auth_cmd": ["claude", "auth", "login"],
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
    # Claude
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "o4": "claude-opus-4-6",
    "s4": "claude-sonnet-4-6",
    "h4": "claude-haiku-4-5-20251001",
    # Codex
    "gpt-codex": "gpt-5.3-codex",
    "codex-max": "gpt-5.1-codex-max",
    "codex-mini": "gpt-5.1-codex-mini",
    # Gemini
    "flash": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
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
        return None, None
    return None, None

LOG_FILE = os.path.join(LOG_DIR, "bot.log")
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
