"""slide-tutor: lecture- and slide-specific study assistant.

Run:  uv run app.py      then open http://localhost:8000
Model calls go through the Claude Agent SDK -> your local Claude Code login (Claude subscription).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

# Use the Claude Code subscription login, not an API key that happens to be in the environment.
if os.environ.get("SLIDE_TUTOR_ALLOW_API_KEY") != "1" and os.environ.pop("ANTHROPIC_API_KEY", None):
    print("note: ignoring ANTHROPIC_API_KEY so your Claude subscription login is used "
          "(set SLIDE_TUTOR_ALLOW_API_KEY=1 to keep it)")

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import flashcards
import jobs
import llm
import pdfproc
import prompts
import storage

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("slide_tutor")
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
STATIC = storage.ROOT / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    jobs.after_scan = flashcards.auto
    jobs.resume_all()
    watcher = asyncio.create_task(jobs.watch_folders())
    log.info("lecture folders: %s", storage.LECTURES)
    yield
    watcher.cancel()


class NoStaleFrontend:
    """Pages and static files get "Cache-Control: no-cache": the browser revalidates them on every load (a cheap 304
    locally) instead of guessing a freshness lifetime and running an old library.js for hours after an update."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == "/" or path.startswith(("/static/", "/lecture/"))):
            return await self.inner(scope, receive, send)

        async def send_no_cache(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": headers + [(b"cache-control", b"no-cache")]}
            await send(message)
        await self.inner(scope, receive, send_no_cache)


app = FastAPI(title="slide-tutor", lifespan=lifespan)
app.add_middleware(NoStaleFrontend)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- helpers

def _lecture(lid: str) -> dict:
    try:
        return storage.load_meta(lid)
    except KeyError:
        raise HTTPException(404, "lecture not found")


def _slide(meta: dict, n: int) -> None:
    if not 1 <= n <= max(meta.get("n_slides") or 0, 0):
        raise HTTPException(404, "slide not found")


def summary(lid: str) -> dict:
    meta = storage.load_meta(lid)
    prep = dict(meta["prep"])
    if meta.get("n_slides"):
        prep["done"] = storage.count_notes(lid, meta["n_slides"])
        prep["total"] = meta["n_slides"]
    prep["running"] = jobs.is_running(lid)
    prep["generating"] = sorted(jobs.generating(lid))
    return {
        "id": meta["id"], "title": meta["title"], "filename": meta.get("source"), "created": meta["created"],
        "subject": storage.subject_of(lid), "kind": meta.get("kind", "lecture"),
        "folder": str(storage.folder(lid)), "source": str(storage.deck_path(lid)),
        "n_slides": meta.get("n_slides") or 0, "status": meta["status"], "prep": prep,
        "context_files": meta.get("context_files", []), "usage": storage.usage_summary(lid),
        "flashcards": flashcards.info(lid, meta),
    }


async def _read_pdf_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, f"{f.filename or 'file'} is not a PDF")
    return data


async def _read_context_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    """Read and validate all context uploads first, so a bad file rejects the request before anything is saved."""
    return [(f.filename or "context.pdf", await _read_pdf_upload(f)) for f in files if f.filename]


def _save_context(lid: str, filename: str, data: bytes) -> None:
    """Store a context PDF in <lecture folder>/context/ (text is extracted by jobs.sync_context)."""
    name = storage.safe_filename(filename)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    base, i = name, 2
    while (storage.context_dir(lid) / name).exists():
        name = f"{Path(base).stem}-{i}{Path(base).suffix}"
        i += 1
    storage.atomic_write_bytes(storage.context_dir(lid) / name, data)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


# ---------------------------------------------------------------- pages

@app.get("/")
async def page_library():
    return FileResponse(STATIC / "index.html")


@app.get("/lecture/{lid}")
async def page_reader(lid: str):
    _lecture(lid)
    return FileResponse(STATIC / "reader.html")


@app.get("/lecture/{lid}/print")
async def page_print(lid: str):
    _lecture(lid)
    return FileResponse(STATIC / "print.html")


@app.get("/lecture/{lid}/flashcards")
async def page_flashcards(lid: str):
    _lecture(lid)
    return FileResponse(STATIC / "flashcards.html")


# ---------------------------------------------------------------- status

_pdf_ok: bool | None = None
_pdf_lock = asyncio.Lock()


