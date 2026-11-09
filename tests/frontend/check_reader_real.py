"""GET-only check that stored chat threads render. Usage: check_reader_real.py <lecture-id> <slide> [base]"""
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "output"
OUT.mkdir(exist_ok=True)
lid, slide = sys.argv[1], sys.argv[2]
base = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8000"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{base}/lecture/{lid}#{slide}")
    pg.wait_for_selector("#notes-body .md")
    pg.click("#tab-ask")
    pg.wait_for_selector(".msg")
    info = pg.evaluate("[...document.querySelectorAll('.msg')].map(m=>[m.className, m.querySelector('.md-slot').innerText.length, m.querySelectorAll('.katex').length, !!m.querySelector('.msg-crop')])")
    print(info, errs)
    pg.screenshot(path=str(OUT / "real_chat.png"))
    assert all(i[1] > 0 for i in info), "empty bubble"
    b.close()
