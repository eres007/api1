"""HTTP layer over the reversed Perchance API.

    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Settings
from .keys import KeyStore
from .perchance import Perchance, PerchanceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("api")

settings = Settings.from_env()
keys = KeyStore(settings.key_file, initial=settings.image_key,
                auto_harvest=settings.enable_browser_harvest)

# Upstream only serves image bytes through a short-lived signed token URL
# (/api/downloadTemporaryImageViaProxy?t=...). The bare ?imageId= form 404s, so
# remember the token URL for the lifetime of the process.
_DOWNLOAD_URLS: dict[str, tuple[str, float]] = {}
_DOWNLOAD_TTL_S = 3600.0


def _remember_url(image_id: str, url: str) -> None:
    now = time.time()
    for iid, (_u, exp) in list(_DOWNLOAD_URLS.items()):
        if exp < now:
            _DOWNLOAD_URLS.pop(iid, None)
    _DOWNLOAD_URLS[image_id] = (url, now + _DOWNLOAD_TTL_S)


def _lookup_url(image_id: str) -> Optional[str]:
    hit = _DOWNLOAD_URLS.get(image_id)
    if not hit:
        return None
    url, exp = hit
    if exp < time.time():
        _DOWNLOAD_URLS.pop(image_id, None)
        return None
    return url


@asynccontextmanager
async def lifespan(_app: FastAPI):
    keys.start_background()
    log.info("image key present: %s", bool(keys.get()))
    yield


app = FastAPI(title="Perchance Reversed API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _client() -> Perchance:
    return Perchance(
        image_key=keys.get() or None,
        impersonate=settings.impersonate,
        proxy=settings.proxy_url,
    )


@app.exception_handler(PerchanceError)
async def _upstream_error(_req, exc: PerchanceError):
    status = exc.status if isinstance(exc.status, int) and 400 <= exc.status < 600 else 502
    return JSONResponse(status_code=status, content={"error": str(exc), "detail": exc.payload})


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "image_key": bool(keys.get()),
        "impersonate": settings.impersonate,
        "proxy": bool(settings.proxy_url),
    }


@app.get("/")
async def index() -> dict:
    return {
        "service": "perchance-reverse-api",
        "endpoints": {
            "GET /healthz": "liveness + whether an image key is configured",
            "GET /generators?max=1000&search=": "generator directory",
            "GET /generator/{name}": "full generator source (DSL + template)",
            "GET /text?prompt=...": "text completion, streams SSE",
            "POST /text": "{prompt|instruction, system?, nbsp?, ...} -> SSE stream",
            "POST /image": "{prompt, resolution?, ...} -> image metadata",
            "GET /image/{image_id}": "download generated image bytes",
            "GET /keys": "image key status",
            "POST /keys/harvest": "on-server Turnstile harvest (if enabled)",
        },
        "docs": "/docs",
    }


@app.get("/probe")
async def probe() -> dict:
    """Report whether upstream is reachable from this instance's egress IP."""
    out: dict[str, Any] = {"impersonate": settings.impersonate, "proxy": bool(settings.proxy_url)}
    async with _client() as p:
        try:
            data = await p.list_generators(1)
            out["perchance"] = {"status": 200, "generators": len(data.get("generators") or [])}
        except PerchanceError as exc:
            body = str(exc.payload or exc)
            out["perchance"] = {
                "status": exc.status,
                "challenge": "Just a moment" in body,
                "detail": body[:160],
            }
        try:
            chunks = []
            async for ev in p.generate_text("hi", max_chars=50):
                if ev.get("type") == "chunk":
                    chunks.append(ev["text"])
                if ev.get("type") == "done":
                    break
            out["text"] = {"ok": True, "sample": "".join(chunks)[:60]}
        except PerchanceError as exc:
            body = str(exc.payload or exc)
            out["text"] = {"ok": False, "status": exc.status,
                           "challenge": "Just a moment" in body, "detail": body[:160]}
    return out


@app.get("/generators")
async def generators(
    max: int = Query(100, ge=1, le=1000),
    search: Optional[str] = None,
) -> dict:
    async with _client() as p:
        return await p.list_generators(max, search)


@app.get("/generator/{name}")
async def generator(name: str) -> dict:
    async with _client() as p:
        return await p.get_generator(name)


def _sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"


