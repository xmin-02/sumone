"""Internationalization support."""
import json
import os

_strings = {}

def load(lang):
    """Load language pack."""
    global _strings
    path = os.path.join(os.path.dirname(__file__), f"{lang}.json")
    with open(path, encoding="utf-8") as f:
        _strings = json.load(f)

def t(key, **kwargs):
    """Get translated string by dot-separated key. t("error.timeout") -> "시간 초과..." """
    val = _strings
    for part in key.split("."):
        if isinstance(val, dict):
            val = val.get(part)
            if val is None:
                return key
        else:
            return key
    if isinstance(val, str) and kwargs:
        return val.format(**kwargs)
    return val if isinstance(val, (str, list, dict)) else key
