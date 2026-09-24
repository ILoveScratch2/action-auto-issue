"""
Actions Logger
"""

_ESCAPES = (("%", "%25"), ("\r", "%0D"), ("\n", "%0A"))


def escape(value):
    text = str(value)
    for raw, encoded in _ESCAPES:
        text = text.replace(raw, encoded)
    return text


def render(template, **replacements):
    """Substitute ``{key}`` placeholders, mirroring the original action's log templates."""
    text = template
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _command(command, message=""):
    line = f"::{command}::{escape(message)}" if message else f"::{command}::"
    print(line, flush=True)


def info(message):
    print(message, flush=True)


def warning(message):
    _command("warning", message)


def error(message):
    _command("error", message)


def set_failed(message):
    error(message)
