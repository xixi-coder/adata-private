import os
from pathlib import Path
from typing import Iterable


def _candidate_roots() -> Iterable[Path]:
    current = Path(__file__).resolve()
    for path in [current] + list(current.parents):
        if (path / ".git").exists():
            yield path
    yield current.parents[2]


def load_local_env() -> bool:
    for root in _candidate_roots():
        env_path = root / ".env.local"
        if not env_path.exists():
            continue

        loaded = False
        with env_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded = True
        return loaded
    return False
