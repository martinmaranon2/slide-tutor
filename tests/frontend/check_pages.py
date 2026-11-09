"""Behavioural check of library/reader/print pages against tests/frontend/mock_server.py (no real backend,
no Claude usage). Run: uv run python tests/frontend/check_pages.py
"""
import sys
import tempfile
from pathlib import Path

import fitz
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_server  # noqa: E402

SHOTS = Path(__file__).resolve().parents[1] / "output"
SHOTS.mkdir(exist_ok=True)
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
LID = mock_server.LID
failures: list[str] = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def main():
    srv = mock_server.serve(PORT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        external = []
        page.on("request", lambda r: external.append(r.url) if not r.url.startswith(BASE) and not r.url.startswith("data:") else None)

        # ---------------- library ----------------
        page.goto(BASE + "/")
        page.wait_for_selector(".lecture")
        page.wait_for_timeout(300)
        check(page.locator(".lecture").count() == 2, "library: 2 cards")
        check("45%" in page.inner_text("#plan-usage"), "library: plan usage shown: " + page.inner_text("#plan-usage"))
        first = page.locator(".lecture").first
        check("Lecture 08" in first.inner_text(), "library: newest first")
        check(first.locator("button:has-text('Prepare lecture')").is_hidden(), "library: prepare hidden while preparing")
        page.wait_for_timeout(4500)  # polling advances the preparing lecture
        t = first.inner_text()
        check("/6" in t and "API-equiv" in t, "library: progress + usage text: " + t.replace("\n", " | ")[:200])
        page.screenshot(path=str(SHOTS / "lib.png"))

        # ---------------- reader ----------------
        page.goto(f"{BASE}/lecture/{LID}")
        page.wait_for_selector("#slide-img[src]")
        page.wait_for_function("document.getElementById('slide-img').naturalWidth > 0")
        page.wait_for_selector("#notes-body .md")
        page.wait_for_timeout(500)
        check(page.evaluate("location.hash") == "#1", "reader: hash #1")
        sh = page.evaluate("(()=>{scrollTo(0,500);scrollTo(500,0);const r=[scrollY,scrollX,document.querySelector('.reader-main').getBoundingClientRect().bottom,innerHeight];scrollTo(0,0);return r})()")
        check(sh[0] == 0 and sh[1] == 0 and sh[2] <= sh[3] + 1, f"reader: no page scroll {sh}")
        box = page.locator("#slide-img").bounding_box()
        check(box["width"] > 700, f"reader: slide large {box}")
        page.screenshot(path=str(SHOTS / "reader1.png"))
        check(page.locator(".thumb").count() == 6, "reader: 6 thumbs")
        check(page.locator(".thumb.current").count() == 1, "reader: current thumb")
        page.keyboard.press("ArrowRight")
        page.wait_for_function("location.hash === '#2'")
        page.keyboard.press("ArrowRight")
        page.wait_for_function("location.hash === '#3'")
        page.wait_for_selector("#notes-body .md")
        page.reload()
        page.wait_for_selector("#notes-body .md")
        check(page.evaluate("location.hash") == "#3", "reader: reload keeps #3")
        page.goto(f"{BASE}/lecture/{LID}")  # no hash -> localStorage
        page.wait_for_selector("#notes-body .md")
        check(page.evaluate("location.hash") == "#3", "reader: localStorage restores slide 3")
        # jump box
        page.fill("#jump", "5")
        page.press("#jump", "Enter")
        page.wait_for_function("location.hash === '#5'")
        check("Waiting" in page.inner_text("#notes-body") or "Generating" in page.inner_text("#notes-body")
              or "No notes" in page.inner_text("#notes-body"), "reader: pending status shown for slide 5")
        page.wait_for_selector("#notes-body .md", timeout=12000)
        check("Write-back" in page.inner_text("#notes-body"), "reader: slide 5 notes arrived by polling")
        # regenerate
        page.click("#regen")
        page.wait_for_selector("#notes-body .notes-status")
        page.wait_for_selector("#notes-body .md", timeout=12000)
        check(True, "reader: regenerate cycle completes")

        # region drag on slide 5
        sb = page.locator("#slide-img").bounding_box()
        page.mouse.move(sb["x"] + sb["width"] * 0.6, sb["y"] + sb["height"] * 0.3)
        page.mouse.down()
        page.mouse.move(sb["x"] + sb["width"] * 0.8, sb["y"] + sb["height"] * 0.5, steps=5)
        check(page.locator(".sel-rect").count() == 1, "reader: selection rect visible while dragging")
        page.mouse.up()
        page.wait_for_selector("#region-chip:not([hidden])")
        check(page.locator("#pane-ask").is_visible(), "reader: region switches to Ask tab")
        check(page.evaluate("document.activeElement.id") == "q", "reader: input focused after region")
        page.screenshot(path=str(SHOTS / "reader_region.png"))
        # tiny click does not create a region
        page.click("#region-remove")
        page.mouse.click(sb["x"] + 50, sb["y"] + 50)
        check(page.locator("#region-chip").is_hidden(), "reader: tiny drag ignored")
        # re-select and ask
        page.mouse.move(sb["x"] + sb["width"] * 0.6, sb["y"] + sb["height"] * 0.3)
        page.mouse.down()
        page.mouse.move(sb["x"] + sb["width"] * 0.8, sb["y"] + sb["height"] * 0.5, steps=5)
        page.mouse.up()
        page.fill("#q", "What is this diagram?")
        page.press("#q", "Enter")
        page.wait_for_selector(".thinking")
        check(page.locator("#region-chip").is_hidden(), "reader: region cleared after ask")
        check(page.locator(".msg.user .msg-crop").count() == 1, "reader: user msg shows crop thumb")
        page.screenshot(path=str(SHOTS / "reader_thinking.png"))
        # navigate away while streaming
        page.click("#next")
        page.wait_for_function("location.hash === '#6'")
        check(page.locator(".msg").count() == 0, "reader: slide 6 thread empty while 5 streams")
        page.wait_for_timeout(300)
        page.click("#prev")
        page.wait_for_function("location.hash === '#5'")
        page.wait_for_function("document.querySelectorAll('.msg.assistant .katex').length > 0", timeout=15000)
        page.wait_for_function("document.getElementById('send').textContent === 'Send'", timeout=15000)
        txt = page.inner_text(".msg.assistant")
        check("costs $5" in txt and "Takeaway" in txt, "reader: streamed answer rendered with math/table")
        check(page.locator(".msg.assistant .katex-error").count() == 0, "reader: no katex errors in chat")
        page.screenshot(path=str(SHOTS / "reader_chat.png"))
        # '/' shortcut
        page.click("#tab-notes")
        page.keyboard.press("/")
        check(page.evaluate("document.activeElement.id") == "q" and page.locator("#pane-ask").is_visible(), "reader: / focuses ask input")
        # typing '/' and arrows inside textarea must not navigate
        page.keyboard.type("a/b")
        page.keyboard.press("ArrowLeft")
        check(page.evaluate("location.hash") == "#5", "reader: arrows ignored while typing")
        page.fill("#q", "")
        # check my answer
        page.click("#q-check")
        check(page.input_value("#q") == "Check my answer: ", "reader: check-answer prefill")
        page.fill("#q", "")
        # clear thread
        page.click("#clear-thread")
        page.wait_for_function("document.querySelectorAll('.msg').length === 0")
        check(True, "reader: thread cleared")
        # quick button
        page.click("#q-exam")
        page.wait_for_selector(".msg.user")
        check("exam-style question" in page.inner_text(".msg.user"), "reader: exam quick button sends text")
        page.wait_for_function("document.getElementById('send').textContent === 'Send'", timeout=15000)
        check(not [e for e in errors if "404" not in e], f"reader: no console errors {errors}")
        check(not external, f"no external requests {external[:3]}")

        # ---------------- print ----------------
        pp = ctx.new_page()
        perr = []
        pp.on("pageerror", lambda e: perr.append(str(e)))
        pp.goto(f"{BASE}/lecture/{LID}/print")
        pp.wait_for_selector("body[data-ready='1']", timeout=30000)
        sizes = pp.evaluate("[...document.querySelectorAll('.page')].map(p=>[+p.dataset.fontSize, !!p.dataset.overflow, p.getBoundingClientRect().height])")
        print("page sizes (pt, overflow, px height):", sizes)
        check(len(sizes) == 6, "print: 6 pages")
        check(all(s[0] >= 8 for s in sizes), "print: never below 8pt")
        check(sizes[1][0] == 11, "print: short notes keep 11pt")
        check(sizes[2][0] < 11, "print: long notes shrunk")
        check("Ready" in pp.inner_text("#tb-status"), "print: status Ready")
        check(pp.locator("#pdf-link").is_visible(), "print: Download PDF link shown")
        w = pp.evaluate("document.querySelector('.slide-box img').getBoundingClientRect().width")
        check(abs(w - 110 * 96 / 25.4) < 2, f"print: slide 110mm wide ({w:.1f}px)")
        pp.screenshot(path=str(SHOTS / "print_screen.png"), full_page=False)
        out = SHOTS / "print_test.pdf"
        pp.pdf(path=str(out), prefer_css_page_size=True, print_background=True)
        n = fitz.open(out).page_count
        check(n == 6 + sum(1 for s in sizes if s[1]) * 0 or n >= 6, f"print: pdf pages = {n}")
        print("pdf pages:", n)
        check(not perr, f"print: no page errors {perr}")
        b.close()
    srv.shutdown()
    print("FAILED" if failures else "ALL OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
