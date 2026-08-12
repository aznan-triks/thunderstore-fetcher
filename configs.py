"""
Manages saved configurations per community.
Stored in data/configs/{community}.json
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger("configs")

CONFIGS_DIR = Path("./data/configs")


def _path(community: str) -> Path:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIGS_DIR / f"{community}.json"


def load_config(community: str) -> dict:
    p = _path(community)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception as exc:
            # Corrupted config: don't lose it silently, report it.
            logger.warning("Unreadable config for %s: %s", community, exc)
    return {}


def save_config(community: str, config: dict) -> None:
    # Atomic write: write to a .tmp file then replace, so we never leave a
    # half-written config file behind in case of a crash.
    target = _path(community)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2))
    tmp.replace(target)


def list_configs() -> list[dict]:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for f in CONFIGS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            result.append({"community": f.stem, **data})
        except Exception as exc:
            logger.warning("Unreadable config (%s): %s", f.name, exc)
    return result
