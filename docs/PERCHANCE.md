# Perchance — Reversed API Notes

Reverse-engineered 2026-10-09 from the three public surfaces, then verified live
with `perchance_suite.py` (22/22 passing). Nothing here requires auth cookies —
only the anti-bot keys the sites hand out themselves.

## Architecture

Perchance is a generator platform. Every page (`/ai-chat`,
`/ai-text-to-image-generator`, `/anything`) is a *generator*: a Perchance-DSL
program (`modelText`) plus an HTML template (`outputTemplate`), served to a
sandboxed iframe on a random subdomain
(`cd2824…​.perchance.org/<generator-name>`).

Two separate AI backends power the generators, both on their own origins:

| Origin | Serves | Protocol |
|---|---|---|
| `text-generation.perchance.org` | `/ai-chat` and any `{import:ai-text-plugin}` | SSE-ish stream |
| `image-generation.perchance.org` | `/ai-text-to-image-generator` and any `{import:text-to-image-plugin}` | JSON request/response |

Generators do not call these APIs directly. Each plugin embeds an iframe pointing
at `<origin>/embed`, and the parent talks to it via `postMessage`
(`{type:"startStream", url, postData, requestId}`). The embed owns the network
call, the anti-bot state, and the streaming parse. That indirection is the only
reason the endpoints aren't trivially callable — but the protocol is simple once
you read the embed.

## 1. `/generators` — the directory

```
GET https://perchance.org/api/getGeneratorList?max=1000
```

No auth. Returns `{status, generators:[…]}` ordered by `lastEditTime` desc.

```json
{"name":"bestaigen","views":65709,"lastEditTime":1791563284980,
 "lastEditTime_ago":112222,
 "metaData":{"title":"…","description":"…","image":"https://…",
             "tags":["…"],"header":{}}}
```

`search=` is accepted but ignored by this endpoint. Note: the *visible* page
renders nothing without JS — the list you see in a browser comes from this XHR.
A hidden `position:fixed;height:0` div in the static HTML carries a SEO link farm
of the same names.

### Generator source

```
POST https://perchance.org/api/getGeneratorPageData
Content-Type: application/json
{"generatorName":"ai-chat"}
```

Returns the full DSL program. This is how you read any generator's logic,
including its prompt templates. `GET /api/generatorPageData/<name>/<token>` is a
cached variant; `POST` is always authoritative. Unknown names →
`{"status":"not-found"}`.

## 2. Text generation (`/ai-chat`)

```
POST https://text-generation.perchance.org/api/generate
    ?browserId=<32 hex>&userKey=<64 hex>&thread=0
    &requestId=<string>&__cacheBust=<float>
Content-Type: application/json
```

Body — **`instructionTokenCount` and `startWithTokenCount` are mandatory**; the
server 400s `invalid_data` without them, even though they're client-computed
estimates (~chars/4):

```json
{
  "instruction": "…prompt… ",        // note: first space is NBSP (U+00A0)
  "startWith": "",
  "stopSequences": ["\nUser:"],
  "generatorName": "ai-chat",
  "instructionTokenCount": 42,
  "startWithTokenCount": 0
}
```

The NBSP is deliberate — the plugin rewrites the first plain space so the
server-side tokenizer can split instruction from completion.

Response is a line protocol, not standard SSE:

```
t:"hello"          <- JSON-encoded text chunk

t:" from"

data:{"text":"","final":true,"stopReason":"natural"}
```

`t:` lines carry raw text; `data:` lines carry control events
(`final`, `stopReason`, `error`/`status`). Concatenate the `t:` payloads.

### Auth

`browserId` is a random 32-hex string the embed mints client-side
(`/public/generation-identity-v2.js`) and persists in `localStorage`. A valid
`userKey` comes from Cloudflare Turnstile (sitekey `0x4AAAAAAAJn3pYzPx4ATVOt`)
via `GET /api/verifyUser?browserId=…&token=…`.

**Finding: `/api/generate` on this origin does not gate on either value.** A
completely random `browserId` with no `userKey` streams completions normally.
The Turnstile flow is real but its result isn't enforced at the generation
endpoint — only `postData` shape is validated. Verified: random-browserId +
no-key streams; missing `requestId` also streams (server synthesizes one).

Related endpoints that *do* validate the key:
- `GET /api/checkUserVerificationStatus?userKey=…` → `verified` / `need_reverification_soon`
- `GET /api/verifyUser?browserId=…&thread=…` → tokenless attempt, else Turnstile

## 3. Image generation (`/ai-text-to-image-generator`)

```
POST https://image-generation.perchance.org/api/generate
    ?userKey=<64 hex>&requestId=<float-ish>&adAccessCode=&__cacheBust=<float>
Content-Type: application/json
```

```json
{
  "prompt": "a red apple on a wooden table, photorealistic",
  "negativePrompt": "",
  "seed": -1,
  "resolution": "768x768",
  "guidanceScale": 7,
  "channel": "ai-text-to-image-generator",
  "subChannel": "public",
  "userKey": "…",
  "adAccessCode": "",
  "requestId": "0.433990417602031"
}
```

The front-end appends an art-style suffix to `prompt` before sending (visible in
captured traffic) — the raw prompt is passed through, so style text is entirely
client-side.

Responses:
- `{"status":"waiting_for_prev_request_to_finish"}` — one generation per key at a time; retry with the same `requestId`.
- `{"status":"invalid_key"}` — key not Turnstile-verified.
- `{"status":"success","imageId":"…","fileExtension":"jpeg","seed":917013955,
   "prompt":"…","width":"768","height":"768","guidanceScale":"7",
   "negativePrompt":"","maybeNsfw":"False",
   "imageDownloadUrl":"/api/downloadTemporaryImageViaProxy?t=v1.…"}`

Download via the returned `imageDownloadUrl` (prefix the origin — it's
relative), or `GET /api/downloadTemporaryImage?imageId=…`. Both return
`image/jpeg`. `GET /api/getUserQueuePosition?userKey=…` reports queue depth.

Unlike text generation, **this origin does enforce the key** — a random 64-hex
`userKey` returns `invalid_key`. Verification uses the same Turnstile sitekey
but its own `browserId`/`localStorage` partition; the key must be harvested from
an `image-generation.perchance.org` frame, not the text one.

## Getting a verified key

The Turnstile widget must run in a real browser. Practical approach used here:

1. Launch headed Chromium (patchright/Playwright).
2. Load `https://perchance.org/ai-chat` → the ai-text-plugin embed runs
   `verifyUser`, solves Turnstile, and writes `userKey-0` into
   `text-generation.perchance.org` localStorage.
3. Read `localStorage` from any frame whose URL starts with that origin.
4. Repeat against `https://perchance.org/ai-text-to-image-generator`,
   *triggering one generation* first — the image embed is only created when a
   generation is requested, so step 3 alone yields nothing on that origin.

Key path: `generation-v2:<browserId>:userKey-<thread>`; browser id under
`generation-v2-browser`.

## Running the suite

```
pip install "scrapling[fetchers]" && scrapling install
python perchance_suite.py
```

Opens a headed browser to harvest keys, then exercises all three surfaces with
direct HTTP calls. Writes `test_results.json`. Last run: **22/22 passed**,
including a real 768×768 JPEG downloaded end-to-end.
