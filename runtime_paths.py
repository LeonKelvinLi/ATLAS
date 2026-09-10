"""Filesystem locations for a process; importing this module creates nothing.

Set ATLAS_* paths in the process environment before starting Python. They are
deliberately not read from .env: finding that file must not depend on itself.
Relative overrides are rejected so a service's working directory cannot
silently select a different database. Defaults preserve existing checkouts.
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PATH_ENV_KEYS = frozenset({
    "ATLAS_STATE_DIR", "ATLAS_MODEL_DIR", "ATLAS_ENV_FILE", "ATLAS_BACKUP_DIR",
})


def configured_path(name: str, default: Path | None = None) -> Path | None:
    value = os.environ.get(name)
    if value is None:
        return default
    if not value.strip():
        raise ValueError(f"{name} must be an absolute path, not an empty value")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path.resolve()


STATE_DIR = configured_path("ATLAS_STATE_DIR", PROJECT_DIR)
LOGS_DIR = STATE_DIR / "logs"
MODELS_DIR = configured_path("ATLAS_MODEL_DIR", STATE_DIR / "models")
ENV_FILE = configured_path("ATLAS_ENV_FILE", STATE_DIR / ".env")
BACKUP_DIR = configured_path("ATLAS_BACKUP_DIR")


def load_env_file(path: Path) -> None:
    """Load non-path settings without changing a future child's locations.

    Process values and the first occurrence of a file key win. Path selectors
    in .env are ignored: constants have already been resolved, so exporting
    those keys now would give child processes a different configuration.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in PATH_ENV_KEYS:
            os.environ.setdefault(key, value.strip())


if __name__ == "__main__":
    import json

    print(json.dumps({
        "code": str(PROJECT_DIR), "state": str(STATE_DIR),
        "logs": str(LOGS_DIR), "models": str(MODELS_DIR),
        "env_file": str(ENV_FILE),
        "backup": str(BACKUP_DIR) if BACKUP_DIR is not None else None,
    }, indent=2))
