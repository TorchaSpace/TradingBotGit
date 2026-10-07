""".env reading / writing that keeps the comments and order of the file (used by the desktop app)."""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

from .config import PROJECT_ROOT

ENV_PATH = PROJECT_ROOT / ".env"
EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_env(path: Path | None = None) -> dict[str, str]:
    path = Path(path or ENV_PATH)
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def _quote(v: str) -> str:
    v = str(v)
    if v == "" or re.fullmatch(r"[A-Za-z0-9_./:,+\-@]*", v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(updates: dict[str, str], path: Path | None = None) -> None:
    """Set the given keys. Existing lines are edited in place, new keys are appended.
    The file is created from .env.example the first time and kept private (chmod 600)."""
    path = Path(path or ENV_PATH)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif EXAMPLE_PATH.exists():
        lines = EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    todo = dict(updates)
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) in todo:
            k = m.group(1)
            lines[i] = f"{k}={_quote(todo.pop(k))}"
    if todo:
        lines.append("")
        lines.append("# ---- uygulama tarafından eklendi ----")
        lines += [f"{k}={_quote(v)}" for k, v in todo.items()]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
