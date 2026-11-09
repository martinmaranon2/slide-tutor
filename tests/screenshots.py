"""Playwright screenshots of the running app (library, reader + notes, chat while streaming/done, region chip, print view).

Usage:  uv run python tests/screenshots.py <lecture_id> [--ask] [--out DIR] [--slide N]
--ask sends one real question (costs one chat call on your plan). Without it, the chat screenshot shows the
existing thread. Requires the server running on http://127.0.0.1:8000.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lecture_id")
    ap.add_argument("--ask", action="store_true")
    ap.add_argument("--slide", type=int, default=2)
    ap.add_argument("--table-slide", type=int, default=5)
    ap.add_argument("--out", default=str(Path(__file__).parent / "screenshots"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lid, n = a.lecture_id, a.slide
    errors: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = await ctx.new_page()
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        await page.goto(BASE + "/", wait_until="networkidle")
        await page.wait_for_timeout(500)
        await page.screenshot(path=out / "01_library.png")

        await page.goto(f"{BASE}/lecture/{lid}#{n}", wait_until="networkidle")
        await page.wait_for_selector("#notes-body .katex", timeout=15000)
        await page.wait_for_timeout(400)
        await page.screenshot(path=out / "02_reader_notes.png")
        # scroll the notes to the bottom (Takeaway, display math)
        await page.eval_on_selector("#notes-scroll", "e => e.scrollTop = e.scrollHeight")
        await page.wait_for_timeout(200)
        await page.screenshot(path=out / "03_reader_notes_bottom.png")

        await page.goto(f"{BASE}/lecture/{lid}#{a.table_slide}", wait_until="networkidle")
        await page.wait_for_selector("#notes-body p", timeout=15000)
        await page.wait_for_timeout(300)
        has_table = await page.eval_on_selector("#notes-body", "e => !!e.querySelector('table')")
        if has_table:
            await page.eval_on_selector("#notes-body table", "e => e.scrollIntoView({block:'center'})")
        await page.wait_for_timeout(200)
        await page.screenshot(path=out / "04_reader_table_slide.png")

        # back to slide n, Ask tab via "/"
        await page.goto(f"{BASE}/lecture/{lid}#{n}", wait_until="networkidle")
        await page.wait_for_selector("#notes-body .katex", timeout=15000)
        await page.locator("body").press("/")
        await page.wait_for_selector("#pane-ask:not([hidden])")
        if a.ask:
            await page.fill("#q", "Give me a 2-line intuition for why the serial part dominates, with one formula.")
            await page.locator("#q").press("Enter")
            await page.wait_for_function(
                "() => { const m = [...document.querySelectorAll('.msg.assistant .md-slot')].pop();"
                " return m && m.innerText.length > 60; }", timeout=90000)
            await page.screenshot(path=out / "05_ask_streaming.png")
            await page.wait_for_function("() => !document.querySelector('#send').disabled", timeout=120000)
            await page.wait_for_timeout(400)
        await page.screenshot(path=out / "06_ask_done.png")

        # region selection chip (no question sent)
        box = await page.locator("#sel-layer").bounding_box()
        if box:
            x0, y0 = box["x"] + box["width"] * 0.05, box["y"] + box["height"] * 0.72
            await page.mouse.move(x0, y0)
            await page.mouse.down()
            await page.mouse.move(x0 + box["width"] * 0.6, y0 + box["height"] * 0.12, steps=8)
            await page.mouse.up()
            await page.wait_for_timeout(300)
            await page.screenshot(path=out / "07_region_chip.png")

        await page.goto(f"{BASE}/lecture/{lid}/print", wait_until="networkidle")
        await page.wait_for_selector('body[data-ready="1"]', timeout=120000)
        await page.screenshot(path=out / "08_print_view.png")
        await page.screenshot(path=out / "09_print_view_full.png", full_page=True)
        pdf = await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        (out / "handout.pdf").write_bytes(pdf)
        info = await page.evaluate("""() => [...document.querySelectorAll('.page')].map(p => {
            const r = p.getBoundingClientRect(); const notes = p.querySelector('.notes, .page-notes, [class*=notes]');
            return {h_mm: Math.round(r.height / 3.7795), font: notes ? getComputedStyle(notes).fontSize : null,
                    cls: p.className}; })""")
        print("print pages:", info)
        await browser.close()

    import pymupdf
    with pymupdf.open(out / "handout.pdf") as d:
        print("handout.pdf pages:", d.page_count, "size(pt):", [round(v) for v in d[0].rect[2:]])
    print("screenshots in", out)
    print("console errors:" if errors else "no console errors", *errors, sep="\n  ")


if __name__ == "__main__":
    asyncio.run(main())
