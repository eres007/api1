"""Reversed Perchance API client.

Reverse-engineered 2026-10-09 from:
  https://perchance.org/generators
  https://perchance.org/ai-text-to-image-generator
  https://perchance.org/ai-chat

Documented behaviour worth remembering:
  * perchance.org sits behind Cloudflare and challenges plain-httpx TLS
    fingerprints (403 "Just a moment..."). curl_cffi browser impersonation
    passes, which is why this module uses it rather than httpx.
  * /api/generate on text-generation does NOT gate on browserId/userKey *values*
    — but it does validate their shape: browserId must be 32 hex chars or you
    get {"status":"invalid_browser_id"}. A random 32-hex browserId with no key
    streams completions normally. instructionTokenCount / startWithTokenCount
    are mandatory in the body (else invalid_data).
  * /api/generate on image-generation DOES gate on userKey -> "invalid_key".
  * requestId is optional server-side for text; for image it is how the client
    de-dups retries while the server answers "waiting_for_prev_request_to_finish".
  * The first plain space in `instruction` is rewritten to NBSP (U+00A0).
  * Upstream "stream" is a line protocol, not SSE: `t:"chunk"` for text,
    `data:{...}` for control events.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, AsyncIterator, Optional, Sequence

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

PLATFORM = "https://perchance.org"
TEXT_ORIGIN = "https://text-generation.perchance.org"
IMAGE_ORIGIN = "https://image-generation.perchance.org"

IMPERSONATE = "chrome131"
JSON_HEADERS = {"Content-Type": "application/json"}
TIMEOUT = 300.0

NBSP = " "


class PerchanceError(RuntimeError):
    """Upstream returned something we could not use."""

    def __init__(self, message: str, status: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _hex(n: int) -> str:
    """n random hex chars (no 0x prefix)."""
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def make_request_id() -> str:
    """Image-gen requestId format: repr of a random float."""
    return str(round(random.random(), 15))


def build_instruction(prompt: str, system: Optional[str] = None) -> str:
    if system:
        text = f"{system}\n\nUser: {prompt}\nAssistant:"
    else:
        text = (
            "The following is a conversation between a User and an AI assistant.\n\n"
            f"User: {prompt}\nAssistant:"
        )
    return text.replace(" ", NBSP, 1)


def normalise_instruction(instruction: str, nbsp: bool) -> str:
    instruction = instruction.replace(NBSP, " ")
    if nbsp and " " in instruction:
        instruction = instruction.replace(" ", NBSP, 1)
    return instruction


class Perchance:
    """Async client for the three reversed surfaces."""

    def __init__(
        self,
        image_key: Optional[str] = None,
        browser_id: Optional[str] = None,
        session: Optional[AsyncSession] = None,
        impersonate: str = IMPERSONATE,
        proxy: Optional[str] = None,
    ) -> None:
        self.image_key = image_key
        self.browser_id = browser_id
        self.impersonate = impersonate or IMPERSONATE
        self.proxy = proxy or None
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "Perchance":
        if self._session is None:
            kwargs: dict[str, Any] = {"impersonate": self.impersonate, "timeout": TIMEOUT}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._session = AsyncSession(**kwargs)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def s(self) -> AsyncSession:
        if self._session is None:
            raise PerchanceError("session not started; use 'async with Perchance()'")
        return self._session

    # ------------------------------------------------------------------
    # /generators
    # ------------------------------------------------------------------
    async def list_generators(self, max_results: int = 100, search: Optional[str] = None) -> dict:
        params: dict[str, Any] = {"max": max_results}
        if search:
            params["search"] = search
        try:
            r = await self.s.get(f"{PLATFORM}/api/getGeneratorList", params=params)
        except RequestsError as exc:
            raise PerchanceError(f"getGeneratorList failed: {exc}") from exc
        if r.status_code != 200:
            raise PerchanceError("getGeneratorList failed", r.status_code, r.text[:300])
        try:
            return r.json()
        except ValueError:
            raise PerchanceError("getGeneratorList non-JSON", r.status_code, r.text[:300])

    async def get_generator(self, name: str) -> dict:
        try:
            r = await self.s.post(
                f"{PLATFORM}/api/getGeneratorPageData", json={"generatorName": name}
            )
        except RequestsError as exc:
            raise PerchanceError(f"getGeneratorPageData failed: {exc}") from exc
        if r.status_code != 200:
            raise PerchanceError("getGeneratorPageData failed", r.status_code, r.text[:300])
        try:
            data = r.json()
        except ValueError:
            raise PerchanceError("getGeneratorPageData non-JSON", r.status_code, r.text[:300])
        if data.get("status") not in (None, "success"):
            raise PerchanceError(f"generator {name!r}: {data.get('status')}", r.status_code, data)
        return data

    # ------------------------------------------------------------------
    # text generation
    # ------------------------------------------------------------------
    async def generate_text(
        self,
        prompt: str = "",
        *,
        instruction: Optional[str] = None,
        system: Optional[str] = None,
        generator_name: str = "ai-chat",
        browser_id: Optional[str] = None,
        user_key: Optional[str] = None,
        thread: int = 0,
        request_id: Optional[str] = None,
        stop_sequences: Sequence[str] = ("\nUser:",),
        nbsp: bool = True,
        max_chars: int = 8000,
    ) -> AsyncIterator[dict]:
        """Yield `{"type": "chunk"|"control"|"done", ...}` dicts."""
        if instruction is None:
            instruction = build_instruction(prompt, system)
        instruction = normalise_instruction(instruction, nbsp)

        payload = {
            "instruction": instruction,
            "startWith": "",
            "stopSequences": list(stop_sequences),
            "generatorName": generator_name,
            "instructionTokenCount": max(1, len(instruction) // 4),
            "startWithTokenCount": 0,
        }

        params: dict[str, Any] = {
            "browserId": browser_id or self.browser_id or _hex(32),
            "thread": thread,
            "requestId": request_id or f"aiTextCompletion{_hex(16)}",
            "__cacheBust": repr(time.time()),
        }
        if user_key:
            params["userKey"] = user_key

        url = f"{TEXT_ORIGIN}/api/generate"
        produced = ""
        saw_final = False
        status: Optional[int] = None

        try:
            async with self.s.stream(
                "POST", url, params=params, content=json.dumps(payload), headers=JSON_HEADERS
            ) as resp:
                status = resp.status_code
                if resp.status_code != 200:
                    body = (await resp.acontent()).decode("utf-8", "replace")
                    raise PerchanceError(
                        f"text generation failed: {body[:300]}", resp.status_code, body
                    )

                async for raw in resp.aiter_lines():
                    line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("t:"):
                        try:
                            chunk = json.loads(line[2:])
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, str):
                            continue
                        produced += chunk
                        yield {"type": "chunk", "text": chunk}
                        if len(produced) >= max_chars:
                            yield {"type": "control", "stopReason": "max_chars", "final": True}
                            saw_final = True
                            break
                    elif line.startswith("data:"):
                        try:
                            ctl = json.loads(line[5:])
                        except json.JSONDecodeError:
                            continue
                        if isinstance(ctl, dict):
                            if ctl.get("final") or ctl.get("status"):
                                saw_final = True
                            yield {"type": "control", **ctl}
                        else:
                            yield {"type": "control", "raw": ctl}
        except RequestsError as exc:
            raise PerchanceError(f"text generation transport error: {exc}", status) from exc

        if not saw_final:
            yield {"type": "control", "final": True, "stopReason": "end_of_stream"}
        yield {"type": "done", "text": produced, "chars": len(produced)}

    # ------------------------------------------------------------------
    # image generation
    # ------------------------------------------------------------------
    async def generate_image(
        self,
        prompt: str,
        *,
        user_key: Optional[str] = None,
        negative_prompt: str = "",
        seed: int = -1,
        resolution: str = "768x768",
        guidance_scale: int = 7,
        channel: str = "ai-text-to-image-generator",
        sub_channel: str = "public",
        request_id: Optional[str] = None,
        max_wait_s: float = 300.0,
        poll_interval_s: float = 2.0,
    ) -> dict:
        key = user_key or self.image_key
        if not key:
            raise PerchanceError("no image userKey configured (set PERCHANCE_IMAGE_KEY)", 401)

        rid = request_id or make_request_id()
        payload = {
            "prompt": prompt,
            "negativePrompt": negative_prompt,
            "seed": seed,
            "resolution": resolution,
            "guidanceScale": guidance_scale,
            "channel": channel,
            "subChannel": sub_channel,
            "userKey": key,
            "adAccessCode": "",
            "requestId": rid,
        }
        params = {
            "userKey": key,
            "requestId": rid,
            "adAccessCode": "",
            "__cacheBust": repr(time.time()),
        }
        url = f"{IMAGE_ORIGIN}/api/generate"
        deadline = time.monotonic() + max_wait_s
        last_status: Optional[int] = None

        while True:
            try:
                r = await self.s.post(
                    url, params=params, content=json.dumps(payload), headers=JSON_HEADERS
                )
            except RequestsError as exc:
                raise PerchanceError(f"image generation transport error: {exc}") from exc
            last_status = r.status_code
            try:
                data = r.json()
            except ValueError:
                raise PerchanceError(
                    f"image generation non-JSON: {r.text[:300]}", last_status
                )

            status = data.get("status")
            if status == "waiting_for_prev_request_to_finish":
                if time.monotonic() > deadline:
                    raise PerchanceError("image generation timed out in queue", 504, data)
                await asyncio.sleep(poll_interval_s)
                continue
            if status != "success":
                raise PerchanceError(f"image generation: {status}", last_status, data)
            data.setdefault("requestId", rid)
            return data

    async def download_image(
        self,
        *,
        image_id: Optional[str] = None,
        url: Optional[str] = None,
    ) -> bytes:
        if url is None:
            if not image_id:
                raise PerchanceError("image_id or url required")
            url = f"{IMAGE_ORIGIN}/api/downloadTemporaryImage?imageId={image_id}"
        elif url.startswith("/"):
            url = IMAGE_ORIGIN + url
        try:
            r = await self.s.get(url)
        except RequestsError as exc:
            raise PerchanceError(f"image download failed: {exc}") from exc
        if r.status_code != 200:
            raise PerchanceError("image download failed", r.status_code)
        return r.content

    # ------------------------------------------------------------------
    # auth helpers
    # ------------------------------------------------------------------
    async def verification_status(self, user_key: str) -> dict:
        try:
            r = await self.s.get(
                f"{TEXT_ORIGIN}/api/checkUserVerificationStatus",
                params={"userKey": user_key, "cacheKey": int(time.time())},
            )
        except RequestsError as exc:
            return {"status": "transport_error", "detail": str(exc)}
        return r.json() if r.status_code == 200 else {"status": "http_error", "code": r.status_code}

    async def queue_position(self, user_key: Optional[str] = None) -> dict:
        key = user_key or self.image_key
        try:
            r = await self.s.get(
                f"{IMAGE_ORIGIN}/api/getUserQueuePosition",
                params={"userKey": key or "", "__cacheBust": repr(time.time())},
            )
        except RequestsError as exc:
            return {"status": "transport_error", "detail": str(exc)}
        return r.json() if r.status_code == 200 else {"status": "http_error", "code": r.status_code}
