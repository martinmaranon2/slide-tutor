"""GET-only check of the print view against a running backend. Usage: check_print_real.py <lecture-id> [base]"""
import sys
from pathlib import Path
import pymupdf
from playwright.sync_api import sync_playwright

lid = sys.argv[1]
base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
out = Path(__file__).resolve().parents[1] / "output"
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1000, "height": 1200})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{base}/lecture/{lid}/print")
    pg.wait_for_selector("body[data-ready='1']", timeout=60000)
    info = pg.evaluate("[...document.querySelectorAll('.page')].map(p=>[+p.dataset.fontSize, p.dataset.imgWidth||110, !!p.dataset.overflow, Math.round(p.getBoundingClientRect().height/96*25.4)])")
    print("font pt / img mm / overflow / height mm:", info)
    print("status:", pg.inner_text("#tb-status"), "errors:", errs)
    pdf = out / "print_real.pdf"
    pg.pdf(path=str(pdf), format="A4", prefer_css_page_size=True, print_background=True)
    d = pymupdf.open(pdf)
    print("pdf pages:", d.page_count)
    for i in range(min(d.page_count, 6)):
        d[i].get_pixmap(dpi=80).save(out / f"print_real_{i+1}.png")
    b.close()
