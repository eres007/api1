"""Runtime configuration, read from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    image_key: str = ""
    key_source: str = "none"  # env | file | none
    browser_id: str = ""
    impersonate: str = "chrome131"
    proxy_url: str = ""
    enable_browser_harvest: bool = False
    key_file: Path = field(default_factory=lambda: Path("data/image_key.txt"))
    default_resolution: str = "768x768"
    image_max_wait_s: float = 240.0
    text_max_chars: int = 8000
    cors_origins: list[str] = field(default_factory=lambda: ["*"])

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.environ.get("PERCHANCE_IMAGE_KEY", "").strip()
        s = cls(
            image_key=key,
            key_source="env" if key else "none",
            browser_id=os.environ.get("PERCHANCE_BROWSER_ID", "").strip(),
            impersonate=os.environ.get("IMPERSONATE", "chrome131").strip() or "chrome131",
            proxy_url=os.environ.get("PROXY_URL", "").strip(),
            enable_browser_harvest=_bool("ENABLE_BROWSER_HARVEST"),
            default_resolution=os.environ.get("IMAGE_RESOLUTION", "768x768"),
            text_max_chars=int(os.environ.get("TEXT_MAX_CHARS", "8000")),
        )
        raw = os.environ.get("KEY_FILE", "").strip()
        if raw:
            s.key_file = Path(raw)
        if not s.image_key and s.key_file.exists():
            s.image_key = s.key_file.read_text(encoding="utf-8").strip()
            if s.image_key:
                s.key_source = "file"
        origins = os.environ.get("CORS_ORIGINS", "*").strip()
        s.cors_origins = [o.strip() for o in origins.split(",") if o.strip()] or ["*"]
        return s
