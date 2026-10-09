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


c = httpx.Client(base_url=BASE, timeout=300)

r = c.get("/healthz")
check("healthz reports key", r.json().get("image_key") is True, r.text)

t0 = time.perf_counter()
r = c.post("/image", json={"prompt": "a red apple on a wooden table, photorealistic",
                           "include_image": True})
ms = (time.perf_counter() - t0) * 1000
d = r.json() if r.status_code == 200 else {}
check("POST /image", r.status_code == 200 and d.get("status") == "success",
      f"status={r.status_code} {ms:.0f}ms")
check("image has id/seed", bool(d.get("imageId")) and bool(d.get("seed")),
      f"seed={d.get('seed')} {d.get('width')}x{d.get('height')}")
check("include_image returned base64", bool(d.get("image_base64")),
      f"bytes={d.get('bytes')}")

if d.get("download_url"):
    t0 = time.perf_counter()
    r2 = c.get(d["download_url"])
    check("GET /image/{id}", r2.status_code == 200 and r2.headers.get("content-type", "").startswith("image/"),
          f"{r2.status_code} {len(r2.content)}B {((time.perf_counter()-t0)*1000):.0f}ms")

r = c.post("/image", json={})
check("POST /image empty -> 400", r.status_code == 400, str(r.status_code))

print(f"\nTOTAL: {ok}/{ok+fail} passed")
sys.exit(1 if fail else 0)
