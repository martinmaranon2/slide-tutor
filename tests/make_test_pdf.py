"""Generate a small synthetic lecture deck (tests/fixtures/test_lecture.pdf).

Deterministic; uses only PyMuPDF built-in fonts. Slide 2 contains a deliberately
wrong worked example (AMAT is really 6 ns, the slide says 5 ns) and is NOT marked as wrong.
"""
import math
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent / "fixtures" / "test_lecture.pdf"
W, H = 960, 540
NAVY = (0.10, 0.20, 0.40)
INK = (0.12, 0.12, 0.14)
GREY = (0.45, 0.45, 0.48)
LIGHT = (0.94, 0.95, 0.97)
BLUE = (0.12, 0.38, 0.80)
ORANGE = (0.85, 0.35, 0.05)
RED = (0.75, 0.12, 0.12)


def hexc(c):
    return "#%02x%02x%02x" % tuple(round(v * 255) for v in c)


def T(page, x, y, w, h, html, size=20, color=INK, bold=False, mono=False, align="left"):
    css = (f"* {{ font-family: {'monospace' if mono else 'sans-serif'}; font-size: {size}pt; "
           f"color: {hexc(color)}; font-weight: {'bold' if bold else 'normal'}; "
           f"text-align: {align}; margin: 0; padding: 0; }}")
    page.insert_htmlbox(pymupdf.Rect(x, y, x + w, y + h), html, css=css, scale_low=1)


def arrow(page, p1, p2, color=RED, width=2.5):
    page.draw_line(p1, p2, color=color, width=width)
    ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    L, sp = 12, 0.4
    a = (p2[0] - L * math.cos(ang - sp), p2[1] - L * math.sin(ang - sp))
    b = (p2[0] - L * math.cos(ang + sp), p2[1] - L * math.sin(ang + sp))
    page.draw_polyline([p2, a, b, p2], color=color, fill=color, width=1, closePath=True)


def box(page, r, label=None, fill=LIGHT, color=NAVY, size=20, bold=False, mono=False):
    r = pymupdf.Rect(r)
    page.draw_rect(r, color=color, fill=fill, width=1.5)
    if label:
        T(page, r.x0 + 4, r.y0 + (r.height - size * 1.5) / 2, r.width - 8, size * 1.6,
          label, size=size, bold=bold, mono=mono, align="center")


def slide(doc, n, title, tsize=34, bar=80):
    page = doc.new_page(width=W, height=H)
    page.draw_rect((0, 0, W, bar), color=None, fill=NAVY)
    T(page, 40, (bar - tsize * 1.5) / 2, 880, tsize * 1.6, title, size=tsize, color=(1, 1, 1), bold=True)
    page.draw_line((40, 518), (920, 518), color=(0.8, 0.8, 0.82), width=0.8)
    T(page, 40, 521, 880, 16, f"DPHPC · Lecture 7 · {n}", size=10, color=GREY)
    return page


doc = pymupdf.open()

# 1 Title / agenda
p = slide(doc, 1, "Lecture 7: Caches, Coherence &amp; Speedup", tsize=38, bar=110)
T(p, 40, 130, 880, 34, "Design of Parallel and High-Performance Computing (DPHPC)", size=22, color=GREY)
T(p, 40, 190, 880, 34, "Agenda", size=24, bold=True, color=NAVY)
T(p, 60, 235, 840, 260,
  "<ul><li>Speedup and Amdahl's Law</li><li>Average memory access time (AMAT)</li>"
  "<li>Cache coherence: the MESI protocol</li><li>Data races and why they matter</li></ul>",
  size=22)

# 2 Formulas
p = slide(doc, 2, "Amdahl's Law &amp; AMAT")
T(p, 40, 100, 880, 40, "S(p) = 1 / ((1 − f) + f/p)", size=26, bold=True, color=NAVY)
T(p, 40, 145, 880, 70,
  "f = parallelizable fraction of the work, p = number of processors<br>"
  "Limit: p → ∞ ⇒ S = 1/(1−f)", size=20)
p.draw_line((40, 235), (920, 235), color=(0.8, 0.8, 0.82), width=1)
T(p, 40, 250, 880, 40, "AMAT = t<sub>hit</sub> + m · t<sub>miss</sub>", size=26, bold=True, color=NAVY)
T(p, 40, 295, 880, 70,
  "m = miss rate, t<sub>hit</sub> = hit time, t<sub>miss</sub> = miss penalty", size=20)
T(p, 40, 400, 880, 80,
  "<i>Example:</i> t<sub>hit</sub> = 1 ns, m = 0.05, t<sub>miss</sub> = 100 ns ⇒ AMAT = 5 ns", size=22)

# 3 MESI diagram
p = slide(doc, 3, "MESI: Write to a Shared Line")
box(p, (120, 110, 340, 170), "Core 0", bold=True)
box(p, (620, 110, 840, 170), "Core 1", bold=True)
box(p, (120, 220, 340, 290), "L1 &nbsp; <b>x: S</b>", fill=(0.88, 0.93, 1.0))
box(p, (620, 220, 840, 290), "L1 &nbsp; <b>x: S</b>", fill=(0.88, 0.93, 1.0))
p.draw_line((230, 170), (230, 220), color=NAVY, width=2)
p.draw_line((730, 170), (730, 220), color=NAVY, width=2)
bus_y = 360
p.draw_rect((80, bus_y, 880, bus_y + 28), color=NAVY, fill=(0.80, 0.82, 0.88), width=1.5)
T(p, 80, bus_y + 3, 800, 24, "Shared bus", size=18, bold=True, align="center", color=NAVY)
p.draw_line((280, 290), (280, bus_y), color=NAVY, width=2)       # L1 0 <-> bus
p.draw_line((680, 290), (680, bus_y), color=NAVY, width=2)       # L1 1 <-> bus
arrow(p, (180, 290), (180, bus_y), RED)                          # BusRdX
T(p, 20, 310, 150, 28, "BusRdX(x)", size=18, bold=True, color=RED, align="right")
arrow(p, (780, bus_y), (780, 290), RED)                          # invalidate
T(p, 790, 310, 140, 28, "invalidate", size=18, bold=True, color=RED)
box(p, (200, 440, 400, 495), "Main memory", bold=True)
p.draw_line((300, bus_y + 28), (300, 440), color=NAVY, width=2)
p.draw_rect((470, 430, 910, 490), color=ORANGE, fill=(1.0, 0.95, 0.88), width=2)
T(p, 485, 449, 415, 30, "After the write: Core 0 → M, Core 1 → I", size=20, bold=True)

