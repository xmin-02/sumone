"""Command registry with decorator-based registration."""

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
