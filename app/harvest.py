"""Turnstile key harvester.

Turnstile must run in a real browser. This loads the image generator, triggers a
generation (the image embed is only created on demand), then reads `userKey-0`
out of `image-generation.perchance.org` localStorage.

    python -m app.harvest --out data/image_key.txt

Requires `pip install -r requirements-browser.txt` and a real display. On Linux
without one, wrap with Xvfb:  xvfb-run -a python -m app.harvest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

WARMUP_PROMPT = "warmup"
PAGE = "https://perchance.org/ai-text-to-image-generator"


def harvest_image_key(prompt: str = WARMUP_PROMPT, wait_ms: int = 45000) -> str | None:
    from patchright.sync_api import sync_playwright

    key: str | None = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(PAGE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(40000)

        gen_frame = None
        for frame in page.frames:
            try:
                if frame.evaluate("() => !!document.querySelector('#generateButtonEl')"):
                    gen_frame = frame
                    break
            except Exception:  # noqa: BLE001
                continue

        if gen_frame:
            gen_frame.evaluate(
                """() => {
                    const tas = [...document.querySelectorAll('textarea')]
                        .filter(t => t.offsetWidth || t.offsetHeight);
                    if (!tas.length) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(tas[0], %r);
                    tas[0].dispatchEvent(new Event('input', { bubbles: true }));
                }"""
                % prompt
            )
            gen_frame.click("#generateButtonEl")
            page.wait_for_timeout(wait_ms)

        for frame in page.frames:
            if not frame.url.startswith("https://image-generation.perchance.org"):
                continue
            try:
                store = frame.evaluate("() => ({ ...localStorage })")
            except Exception:  # noqa: BLE001
                continue
            for k, v in store.items():
                if k.endswith(":userKey-0") and v:
                    key = v

        browser.close()
    return key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Harvest an image-gen Turnstile userKey")
    ap.add_argument("--out", default="data/image_key.txt")
    ap.add_argument("--prompt", default=WARMUP_PROMPT)
    args = ap.parse_args(argv)

    key = harvest_image_key(args.prompt)
    if not key:
        print("FAILED: no userKey found in localStorage", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(key, encoding="utf-8")
    print(f"SAVED {key[:16]}... -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
