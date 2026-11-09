"""Tiny stand-in for the real backend, used ONLY to visually/behaviourally test static/ without spending
Claude usage. Serves static/ plus a fake API following docs/API.md.

Run: uv run python tests/frontend/mock_server.py [port]      (default 8765)
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import fitz  # pymupdf (project dependency)

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
HERE = Path(__file__).resolve().parent

FIXTURE = re.search(r'id="fixture">(.*?)</script>', (HERE / "render_test.html").read_text(), re.S).group(1)
N = 6
LID = "lecture-07-caches-3f9a"
LID2 = "lecture-08-vm-77aa"

TITLES = ["Cache basics", "Cache misses", "Associativity", "Replacement policies", "Write policies", "Summary"]
NOTES = {
    1: FIXTURE,
    2: "## Short\n\nA hit costs $t_h$. Takeaway: keep misses rare.",
    3: "\n\n".join(
        f"### Section {i}\n\nParagraph {i} with math $f_{i}(x)=x^{i}+\\frac{{1}}{{{i}}}$ and more words to fill the page up. "
        "This sentence is intentionally verbose so that the print fitting loop has to shrink the font to fit. " * 2
        for i in range(1, 15)
    ),
    4: "## Replacement\n\n1. LRU\n2. FIFO\n3. Random\n\n> \"Evict the least recently used line.\"\n\n⚠ check: LRU is costly in hardware.\n\n**Takeaway:** pick pseudo-LRU.",
    6: "## Summary\n\n- hit time\n- miss rate\n\nTakeaway: $\\text{AMAT}=t_h+m\\,t_m$.",
}
# slide 5 notes become ready after a few polls (simulates generation)
poll_counts: dict[int, int] = {}
chats: dict[int, list] = {}
regen_state: dict[int, int] = {}
prep_calls = {"n": 0}


def make_pdf() -> fitz.Document:
    doc = fitz.open()
    for i in range(N):
        page = doc.new_page(width=960, height=540)
        page.draw_rect(fitz.Rect(0, 0, 960, 540), color=None, fill=(0.97, 0.97, 1))
        page.draw_rect(fitz.Rect(0, 0, 960, 70), color=None, fill=(0.2, 0.3, 0.7))
        page.insert_text((30, 48), f"{TITLES[i]}", fontsize=30, color=(1, 1, 1))
        page.insert_text((60, 150), f"Slide {i + 1}", fontsize=60, color=(0.1, 0.1, 0.3))
        page.insert_text((60, 230), "AMAT = t_hit + miss_rate * t_miss", fontsize=28, color=(0.2, 0.2, 0.2))
        page.draw_rect(fitz.Rect(600, 150, 900, 450), color=(0.8, 0.2, 0.2), fill=(1, 0.9, 0.9), width=3)
        page.insert_text((640, 310), "diagram", fontsize=36, color=(0.6, 0.1, 0.1))
    return doc


DOC = make_pdf()


def png(n: int, width: int) -> bytes:
    page = DOC[n - 1]
    zoom = width / page.rect.width
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")


def usage(calls=12):
    return {"calls": calls, "input_tokens": 340, "cache_creation_input_tokens": 9000, "cache_read_input_tokens": 88000,
            "output_tokens": 15000, "cost_usd": 0.41, "by_kind": {}}


def summary(lid: str) -> dict:
    if lid == LID:
        return {"id": LID, "subject": "DPHPC", "title": "Lecture 07 caches", "filename": "lecture-07-caches.pdf", "kind": "lecture", "source": "/home/me/lectures/DPHPC/Lectures/lecture-07-caches.pdf", "folder": "/home/me/lectures/DPHPC/Results/Lectures/lecture-07-caches", "created": "2026-11-09T12:00:00Z",
                "n_slides": N, "status": "incomplete" if False else "ready",
                "prep": {"done": N, "total": N, "running": False, "errors": {}, "generating": [], "message": None, "paused_until": None},
                "context_files": [{"name": "prev-lecture.pdf", "chars": 41000}], "usage": usage()}
    done = min(N, prep_calls["n"] // 2)
    st = "preparing" if done < N else "ready"
    return {"id": LID2, "subject": "Architecture", "title": "Lecture 08 virtual memory", "filename": "recitation-08-vm.pdf", "kind": "recitation", "source": "/home/me/lectures/Architecture/Recitations/recitation-08-vm.pdf", "folder": "/home/me/lectures/Architecture/Results/Recitations/recitation-08-vm", "created": "2026-11-10T12:00:00Z",
            "n_slides": N, "status": st,
            "prep": {"done": done, "total": N, "running": st == "preparing", "errors": {}, "generating": [done + 1] if st == "preparing" else [],
                     "message": "Usage limit reached — resumes 14:00" if st == "preparing" and done == 3 else None, "paused_until": None},
            "context_files": [], "usage": usage(3)}


def notes_for(lid: str, n: int) -> dict:
    t = TITLES[n - 1]
    if lid == LID2:
        done = min(N, prep_calls["n"] // 2)
        if n > done:
            return {"n": n, "title": t, "markdown": None, "status": "generating" if n == done + 1 else "pending", "error": None}
    if n in regen_state and regen_state[n] > 0:
        regen_state[n] -= 1
        return {"n": n, "title": t, "markdown": None, "status": "generating", "error": None}
    if n == 5:
        c = poll_counts.get(5, 0)
        poll_counts[5] = c + 1
        if c < 3:
            return {"n": n, "title": t, "markdown": None, "status": "pending" if c < 1 else "generating", "error": None}
        return {"n": n, "title": t, "markdown": "## Write policies\n\nWrite-back vs write-through.", "status": "done", "error": None}
    return {"n": n, "title": t, "markdown": NOTES.get(n, "x"), "status": "done", "error": None}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def send_bytes(self, code, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self.file(STATIC / "index.html")
        if re.fullmatch(r"/lecture/[^/]+", path):
            return self.file(STATIC / "reader.html")
        if re.fullmatch(r"/lecture/[^/]+/print", path):
            return self.file(STATIC / "print.html")
        if path.startswith("/static/"):
            return self.file(STATIC / path[len("/static/"):])
        if path == "/api/status":
            return self.send_json({"playwright_pdf": True, "notes_model": "m", "chat_model": "m", "concurrency": 3, "lectures_dir": "/home/me/lectures", "loose_pdfs": ["DPHPC/lecture09.pdf", "slides.pdf"],
                                   "rate_limit": {"five_hour": {"utilization": 0.45, "resets_at": 1794224400, "status": "allowed"},
                                                  "seven_day": {"utilization": 0.5, "resets_at": 1794224400, "status": "allowed"}}})
        if path == "/api/subjects":
            return self.send_json([{"name": "Analysis", "lecture": 0, "recitation": 0}, {"name": "Architecture", "lecture": 0, "recitation": 1}, {"name": "DPHPC", "lecture": 1, "recitation": 0}])
        if path == "/api/lectures":
            return self.send_json([summary(LID2), summary(LID)])
        m = re.fullmatch(r"/api/lectures/([^/]+)(/.*)?", path)
        if m and m.group(1) in (LID, LID2):
            lid, rest = m.group(1), m.group(2) or ""
            if rest == "":
                d = summary(lid)
                done = d["prep"]["done"]
                d["slides"] = [{"n": i + 1, "title": TITLES[i], "has_notes": (i + 1 <= done) if lid == LID2 else True} for i in range(N)]
                return self.send_json(d)
            if rest == "/progress":
                if lid == LID2:
                    prep_calls["n"] += 1
                d = summary(lid)
                return self.send_json({"status": d["status"], "prep": d["prep"], "usage": d["usage"]})
            mm = re.fullmatch(r"/slides/(\d+)/image", rest)
            if mm:
                size = (re.search(r"size=(\w+)", urlparse(self.path).query) or [0, "lg"])[1]
                return self.send_bytes(200, png(int(mm.group(1)), 1600 if size == "lg" else 320), "image/png")
            mm = re.fullmatch(r"/slides/(\d+)/notes", rest)
            if mm:
                return self.send_json(notes_for(lid, int(mm.group(1))))
            if rest == "/notes":
                return self.send_json([{"n": i, "title": TITLES[i - 1], "markdown": NOTES.get(i, "- point")} for i in range(1, N + 1)])
            mm = re.fullmatch(r"/slides/(\d+)/chat", rest)
            if mm:
                return self.send_json({"messages": chats.get(int(mm.group(1)), [])})
            if rest == "/pdf":
                return self.send_bytes(501, b"{}", "application/json")
        self.send_json({"detail": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        mm = re.fullmatch(r"/api/lectures/[^/]+/slides/(\d+)/chat", path)
        if mm:
            chats.pop(int(mm.group(1)), None)
            return self.send_json({"ok": True})
        self.send_json({"ok": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        mm = re.fullmatch(r"/api/lectures/([^/]+)/slides/(\d+)/regenerate", path)
        if mm:
            regen_state[int(mm.group(2))] = 2
            return self.send_json({"ok": True})
        mm = re.fullmatch(r"/api/lectures/([^/]+)/slides/(\d+)/ask", path)
        if mm:
            return self.ask(int(mm.group(2)), json.loads(body or b"{}"))
        if path == "/api/lectures":
            if b'name="subject"' not in body:
                return self.send_json({"detail": "subject is required"}, 400)
            return self.send_json(summary(LID2))
        if path.endswith("/prepare"):
            return self.send_json(summary(LID2))
        self.send_json({"detail": "mock: not supported"}, 400)

    def ask(self, n: int, req: dict):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def frame(ev, data):
            payload = f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        now = "2026-11-09T12:00:00Z"
        region = None
        if req.get("region"):
            region = {"rect": req["region"], "image": f"/api/lectures/{LID}/slides/{n}/image?size=sm"}
        user = {"role": "user", "content": req["question"], "ts": now, "region": region}
        chats.setdefault(n, []).append(user)
        frame("start", {"slide": n, "user_message": user})
        time.sleep(1.0)  # "thinking"
        answer = (f"Here is slide **{n}**. The miss rate is $m = 1 - h$.\n\n$$\\text{{AMAT}} = t_h + m\\, t_m$$\n\n"
                  "| a | b |\n|---|---|\n| $|x|$ | costs $5 |\n\n```python\nprint('$HOME')\n```\n\nTakeaway: keep $m$ small.")
        pos = 0
        while pos < len(answer):
            frame("delta", {"text": answer[pos:pos + 9]})
            pos += 9
            time.sleep(0.15)
        msg = {"role": "assistant", "content": answer, "ts": now, "region": None}
        chats[n].append(msg)
        frame("done", {"message": msg, "usage": {}})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True

    def file(self, p: Path):
        p = p.resolve()
        if STATIC not in p.parents and p != STATIC / "index.html" or not p.is_file():
            return self.send_json({"detail": "not found"}, 404)
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2",
                 ".woff": "font/woff", ".ttf": "font/ttf"}.get(p.suffix, "application/octet-stream")
        self.send_bytes(200, p.read_bytes(), ctype)


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"mock backend on http://127.0.0.1:{port}")
    srv.serve_forever()