async def playwright_available() -> bool:
    global _pdf_ok
    async with _pdf_lock:
        if _pdf_ok is None:
            try:
                from playwright.async_api import async_playwright
                async with async_playwright() as p:
                    browser = await p.chromium.launch()
                    await browser.close()
                _pdf_ok = True
            except Exception as e:
                log.info("server-side PDF export disabled: %s", str(e).splitlines()[0] if str(e) else e)
                _pdf_ok = False
    return _pdf_ok


@app.get("/api/status")
async def status():
    return {"playwright_pdf": await playwright_available(), "notes_model": llm.NOTES_MODEL,
            "chat_model": llm.CHAT_MODEL, "concurrency": jobs.CONCURRENCY, "lectures_dir": str(storage.LECTURES),
            "loose_pdfs": storage.loose_pdfs(),
            "rate_limit": _current_rate_limits() or None, "cli": llm.last_init or None}


def _current_rate_limits() -> dict:
    """Drop windows that have already reset (their utilization is stale)."""
    now = time.time()
    return {k: v for k, v in llm.rate_limits.items() if not v.get("resets_at") or v["resets_at"] > now}


# ---------------------------------------------------------------- lectures

@app.get("/api/lectures")
async def list_lectures():
    out = [summary(lid) for lid in storage.list_ids()]
    out.sort(key=lambda s: s["created"], reverse=True)
    return out


@app.get("/api/subjects")
async def list_subjects():
    return storage.subjects()


@app.post("/api/lectures")
async def upload(file: UploadFile = File(...), context: list[UploadFile] = File(default=[]),
                 subject: str = Form(""), kind: str = Form("lecture"), autostart: str = Form("true")):
    try:
        storage.safe_dirname(subject)
    except ValueError:
        raise HTTPException(400, "Choose a subject (the folder the slides go into).")
    if kind not in storage.KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(storage.KINDS)}")
    data = await _read_pdf_upload(file)
    ctx = await _read_context_uploads(context)
    # saved to <Subject>/Lectures|Recitations/<file>.pdf, exactly like a PDF dropped in by hand
    lid = storage.save_upload(subject, kind, file.filename or "slides.pdf", data,
                              autostart=autostart.lower() not in ("false", "0", "no", "off"))
    for name, blob in ctx:
        _save_context(lid, name, blob)
    jobs.spawn(jobs.process_upload(lid), lid)
    return summary(lid)


@app.get("/api/lectures/{lid}")
async def get_lecture(lid: str):
    _lecture(lid)
    s = summary(lid)
    s["slides"] = [{"n": t["n"], "title": t["title"], "has_notes": storage.has_notes(lid, t["n"])}
                   for t in storage.load_text(lid)]
    return s


@app.delete("/api/lectures/{lid}")
async def delete_lecture(lid: str):
    _lecture(lid)
    jobs.cancel(lid)
    dest = storage.delete_lecture(lid)
    log.info("moved %s to %s", lid, dest)
    return {"ok": True, "moved_to": str(dest)}


@app.post("/api/lectures/{lid}/context")
async def add_context(lid: str, context: list[UploadFile] = File(...)):
    _lecture(lid)
    for name, blob in await _read_context_uploads(context):
        _save_context(lid, name, blob)
    await jobs.sync_context(lid)
    return summary(lid)


@app.delete("/api/lectures/{lid}/context/{name}")
async def remove_context(lid: str, name: str):
    _lecture(lid)
    safe = storage.safe_filename(name)
    for p in (storage.context_dir(lid) / safe, storage.context_text_path(lid, safe)):
        p.unlink(missing_ok=True)
    await jobs.sync_context(lid)
    return summary(lid)


@app.post("/api/lectures/{lid}/prepare")
async def prepare(lid: str):
    meta = _lecture(lid)
    if meta["status"] == "processing":
        raise HTTPException(409, "PDF is still being processed")
    if meta["status"] == "error":                     # e.g. the PDF was replaced with a readable one: retry
        jobs.spawn(jobs.process_upload(lid), lid)
        return summary(lid)
    jobs.start(lid)
    return summary(lid)


# ---------------------------------------------------------------- flashcards

@app.get("/api/lectures/{lid}/flashcards")
async def get_flashcards(lid: str):
    meta = _lecture(lid)
    return {"id": lid, "title": meta["title"], "subject": storage.subject_of(lid), "kind": meta.get("kind", "lecture"),
            "flashcards": flashcards.info(lid, meta),
            "cards": [{"q": q, "a": a} for q, a in flashcards.read_cards(lid)]}