async def _text_stream(payload: dict) -> Any:
    try:
        async with _client() as p:
            async for event in p.generate_text(
                prompt=payload.get("prompt", ""),
                instruction=payload.get("instruction"),
                system=payload.get("system"),
                generator_name=payload.get("generator_name", "ai-chat"),
                user_key=payload.get("user_key") or None,
                nbsp=bool(payload.get("nbsp", True)),
                max_chars=int(payload.get("max_chars", settings.text_max_chars)),
            ):
                yield _sse(event)
    except PerchanceError as exc:
        # Headers are already sent, so surface the failure in-band rather than
        # aborting the chunked response.
        yield _sse({"type": "error", "error": str(exc), "upstream_status": exc.status})
        yield _sse({"type": "done", "text": "", "chars": 0})


@app.get("/text")
async def text_get(
    prompt: str = Query(..., min_length=1),
    max_chars: int = Query(4000, ge=100, le=32000),
) -> StreamingResponse:
    return StreamingResponse(
        _text_stream({"prompt": prompt, "max_chars": max_chars}),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/text")
async def text_post(payload: dict) -> StreamingResponse:
    if not payload.get("prompt") and not payload.get("instruction"):
        raise HTTPException(400, "provide 'prompt' or 'instruction'")
    return StreamingResponse(
        _text_stream(payload),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/image")
async def image(payload: dict) -> dict:
    prompt = payload.get("prompt")
    if not prompt:
        raise HTTPException(400, "provide 'prompt'")
    if not keys.get():
        raise HTTPException(
            503,
            "no image userKey configured — set PERCHANCE_IMAGE_KEY, or run "
            "`python -m app.harvest --out data/image_key.txt` on a desktop and redeploy",
        )

    async with _client() as p:
        data = await p.generate_image(
            prompt,
            negative_prompt=payload.get("negative_prompt", ""),
            seed=int(payload.get("seed", -1)),
            resolution=payload.get("resolution", settings.default_resolution),
            guidance_scale=int(payload.get("guidance_scale", 7)),
            max_wait_s=float(payload.get("max_wait_s", settings.image_max_wait_s)),
        )

    image_id = data.get("imageId")
    upstream_url = data.get("imageDownloadUrl")
    if image_id and upstream_url:
        _remember_url(image_id, upstream_url)

    out = {
        k: data.get(k)
        for k in ("status", "imageId", "fileExtension", "seed", "prompt",
                  "width", "height", "guidanceScale", "negativePrompt", "maybeNsfw")
        if k in data
    }
    out["download_url"] = f"/image/{image_id}" if image_id else None

    if payload.get("include_image") and image_id:
        try:
            async with _client() as p:
                raw = await p.download_image(image_id=image_id, url=upstream_url)
            out["image_base64"] = __import__("base64").b64encode(raw).decode("ascii")
            out["bytes"] = len(raw)
        except PerchanceError as exc:
            out["download_error"] = str(exc)
    return out


@app.get("/image/{image_id}")
async def image_download(image_id: str) -> Response:
    url = _lookup_url(image_id)
    if not url:
        raise HTTPException(
            410,
            "image token expired or unknown on this instance — regenerate with "
            "POST /image?include_image=true to get the bytes directly",
        )
    async with _client() as p:
        try:
            raw = await p.download_image(url=url)
        except PerchanceError as exc:
            raise HTTPException(exc.status if exc.status and exc.status >= 400 else 502, str(exc))
    return Response(content=raw, media_type="image/jpeg",
                    headers={"Content-Disposition": f'inline; filename="{image_id}.jpeg"'})


@app.get("/keys")
async def keys_status() -> dict:
    status: dict[str, Any] = {
        "image_key_present": bool(keys.get()),
        "image_key_prefix": (keys.get()[:12] + "...") if keys.get() else None,
        "source": settings.key_source,
        "auto_harvest": settings.enable_browser_harvest,
    }
    if keys.get():
        async with _client() as p:
            try:
                status["queue_position"] = await p.queue_position()
            except PerchanceError as exc:
                status["queue_position"] = {"error": str(exc)}
    return status


@app.post("/keys/harvest")
async def keys_harvest() -> dict:
    if not settings.enable_browser_harvest:
        raise HTTPException(400, "ENABLE_BROWSER_HARVEST is off on this instance")
    loop = asyncio.get_running_loop()
    key = await loop.run_in_executor(None, KeyStore.harvest_once)
    if not key:
        raise HTTPException(502, "harvest returned no key")
    keys.set(key)
    return {"status": "ok", "key_prefix": key[:16] + "..."}
