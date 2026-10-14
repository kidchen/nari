"""Load settings.yaml, cards.yaml and environment secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("NARI_CONFIG_DIR", ROOT / "config"))
DATA_DIR = Path(os.environ.get("NARI_DATA_DIR", ROOT / "data"))


@dataclass(frozen=True)
class Config:
    settings: dict[str, Any]
    cards: dict[str, Any]
    telegram_token: str
    invite_code: str
    gemini_api_key: str
    gmail_address: str = ""
    gmail_app_password: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.get("timezone", "America/Los_Angeles"))

    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        return self.settings["profiles"]

    @property
    def primary_profile(self) -> str:
        for pid, p in self.profiles.items():
            if p.get("primary"):
                return pid
        return next(iter(self.profiles))

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "nari.db"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Dicts merge recursively; lists/scalars in `override` replace; a None value deletes the key.

    A dict containing `_replace: true` replaces the base dict wholesale instead of merging.
    """
    out = dict(base)
    for k, v in override.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and v.get("_replace"):
            out[k] = {kk: vv for kk, vv in v.items() if kk != "_replace"}
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_layered(name: str) -> dict[str, Any]:
    """config/<name>.yaml (public, committed) overlaid with config/<name>.local.yaml (private, git-ignored)."""
    return deep_merge(_load_yaml(CONFIG_DIR / f"{name}.yaml"), _load_yaml(CONFIG_DIR / f"{name}.local.yaml"))


def load_config(require_secrets: bool = True) -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    invite = os.environ.get("BOT_INVITE_CODE", "")
    if require_secrets and (not token or not invite):
        raise SystemExit("TELEGRAM_BOT_TOKEN and BOT_INVITE_CODE must be set (see .env.example)")
    return Config(
        settings=load_layered("settings"),
        cards=load_layered("cards").get("cards", {}),
        telegram_token=token,
        invite_code=invite,
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gmail_address=os.environ.get("GMAIL_ADDRESS", ""),
        gmail_app_password=os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", ""),
    )