@app.post("/api/lectures/{lid}/flashcards")
async def make_flashcards(lid: str):
    meta = _lecture(lid)
    if not storage.flashcards_enabled(storage.subject_dir(lid)):
        raise HTTPException(409, f"Flashcards are off for this subject: add {storage.SUBJECT_FLASHCARDS} to "
                                 f"the {storage.subject_of(lid)} folder")
    if flashcards.running(lid):
        raise HTTPException(409, "Flashcards are already being written")
    if meta["status"] in ("processing", "error") or not meta.get("n_slides"):
        raise HTTPException(409, "The slides haven't been processed yet")
    flashcards.start(lid)
    return summary(lid)


@app.get("/api/lectures/{lid}/flashcards.txt")
async def flashcards_file(lid: str):
    meta = _lecture(lid)
    p = storage.flashcards_path(lid)
    if not p.exists():
        raise HTTPException(404, "No flashcards yet")
    return FileResponse(p, media_type="text/plain; charset=utf-8",
                        filename=f"{storage.safe_filename(meta['title'])} flashcards.txt")


@app.get("/api/lectures/{lid}/progress")
async def progress(lid: str):
    _lecture(lid)
    s = summary(lid)
    return {"status": s["status"], "prep": s["prep"], "usage": s["usage"]}


# ---------------------------------------------------------------- slides & notes

@app.get("/api/lectures/{lid}/slides/{n}/image")
async def slide_image(lid: str, n: int, size: str = "lg"):
    meta = _lecture(lid)
    _slide(meta, n)
    p = storage.slide_png(lid, n, "sm" if size == "sm" else "lg")
    if not p.exists():
        raise HTTPException(404, "not rendered yet")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


def _notes_payload(lid: str, meta: dict, n: int, title: str) -> dict:
    md = storage.read_notes(lid, n)
    err = meta["prep"]["errors"].get(str(n))
    if n in jobs.generating(lid):
        status = "generating"
    elif md:
        status = "done"
    elif err:
        status = "error"
    else:
        status = "pending"
    return {"n": n, "title": title, "markdown": md, "status": status, "error": err if status == "error" else None}


@app.get("/api/lectures/{lid}/slides/{n}/notes")
async def slide_notes(lid: str, n: int):
    meta = _lecture(lid)
    _slide(meta, n)
    title = storage.load_text(lid)[n - 1]["title"]
    return _notes_payload(lid, meta, n, title)


@app.post("/api/lectures/{lid}/slides/{n}/regenerate")
async def regenerate(lid: str, n: int):
    meta = _lecture(lid)
    _slide(meta, n)
    jobs.regenerate(lid, n)
    return {"ok": True}


@app.get("/api/lectures/{lid}/notes")
async def all_notes(lid: str):
    _lecture(lid)
    return [{"n": t["n"], "title": t["title"], "markdown": storage.read_notes(lid, t["n"])}
            for t in storage.load_text(lid)]


# ---------------------------------------------------------------- chat

@app.get("/api/lectures/{lid}/slides/{n}/chat")
async def get_chat(lid: str, n: int):
    meta = _lecture(lid)
    _slide(meta, n)
    return {"messages": storage.load_chat(lid, n)}


@app.delete("/api/lectures/{lid}/slides/{n}/chat")
async def clear_chat(lid: str, n: int):
    meta = _lecture(lid)
    _slide(meta, n)
    storage.clear_chat(lid, n)
    return {"ok": True}


@app.get("/api/lectures/{lid}/chat-images/{name}")
async def chat_image(lid: str, name: str):
    _lecture(lid)
    p = storage.chat_images_dir(lid) / storage.safe_filename(name)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


class AskBody(BaseModel):
    question: str
    region: list[float] | None = None


