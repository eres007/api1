# perchance-reverse-api

HTTP wrapper around the three Perchance surfaces that were reverse-engineered on
2026-10-09. Runs anywhere Docker runs — tuned for Render.

Full reversal notes: [`docs/PERCHANCE.md`](docs/PERCHANCE.md).

## What it exposes

| Route | Upstream | Notes |
|---|---|---|
| `GET /generators?max=1000&search=` | `perchance.org/api/getGeneratorList` | generator directory, no auth |
| `GET /generator/{name}` | `perchance.org/api/getGeneratorPageData` | full source of any generator |
| `GET/POST /text` | `text-generation.perchance.org/api/generate` | SSE stream of completions |
| `POST /image` | `image-generation.perchance.org/api/generate` | JSON, returns image metadata |
| `GET /image/{id}` | `…/api/downloadTemporaryImage` | image bytes |
| `GET /keys` | — | whether an image key is configured |
| `GET /healthz` | — | Render health check |

## Quick start (local)

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
curl 'http://127.0.0.1:8000/generators?max=3'
curl 'http://127.0.0.1:8000/generator/ai-chat'
curl -N 'http://127.0.0.1:8000/text?prompt=hello'
curl -X POST http://127.0.0.1:8000/image -H 'content-type: application/json' \
     -d '{"prompt":"a red apple on a wooden table","include_image":true}'
```

`/text` and `/image` both stream/return real upstream output.

## Deploying to Render

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo (`render.yaml` is picked up
   automatically), or **New → Web Service** → build command left blank,
   runtime **Docker**.
3. Set the environment var **`PERCHANCE_IMAGE_KEY`** (see below). Without it,
   `/generators`, `/generator/{name}` and `/text` all work; `/image` returns
   `503` with instructions.

Render sets `$PORT`; the Dockerfile reads it.

### Getting the image key

Text generation needs **no** key (verified: a random `browserId` with no
`userKey` streams fine). Image generation **does** enforce one — a random key
returns `invalid_key`.

On a machine with a display:

```bash
pip install -r requirements-browser.txt && playwright install chromium
python -m app.harvest --out data/image_key.txt
```

Paste the printed key into Render's `PERCHANCE_IMAGE_KEY`. (Worth confirming it
still validates from a different egress IP — if not, see below.)

### Optional: harvest on the server

Only if the instance has ≥ 1 GB RAM:

```dockerfile
# Dockerfile.browser instead of Dockerfile
```
Set `ENABLE_BROWSER_HARVEST=1` (uses Xvfb + Chromium) and point Render at
`Dockerfile.browser` via **Root Directory / Dockerfile path**. The free tier is
512 MB, which Chromium will likely OOM — the local-harvest route is safer.

## Cloudflare Worker instead?

See the tradeoff table in the repo root discussion. Short version: Workers are
better for `/generators` and `/text` (stateless, edge-local, 100k req/day free),
Render is required for anything needing a browser or long-lived state. This
codebase is WSGI-agnostic HTTP over `httpx`, so porting the stateless routes to
a Worker is mostly a rewrite of `app/perchance.py` into `fetch()` handlers.

## Notes / gotchas

- **Cloudflare fingerprints `perchance.org`.** Plain `httpx` gets `403 Just a
  moment...` on every route even with a full browser header set — it's the TLS
  fingerprint, not the headers. This service uses `curl_cffi` with
  `impersonate="chrome131"`, which passes (119–530 ms vs. a challenge page).
  If you port this, keep the impersonation.
- **`browserId` must be 32 hex chars.** Wrong length → upstream responds
  `{"status":"invalid_browser_id"}` mid-stream (an HTTP 200, so it surfaces as a
  broken chunked response unless you catch it). The *value* is not checked —
  a random 32-hex id with no `userKey` streams fine.
- **`instructionTokenCount` / `startWithTokenCount` are mandatory** or upstream
  400s `invalid_data`, even though they're just client-side `len/4` estimates.
- The first plain space in `instruction` is rewritten to NBSP (U+00A0). Pass
  `nbsp: false` to disable.
- One image generation per key at a time — upstream answers
  `waiting_for_prev_request_to_finish`, and the client polls with the **same**
  `requestId` until it clears.
- **Image bytes only come from the signed token URL.**
  `GET /api/downloadTemporaryImage?imageId=…` returns `404`; you must use the
  `imageDownloadUrl` (`/api/downloadTemporaryImageViaProxy?t=…`) returned by the
  generate call. The server caches that token URL for 1 h so `GET /image/{id}`
  keeps working; after that it answers `410` and you re-generate
  (`include_image: true` returns the bytes inline instead).
- Upstream response is a line protocol, not real SSE: `t:"chunk"` for text,
  `data:{...}` for control events. This server re-emits it as proper SSE, and
  converts mid-stream upstream failures into an `{"type":"error"}` SSE event so
  the connection doesn't just abort.
