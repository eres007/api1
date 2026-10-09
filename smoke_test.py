import json, os, sys, time, httpx

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"PASS  {name}  {detail}")
    else:
        fail += 1
        print(f"FAIL  {name}  {detail}")


c = httpx.Client(base_url=BASE, timeout=120)

r = c.get("/healthz")
check("healthz", r.status_code == 200 and r.json().get("status") == "ok", r.text)

r = c.get("/")
check("index lists endpoints", r.status_code == 200 and "endpoints" in r.json())

t0 = time.perf_counter()
r = c.get("/generators", params={"max": 10})
ms = (time.perf_counter() - t0) * 1000
data = r.json() if r.status_code == 200 else {}
gens = data.get("generators") or []
check("GET /generators", r.status_code == 200 and len(gens) > 0, f"n={len(gens)} {ms:.0f}ms")

r = c.get("/generator/dice-roller")
d = r.json() if r.status_code == 200 else {}
check("GET /generator/{name}", r.status_code == 200 and d.get("status") == "success",
      f"modelText={len(d.get('generator', {}).get('modelText', ''))} chars")

r = c.get("/generator/does-not-exist-xyz")
check("GET /generator unknown -> error", r.status_code >= 400, f"status={r.status_code}")

# text streaming
t0 = time.perf_counter()
events = []
first = None
with c.stream("GET", "/text", params={"prompt": "Say hello in exactly five words."}) as resp:
    check("GET /text status", resp.status_code == 200, f"{resp.status_code} {resp.headers.get('content-type')}")
    for line in resp.iter_lines():
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        if ev.get("type") == "chunk" and first is None:
            first = (time.perf_counter() - t0) * 1000
        events.append(ev)
text = "".join(e["text"] for e in events if e.get("type") == "chunk")
total = (time.perf_counter() - t0) * 1000
check("text produced output", len(text) > 0, repr(text[:60]))
check("text SSE has done event", any(e.get("type") == "done" for e in events),
      f"first={first or 0:.0f}ms total={total:.0f}ms events={len(events)}")

# POST /text with instruction
with c.stream("POST", "/text", json={"instruction": "User: 2+2?\nAssistant:",
                                     "nbsp": False, "max_chars": 400}) as resp:
    got = "".join(
        json.loads(l[6:])["text"]
        for l in resp.iter_lines() if l.startswith("data: ")
        and json.loads(l[6:]).get("type") == "chunk"
    )
check("POST /text instruction", resp.status_code == 200 and len(got) > 0, repr(got[:60]))

r = c.post("/text", json={})
check("POST /text empty -> 400", r.status_code == 400, str(r.status_code))

# image: behaviour depends on whether a key is configured
r = c.get("/keys")
has_key = r.status_code == 200 and bool(r.json().get("image_key_present"))
check("GET /keys", r.status_code == 200, f"key={has_key}")

r = c.post("/image", json={"prompt": "test"})
if has_key:
    check("POST /image with key accepted", r.status_code == 200, str(r.status_code))
else:
    check("POST /image without key -> 503", r.status_code == 503, str(r.status_code))
    check("503 explains remedy", "PERCHANCE_IMAGE_KEY" in r.text)

r = c.post("/keys/harvest")
check("harvest disabled -> 400", r.status_code == 400, str(r.status_code))

print(f"\nTOTAL: {ok}/{ok+fail} passed")
sys.exit(1 if fail else 0)