@app.post("/api/lectures/{lid}/slides/{n}/ask")
async def ask(lid: str, n: int, body: AskBody):
    meta = _lecture(lid)
    _slide(meta, n)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "empty question")

    region_png, region_info = None, None
    if body.region:
        if len(body.region) != 4:
            raise HTTPException(400, "region must be [x0,y0,x1,y1]")
        x0, y0, x1, y1 = body.region
        rect = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        try:
            region_png = await asyncio.to_thread(pdfproc.render_region, lid, n, rect)
        except ValueError as e:
            raise HTTPException(400, str(e))
        name = f"{n:03d}-{int(time.time() * 1000)}.png"
        storage.atomic_write_bytes(storage.chat_images_dir(lid) / name, region_png)
        region_info = {"rect": [round(v, 4) for v in rect], "image": f"/api/lectures/{lid}/chat-images/{name}"}

    slides = storage.load_text(lid)
    history = storage.load_chat(lid, n)
    user_msg = {"role": "user", "content": question, "ts": storage.now_iso(), "region": region_info}
    storage.append_chat(lid, n, user_msg)

    system = prompts.chat_system(lid, slides)
    content, info = prompts.chat_request(lid, n, slides, question, history, region_png)
    storage.log_request(lid, {"kind": "chat", "slide": n, "model": llm.CHAT_MODEL, "effort": llm.CHAT_EFFORT,
                              "included": info, "request": llm.describe_content(system, content)})

    queue: asyncio.Queue = asyncio.Queue()

    async def produce():
        # Runs independently of the HTTP connection: the answer is persisted even if the browser goes away.
        try:
            res = await llm.run_with_retries(system, content, model=llm.CHAT_MODEL, effort=llm.CHAT_EFFORT,
                                             on_delta=lambda t: queue.put_nowait(("delta", {"text": t})))
            msg = {"role": "assistant", "content": res.text, "ts": storage.now_iso(), "region": None}
            storage.append_chat(lid, n, msg)
            storage.record_usage(lid, "chat", n, res.model, res.usage, res.cost_usd, res.duration_ms)
            queue.put_nowait(("done", {"message": msg, "usage": {k: res.usage.get(k) for k in storage.USAGE_FIELDS}}))
        except llm.UsageLimitError as e:
            when = datetime.fromtimestamp(e.resets_at).strftime("%a %H:%M") if e.resets_at else "later"
            _drop_unanswered(lid, n, user_msg)
            queue.put_nowait(("error", {"message": f"Plan usage limit reached. Try again after {when}."}))
        except asyncio.CancelledError:              # lecture deleted or server shutting down
            _drop_unanswered(lid, n, user_msg)
            queue.put_nowait(("error", {"message": "Cancelled."}))
            raise
        except Exception as e:
            log.exception("chat failed")
            _drop_unanswered(lid, n, user_msg)
            queue.put_nowait(("error", {"message": str(e) or type(e).__name__}))
        finally:
            queue.put_nowait(None)

    jobs.spawn(produce(), lid)

    async def stream():
        yield _sse("start", {"slide": n, "user_message": user_msg})
        while (item := await queue.get()) is not None:
            yield _sse(*item)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _drop_unanswered(lid: str, n: int, user_msg: dict) -> None:
    """Remove a question whose answer failed (wherever it is: another question may have been asked meanwhile)."""
    if not storage.exists(lid):
        return
    msgs = storage.load_chat(lid, n)
    if user_msg in msgs:
        msgs.remove(user_msg)
        storage.write_json(storage.chat_path(lid, n), msgs)


# ---------------------------------------------------------------- server-side PDF

async def export_handout(lid: str) -> bytes:
    """Render the print view to PDF with headless Chromium and save it as <deck folder>/handout.pdf."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{PORT}/lecture/{lid}/print", wait_until="networkidle")
            await page.wait_for_selector('body[data-ready="1"]', timeout=180_000)
            pdf = await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            await browser.close()
    if storage.exists(lid):
        storage.atomic_write_bytes(storage.handout_path(lid), pdf)   # the "result" copy in Results/
    return pdf


async def _auto_handout(lid: str) -> None:
    if await playwright_available():
        await export_handout(lid)
        log.info("wrote %s", storage.handout_path(lid))


jobs.on_ready = _auto_handout


@app.get("/api/lectures/{lid}/pdf")
async def handout_pdf(lid: str):
    _lecture(lid)
    if not await playwright_available():
        return JSONResponse({"error": "Playwright/Chromium not installed (uv run playwright install chromium)"},
                            status_code=501)
    pdf = await export_handout(lid)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{lid}-handout.pdf"'})


if __name__ == "__main__":
    print(f"slide-tutor → http://localhost:{PORT}  (notes: {llm.NOTES_MODEL}, chat: {llm.CHAT_MODEL})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