# 4 Code
p = slide(doc, 4, "A Data Race")
code = [
    "long counter = 0;",
    "",
    "void *worker(void *arg) {",
    "    for (int i = 0; i < 1000000; i++)",
    "        counter++;",
    "    return NULL;",
    "}",
    "",
    "int main(void) {",
    "    pthread_t t[2];",
    "    for (int i = 0; i < 2; i++)",
    "        pthread_create(&t[i], NULL, worker, NULL);",
    "    for (int i = 0; i < 2; i++)",
    "        pthread_join(t[i], NULL);",
    "    printf(\"%ld\\n\", counter);",
    "}",
]
p.draw_rect((30, 100, 650, 100 + 16 * 24 + 20), color=GREY, fill=(0.96, 0.96, 0.97), width=1)
for i, line in enumerate(code):
    y = 120 + i * 24
    p.insert_text((42, y + 12), f"{i + 1:2d}  {line}".rstrip(), fontname="cour", fontsize=16, color=INK)
p.draw_rect((675, 150, 930, 270), color=RED, fill=(1.0, 0.93, 0.93), width=2)
T(p, 690, 165, 225, 150, "Expected: 2000000 · Observed: 1342817 (varies)", size=22, bold=True)

# 5 Table
p = slide(doc, 5, "MESI Transitions (local core)")
cols = [40, 150, 560, 720, 920]
rows = [("State", "Event", "Next state", "Bus action"),
        ("I", "PrRd (others have copy)", "S", "BusRd"),
        ("I", "PrWr", "M", "BusRdX"),
        ("S", "PrWr", "M", "BusUpgr"),
        ("E", "PrWr", "M", "–"),
        ("M", "BusRd (snooped)", "S", "Flush")]
rh, y0 = 52, 110
for r, row in enumerate(rows):
    for c, cell in enumerate(row):
        rect = pymupdf.Rect(cols[c], y0 + r * rh, cols[c + 1], y0 + (r + 1) * rh)
        p.draw_rect(rect, color=NAVY, fill=(0.82, 0.87, 0.96) if r == 0 else (LIGHT if r % 2 == 0 else (1, 1, 1)), width=1.2)
        T(p, rect.x0 + 12, rect.y0 + 12, rect.width - 20, 32, cell, size=20, bold=(r == 0))

# 6 Plot
p = slide(doc, 6, "Speedup vs. Processors")
X0, X1, Y0, Y1 = 120, 880, 450, 110  # Y0 = bottom (value 0), Y1 = top (value 40)
px = lambda pp: X0 + math.log2(pp) / 6 * (X1 - X0)
py = lambda s: Y0 - s / 40 * (Y0 - Y1)
for s in range(0, 41, 10):
    p.draw_line((X0, py(s)), (X1, py(s)), color=(0.88, 0.88, 0.9), width=0.6)
    p.draw_line((X0 - 6, py(s)), (X0, py(s)), color=INK, width=1.2)
    T(p, X0 - 52, py(s) - 10, 42, 22, str(s), size=14, align="right")
for k in range(7):
    pp = 2 ** k
    p.draw_line((px(pp), Y0), (px(pp), Y0 + 6), color=INK, width=1.2)
    T(p, px(pp) - 20, Y0 + 8, 40, 22, str(pp), size=14, align="center")
p.draw_line((X0, Y0), (X1, Y0), color=INK, width=1.8)
p.draw_line((X0, Y0), (X0, Y1), color=INK, width=1.8)
T(p, X0, Y0 + 30, X1 - X0, 24, "Processors p (log<sub>2</sub> scale)", size=16, align="center")
p.insert_text((50, 320), "Speedup S(p)", fontname="helv", fontsize=16, rotate=90)
amd = lambda f, n: 1 / ((1 - f) + f / n)
for f, col in ((0.90, BLUE), (0.99, ORANGE)):
    pts = [(px(2 ** (i / 8)), py(amd(f, 2 ** (i / 8)))) for i in range(0, 49)]
    p.draw_polyline(pts, color=col, width=3)
p.draw_line((X0, py(10)), (X1, py(10)), color=BLUE, width=1.5, dashes="[6 4] 0")
T(p, 640, py(10) - 30, 240, 24, "1/(1−f) = 10", size=16, color=BLUE, bold=True, align="right")
lx, ly = 160, 118
p.draw_rect((lx - 10, ly - 6, lx + 190, ly + 62), color=GREY, fill=(1, 1, 1), width=0.8)
for i, (lab, col) in enumerate((("f = 0.90", BLUE), ("f = 0.99", ORANGE))):
    yy = ly + 12 + i * 30
    p.draw_line((lx, yy), (lx + 40, yy), color=col, width=3)
    T(p, lx + 50, yy - 11, 130, 24, lab, size=16)

OUT.parent.mkdir(parents=True, exist_ok=True)
doc.save(OUT)
print("wrote", OUT, len(doc), "pages")
