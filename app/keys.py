"""Image-gen userKey store.

The image origin enforces a Turnstile-verified `userKey`; the text origin does
not. Two ways to get one:

1. `PERCHANCE_IMAGE_KEY` env var (or `data/image_key.txt`) — harvest locally with
   `python -m app.harvest --out data/image_key.txt` on a machine that has a real
   display, then paste/deploy. Recommended: Render's free instances are too small
   to reliably run Chromium.
2. `ENABLE_BROWSER_HARVEST=1` — attempts on-server harvesting. Needs Playwright +
   a display (Xvfb on Linux). Opt-in because of the memory cost.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("keys")


class KeyStore:
    def __init__(self, key_file: Path, initial: str = "", auto_harvest: bool = False) -> None:
        self._file = Path(key_file)
        self._lock = threading.Lock()
        self._key = initial or self._read_file()
        self._verified_at: Optional[float] = None
        self._auto = auto_harvest
        self._worker: Optional[threading.Thread] = None

    # -- storage ------------------------------------------------------
    def _read_file(self) -> str:
        try:
            return self._file.read_text(encoding="utf-8").strip() if self._file.exists() else ""
        except OSError:
            return ""

    def get(self) -> str:
        with self._lock:
            return self._key

    def set(self, key: str) -> None:
        key = (key or "").strip()
        if not key:
            return
        with self._lock:
            self._key = key
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                self._file.write_text(key, encoding="utf-8")
                self._verified_at = time.time()
            except OSError as exc:
                log.warning("could not persist key: %s", exc)

    def mark_stale(self) -> None:
        with self._lock:
            self._verified_at = None

    # -- lifecycle ----------------------------------------------------
    def start_background(self, interval_s: int = 1800) -> None:
        if not self._auto or self._worker:
            return
        self._worker = threading.Thread(
            target=self._loop, args=(interval_s,), daemon=True, name="key-harvester"
        )
        self._worker.start()
        log.info("background key harvester started (every %ss)", interval_s)

    def _loop(self, interval_s: int) -> None:
        while True:
            try:
                self.harvest_once()
            except Exception as exc:  # noqa: BLE001 - background thread
                log.warning("harvest failed: %s", exc)
            time.sleep(interval_s)

    @staticmethod
    def harvest_once() -> Optional[str]:
        try:
            from .harvest import harvest_image_key
        except Exception as exc:  # noqa: BLE001
            log.warning("browser harvest unavailable: %s", exc)
            return None
        return harvest_image_key()


__all__ = ["KeyStore"]
