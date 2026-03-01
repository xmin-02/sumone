"""Command registry with decorator-based registration and auto-discovery."""
import importlib
import pkgutil

_handlers = {}   # {"/help": handler_func, ...}
_callbacks = {}  # {"stg:": handler_func, ...}


def command(name, aliases=None):
    """Decorator: register a command handler."""
    def decorator(func):
        _handlers[name] = func
        for a in (aliases or []):
            _handlers[a] = func
        return func
    return decorator


def callback(prefix):
    """Decorator: register a callback query handler."""
    def decorator(func):
        _callbacks[prefix] = func
        return func
    return decorator


def dispatch(text):
    """Match text to a command handler."""
    cmd = text.split()[0].lower()
    return _handlers.get(cmd)


def dispatch_callback(data):
    """Match callback_data to a handler."""
    for prefix, handler in _callbacks.items():
        if data.startswith(prefix):
            return handler
    return None


def _auto_import():
    """Auto-import all command modules from subpackages."""
    import commands as _pkg
    for finder, sub_name, is_pkg in pkgutil.iter_modules(_pkg.__path__):
        if is_pkg:
            sub_pkg = importlib.import_module(f"commands.{sub_name}")
            for _, mod_name, _ in pkgutil.iter_modules(sub_pkg.__path__):
                importlib.import_module(f"commands.{sub_name}.{mod_name}")


_auto_import()
