"""
Manages saved configurations per community.
Stored in data/configs/{community}.json
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("configs")

CONFIGS_DIR = Path("./data/configs")

# Thunderstore community identifiers (slugs) only contain letters, digits,
# '.', '_' and '-'. Enforcing that pattern server-side keeps user-supplied
# communities from escaping their directory (path traversal via '/', '..'
# etc.) in the output folders and in the config storage paths.
_COMMUNITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_valid_community(community: str) -> bool:
    """True if the name is a safe community identifier usable in paths/URLs."""
    return (
        isinstance(community, str)
        and bool(community)
        and bool(_COMMUNITY_RE.fullmatch(community))
    )


def _path(community: str) -> Path:
    if not is_valid_community(community):
        raise ValueError(f"Unsafe community name: {community!r}")
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIGS_DIR / f"{community}.json"


def load_config(community: str) -> dict:
    p = _path(community)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else {}
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
            if isinstance(data, dict):
                result.append({"community": f.stem, **data})
        except Exception as exc:
            logger.warning("Unreadable config (%s): %s", f.name, exc)
    return result
