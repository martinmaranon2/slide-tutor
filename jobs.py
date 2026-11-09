"""Background work: PDF processing after upload, and the resumable notes pre-generation job.

* Resumable: a slide is skipped when notes/NNN.md exists (written atomically), so restarts never regenerate.
* Concurrency: one global semaphore (CONCURRENCY, default 4) shared by all lectures and regenerate calls.
* Cache warm-up: the first missing slide runs alone so the shared system prompt is cached before fan-out.
* Transient errors retry with backoff inside llm.run_with_retries; a plan usage limit pauses the job and
  schedules an automatic resume at the reset time.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import llm
import pdfproc
import prompts
import storage

log = logging.getLogger("slide_tutor.jobs")

CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "4")))
_sem: asyncio.Semaphore | None = None
_jobs: dict[str, asyncio.Task] = {}
_side_tasks: set[asyncio.Task] = set()
_resume_timers: dict[str, asyncio.Task] = {}
_generating: dict[str, set[int]] = {}
_lecture_tasks: dict[str, set[asyncio.Task]] = {}   # side tasks per lecture (regenerate, chat answers, processing)
_handout_timers: dict[str, asyncio.Task] = {}
_handout_sem: asyncio.Semaphore | None = None
on_ready = None   # async fn(lid) set by app: writes Results/.../handout.pdf once a deck's notes are complete


def sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(CONCURRENCY)
    return _sem


def spawn(coro, lid: str | None = None) -> asyncio.Task:
    """Run a background task. With lid, it is cancelled when that lecture is deleted."""
    t = asyncio.create_task(coro)
    _side_tasks.add(t)
    t.add_done_callback(_side_tasks.discard)
    if lid:
        owned = _lecture_tasks.setdefault(lid, set())
        owned.add(t)
        t.add_done_callback(owned.discard)
    return t


def is_running(lid: str) -> bool:
    t = _jobs.get(lid)
    return t is not None and not t.done()


def generating(lid: str) -> set[int]:
    return _generating.setdefault(lid, set())


# ---------------------------------------------------------------- upload processing

async def process_upload(lid: str) -> None:
    storage.update_meta(lid, status="processing")
    try:
        slides = await asyncio.to_thread(pdfproc.process_deck, lid)
    except Exception as e:  # corrupt PDF etc.
        if not storage.exists(lid):
            return  # deleted while processing
        log.exception("processing %s failed", lid)
        meta = storage.load_meta(lid)
        meta["status"] = "error"
        meta["prep"]["message"] = f"Could not process PDF: {e}"
        storage.save_meta(lid, meta)
        return
    if not storage.exists(lid):
        return
    meta = storage.load_meta(lid)
    meta["n_slides"] = len(slides)
    meta["status"] = "ready_to_prepare"
    meta["prep"]["total"] = len(slides)
    storage.save_meta(lid, meta)
    log.info("processed %s: %d slides", lid, len(slides))
    await sync_context(lid)
    if storage.load_meta(lid).get("autostart", True):
        start(lid)


# ---------------------------------------------------------------- lecture folders

async def sync_context(lid: str) -> None:
    """Extract text of context/*.pdf files that are new or changed; keep meta.context_files in sync with the folder."""
    files = []
    for pdf in storage.context_pdfs(lid):
        txt = storage.context_text_path(lid, pdf.name)
        try:
            stale = not txt.exists() or txt.stat().st_mtime < pdf.stat().st_mtime
            if stale:
                text = await asyncio.to_thread(pdfproc.extract_pdf_text, pdf)
                storage.atomic_write_text(txt, text)
            files.append({"name": pdf.name, "chars": txt.stat().st_size})
        except Exception as e:
            log.warning("context file %s/%s unreadable: %s", lid, pdf.name, e)
    if not storage.exists(lid):
        return
    meta = storage.load_meta(lid)
    if meta.get("context_files") != files:
        meta["context_files"] = files
        storage.save_meta(lid, meta)


async def scan_folders() -> None:
    """Pick up new lecture folders (and new context files) dropped into lectures/."""
    for lid in storage.scan():
        log.info("new lecture folder: %s -> %s", storage.folder(lid).name, lid)
        spawn(process_upload(lid), lid)
    for lid in storage.list_ids():
        if storage.load_meta(lid)["status"] != "processing":
            await sync_context(lid)


async def watch_folders(interval: float = 5.0) -> None:
    while True:
        try:
            await scan_folders()
        except Exception:
            log.exception("folder scan failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- notes job

def start(lid: str) -> bool:
    if is_running(lid):
        return False
    meta = storage.load_meta(lid)
    if meta["status"] in ("processing", "error"):
        return False
    timer = _resume_timers.pop(lid, None)
    if timer:
        timer.cancel()
    task = asyncio.create_task(_run(lid))
    _jobs[lid] = task
    task.add_done_callback(lambda _t: _jobs.pop(lid, None) if _jobs.get(lid) is _t else None)
    return True


def cancel(lid: str) -> None:
    """Stop all work for a lecture (prep job, resume timer, regenerate, chat answers, processing)."""
    for d in (_jobs, _resume_timers, _handout_timers):
        t = d.pop(lid, None)
        if t:
            t.cancel()
    for t in list(_lecture_tasks.pop(lid, ())):
        t.cancel()


async def _generate_slide(lid: str, n: int, slides: list[dict], system: str) -> None:
    content, info = prompts.notes_request(lid, n, slides)
    storage.log_request(lid, {"kind": "notes", "slide": n, "model": llm.NOTES_MODEL, "effort": llm.NOTES_EFFORT,
                              "included": info, "request": llm.describe_content(system, content)})
    gen = generating(lid)
    added = n not in gen          # regenerate() marks the slide itself before waiting for the semaphore
    gen.add(n)
    try:
        def on_retry(attempt, delay, err):
            storage.update_prep(lid, message=f"Slide {n}: {err} (retry {attempt} in {delay:.0f}s)")

        res = await llm.run_with_retries(system, content, model=llm.NOTES_MODEL, effort=llm.NOTES_EFFORT,
                                         on_retry=on_retry)
        storage.write_notes(lid, n, res.text.strip() + "\n")
        storage.record_usage(lid, "notes", n, res.model, res.usage, res.cost_usd, res.duration_ms)
        meta = storage.load_meta(lid)
        meta["prep"]["errors"].pop(str(n), None)
        meta["prep"]["done"] = storage.count_notes(lid, meta["n_slides"])
        storage.save_meta(lid, meta)
        u = res.usage
        log.info("notes %s #%d: in=%s cache_w=%s cache_r=%s out=%s %.1fs", lid, n, u.get("input_tokens"),
                 u.get("cache_creation_input_tokens"), u.get("cache_read_input_tokens"), u.get("output_tokens"),
                 res.duration_ms / 1000)
    finally:
        if added:
            gen.discard(n)


async def _run(lid: str) -> None:
    await sync_context(lid)
    meta = storage.load_meta(lid)
    slides = storage.load_text(lid)
    total = len(slides)
    system = prompts.notes_system(lid, slides)  # built once -> byte-identical prefix for every slide
    todo = [n for n in range(1, total + 1) if not storage.has_notes(lid, n)]
    meta["status"] = "preparing"
    meta["prep"].update(done=total - len(todo), total=total, running=True, message=None, paused_until=None)
    storage.save_meta(lid, meta)
    log.info("prepare %s: %d/%d slides already done, %d to go", lid, total - len(todo), total, len(todo))

    limit: llm.UsageLimitError | None = None

    async def one(n: int) -> None:
        nonlocal limit
        if limit:
            return
        async with sem():
            if limit or storage.has_notes(lid, n) or n in generating(lid):
                return            # (a slide being regenerated right now is finished by that call)
            try:
                await _generate_slide(lid, n, slides, system)
            except llm.UsageLimitError as e:
                limit = e
            except llm.LLMError as e:
                _record_error(lid, n, str(e))
            except Exception as e:
                log.exception("slide %d failed", n)
                _record_error(lid, n, f"{type(e).__name__}: {e}")

    cancelled = False
    try:
        if todo:
            await one(todo[0])               # warm the prompt cache with one request first
            await asyncio.gather(*(one(n) for n in todo[1:]))
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if storage.exists(lid):              # (not deleted meanwhile)
            _finish_run(lid, total, cancelled, limit)
    if storage.exists(lid):
        log.info("prepare %s finished: %s", lid, storage.load_meta(lid)["status"])


def _finish_run(lid: str, total: int, cancelled: bool, limit: "llm.UsageLimitError | None") -> None:
    meta = storage.load_meta(lid)
    meta["prep"]["running"] = False
    meta["prep"]["done"] = storage.count_notes(lid, total)
    if cancelled:                            # server shutdown: stay "preparing" so the next start resumes
        storage.save_meta(lid, meta)
    elif limit:
        resets = limit.resets_at
        when = datetime.fromtimestamp(resets).strftime("%a %H:%M") if resets else "later"
        meta["status"] = "paused"
        meta["prep"]["paused_until"] = resets
        meta["prep"]["message"] = f"Plan usage limit reached. Resumes automatically at {when}."
        storage.save_meta(lid, meta)
        _schedule_resume(lid, resets)
    else:
        done = meta["prep"]["done"] == total
        meta["status"] = "ready" if done else "incomplete"
        meta["prep"]["message"] = None if done else f"{total - meta['prep']['done']} slide(s) failed; click Prepare to retry."
        storage.save_meta(lid, meta)
        if done:
            schedule_handout(lid)


def schedule_handout(lid: str, delay: float = 10.0) -> None:
    """(Re)write the deck's handout.pdf once things have been quiet for `delay` seconds (debounced; one at a time)."""
    global _handout_sem
    if on_ready is None:
        return
    if _handout_sem is None:
        _handout_sem = asyncio.Semaphore(1)
    old = _handout_timers.pop(lid, None)
    if old:
        old.cancel()

    async def later():
        await asyncio.sleep(delay)
        async with _handout_sem:
            if storage.exists(lid) and storage.load_meta(lid)["status"] == "ready":
                try:
                    await on_ready(lid)
                except Exception as e:
                    log.warning("handout for %s failed: %s", lid, e)
        if _handout_timers.get(lid) is asyncio.current_task():
            _handout_timers.pop(lid, None)

    _handout_timers[lid] = spawn(later(), lid)


def _record_error(lid: str, n: int, msg: str) -> None:
    if not storage.exists(lid):
        return
    meta = storage.load_meta(lid)
    meta["prep"]["errors"][str(n)] = msg
    storage.save_meta(lid, meta)


def _schedule_resume(lid: str, resets_at: int | None) -> None:
    async def later():
        delay = (resets_at - time.time() + 60) if resets_at else 15 * 60
        await asyncio.sleep(max(30.0, delay))
        _resume_timers.pop(lid, None)
        if storage.exists(lid):
            start(lid)

    old = _resume_timers.pop(lid, None)
    if old:
        old.cancel()
    _resume_timers[lid] = asyncio.create_task(later())


# ---------------------------------------------------------------- single-slide regenerate

def regenerate(lid: str, n: int) -> bool:
    if n in generating(lid):
        return False
    generating(lid).add(n)  # mark immediately so the UI sees "generating" before the semaphore frees up

    async def go():
        try:
            slides = storage.load_text(lid)
            system = prompts.notes_system(lid, slides)
            async with sem():
                await _generate_slide(lid, n, slides, system)
        except llm.LLMError as e:
            _record_error(lid, n, str(e))
        except Exception as e:
            if not storage.exists(lid):
                return
            log.exception("regenerate %d failed", n)
            _record_error(lid, n, f"{type(e).__name__}: {e}")
        finally:
            generating(lid).discard(n)
            if storage.exists(lid):
                meta = storage.load_meta(lid)
                meta["prep"]["done"] = storage.count_notes(lid, meta["n_slides"])
                if (not is_running(lid) and meta["status"] in ("ready", "incomplete", "ready_to_prepare", "paused")
                        and meta["prep"]["done"] == meta["n_slides"]):
                    meta["status"] = "ready"
                    meta["prep"].update(message=None, paused_until=None)
                    timer = _resume_timers.pop(lid, None)
                    if timer:
                        timer.cancel()
                storage.save_meta(lid, meta)
                if meta["status"] == "ready":
                    schedule_handout(lid)

    spawn(go(), lid)
    return True


# ---------------------------------------------------------------- startup

def kill_orphaned_cli() -> int:
    """If the server was hard-killed, its in-flight Claude Code subprocesses are re-parented to init (ppid 1)
    and would keep spending plan usage on answers nobody reads. Kill only those: same bundled CLI path as ours."""
    import signal
    import subprocess

    try:
        import claude_agent_sdk
        marker = str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[1] == "1" and parts[2].startswith(marker):
            try:
                os.kill(int(parts[0]), signal.SIGTERM)
                killed += 1
            except OSError:
                pass
    if killed:
        log.info("killed %d orphaned Claude Code subprocess(es) from a previous run", killed)
    return killed


def resume_all() -> None:
    """Called on server start: finish interrupted processing / prep jobs, re-arm paused ones."""
    kill_orphaned_cli()
    new = storage.scan()
    for lid in new:
        log.info("new lecture folder: %s -> %s", storage.folder(lid).name, lid)
        spawn(process_upload(lid), lid)
    for lid in storage.list_ids():
        if lid in new:
            continue
        try:
            meta = storage.load_meta(lid)
        except KeyError:
            continue
        status = meta.get("status")
        if status == "processing":
            spawn(process_upload(lid), lid)
        elif status == "preparing":
            log.info("resuming interrupted prep for %s", lid)
            start(lid)
        elif status == "paused":
            _schedule_resume(lid, meta["prep"].get("paused_until"))
        elif status == "ready" and not storage.handout_path(lid).exists():
            schedule_handout(lid)
        if meta["prep"].get("running") and status != "preparing":
            storage.update_prep(lid, running=False)
