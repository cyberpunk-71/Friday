"""Central configuration: YAML defaults + .env + live settings table merge.

Live settings (DB `settings` table) always win; the Admin panel edits them and
the ⟨CTRL⟩ config_deltas write them. Every value is therefore live-tunable.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(root: Path) -> None:
    """Tiny .env loader (no external dep). VM systemd also uses EnvironmentFile."""
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _load_yaml(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


class Config:
    """Lazy-loaded singleton config. `cfg.get(...)` supports dotted keys."""

    _instance: "Config | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        _load_dotenv(_ROOT)
        self.root = _ROOT
        self.data_dir = Path(os.environ.get("FRIDAY_DATA_DIR", _ROOT / "data"))
        self.genome_dir = Path(os.environ.get("FRIDAY_GENOME_DIR", _ROOT / "genome"))
        self._defaults = _load_yaml(_ROOT / "configs" / "defaults.yaml")
        self._env = dict(os.environ)
        # live overrides loaded from DB later by app bootstrap
        self._live: dict = {}

    @classmethod
    def instance(cls) -> "Config":
        with cls._lock:
            if cls._instance is None:
                cls._instance = Config()
            return cls._instance

    def set_live(self, live: dict) -> None:
        self._live = live

    def get(self, dotted: str, default=None):
        node = self._defaults
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def env(self, key: str, default=None):
        return self._env.get(key, default)

    def data_path(self, *parts: str) -> Path:
        p = self.data_dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def genome_path(self, *parts: str) -> Path:
        p = self.genome_dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ---- live setting helpers (Admin panel + ⟨CTRL⟩ deltas) ----
    def live(self, key: str, default=None):
        return self._live.get(key, default)

    def live_or(self, dotted: str, default=None):
        """dotted default-config key with live override by the same dotted key."""
        return self._live.get(dotted, self.get(dotted, default))

    def set_live_value(self, key: str, value) -> None:
        self._live[key] = value

    def reset_for_tests(self, data_dir: str, genome_dir: str | None = None) -> None:
        """Test hook: repoint data/genome dirs on the singleton."""
        self.data_dir = Path(data_dir)
        if genome_dir:
            self.genome_dir = Path(genome_dir)
        self._live = {}


cfg = Config.instance()
