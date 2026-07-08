import os
from pathlib import Path


def _strip_inline_comment(value: str) -> str:
    in_single_quote = False
    in_double_quote = False

    for idx, char in enumerate(value):
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif char == "#" and not in_single_quote and not in_double_quote:
            return value[:idx].rstrip()

    return value.strip()


def load_env_file(env_path: str = ".env") -> bool:
    """Load simple KEY=VALUE pairs from a local .env file if present.

    Existing process environment variables are preserved so users can
    override .env defaults from the shell for one-off runs.
    """
    path = Path(env_path)
    if not path.exists():
        return False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_inline_comment(value.strip())
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        # Preserve shell-provided env vars (e.g. PowerShell $env:KEY=...)
        # so .env acts as a default source instead of a hard override.
        os.environ.setdefault(key, value)

    return True