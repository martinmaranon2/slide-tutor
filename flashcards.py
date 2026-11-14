"""Exam flashcards per deck, for subjects that opt in by having a flashcards.md.

    lectures/<Subject>/
      flashcards.md            your flashcard instructions (its presence turns flashcards on for the subject)
      tutor_instructions.md    optional: your tutor prompt for the subject (also used here)
      Exams/*.pdf              past exams and solutions: the model's reference for what is tested and how
      Results/<Kind>/<deck>/flashcards.txt     one card per line: "Question >> Answer"

Lectures get their cards automatically once all their notes exist and Exams/ holds at least one PDF; recitations,
and lectures without exams, on request. A finished file is never regenerated automatically. One deck at a time.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import jobs
import llm
import pdfproc
import prompts
import storage

log = logging.getLogger("slide_tutor.flashcards")

COUNT = max(1, int(os.environ.get("FLASHCARDS_COUNT", "50")))
MODEL = os.environ.get("FLASHCARDS_MODEL", llm.NOTES_MODEL)
EFFORT = os.environ.get("FLASHCARDS_EFFORT", "high")
EXAM_CHARS = int(os.environ.get("FLASHCARDS_EXAM_CHARS", "300000"))   # ~75k tokens of past exams at most
SEP = ">>"
EXAM_TEXT = storage.STATE / "exam-text"

_tasks: dict[str, asyncio.Task] = {}
_phase: dict[str, str] = {}          # lid -> "queued" | "generating" (in memory only; a restart starts over)
_sem: asyncio.Semaphore | None = None


# ---------------------------------------------------------------- cards file

def _split(line: str) -> tuple[str, str] | None:
    """Split at the separator: the first ">>" outside $...$ math (the first one at all if every one is inside).
    Any other ">>" becomes \\gg inside math and ≫ outside, so the saved line has exactly one separator."""
    spots = []                                   # (index, inside_math)
    i, dollars = 0, 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == "$":
            dollars += 1
        elif line.startswith(SEP, i):
            spots.append((i, dollars % 2 == 1))
            i += len(SEP)
            continue
        i += 1
    if not spots:
        return None
    cut = next((s for s in spots if not s[1]), spots[0])[0]

    def fix(part: str, offset: int) -> str:
        out, last = [], 0
        for pos, in_math in spots:
            if pos == cut or not offset <= pos < offset + len(part):
                continue
            spaced = part[pos - offset + len(SEP):pos - offset + len(SEP) + 1] in (" ", "")
            out += [part[last:pos - offset], (r"\gg" if spaced else r"\gg ") if in_math else "≫"]
            last = pos - offset + len(SEP)
        return "".join(out) + part[last:]

    q, a = line[:cut], line[cut + len(SEP):]
    return fix(q, 0).strip(), fix(a, cut + len(SEP)).strip()


def parse(text: str) -> list[tuple[str, str]]:
    """Cards from model output or the saved file: one per line with the separator. Other lines are dropped and
    stray numbering or bullets removed."""
    cards = []
    for raw in text.splitlines():
        line = re.sub(r"^(?:[-*•]\s+|\d{1,3}[.)]\s+)", "", raw.strip())
        qa = _split(line) if SEP in line else None
        if qa and qa[0] and qa[1]:
            cards.append(qa)
    return cards


def read_cards(lid: str) -> list[tuple[str, str]]:
    p = storage.flashcards_path(lid)
    try:
        return parse(p.read_text("utf-8"))
    except FileNotFoundError:
        return []


def _count(lid: str) -> int:
    p = storage.flashcards_path(lid)
    try:
        return sum(1 for line in p.read_text("utf-8").splitlines() if SEP in line)
    except FileNotFoundError:
        return 0


# ---------------------------------------------------------------- past exams

def exam_texts(subject: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """[(name, text)] of the subject's exam PDFs within EXAM_CHARS (newest-looking names first), and the names
    left out for size. Extracted text is cached in .state/exam-text by file name, size and mtime."""
    EXAM_TEXT.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(storage.exam_pdfs(subject), key=lambda p: [int(t) if t.isdigit() else t.lower()
                                                            for t in re.split(r"(\d+)", p.name)], reverse=True)
    out, skipped, used = [], [], 0
    for pdf in pdfs:
        st = pdf.stat()
        key = hashlib.sha256(f"{pdf.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:24]
        cache = EXAM_TEXT / f"{key}.txt"
        if cache.exists():
            text = cache.read_text("utf-8")
        else:
            try:
                text = pdfproc.extract_pdf_text(pdf, ocr=True)
            except Exception as e:
                log.warning("exam %s unreadable: %s", pdf.name, e)
                skipped.append(pdf.name)
                continue
            storage.atomic_write_text(cache, text)
        if not text.strip() or (out and used + len(text) > EXAM_CHARS):
            skipped.append(pdf.name)
            continue
        out.append((pdf.name, text[:EXAM_CHARS]))
        used += len(text)
    return sorted(out, key=lambda x: x[0].lower()), skipped


# ---------------------------------------------------------------- status

def info(lid: str, meta: dict | None = None) -> dict:
    meta = meta or storage.load_meta(lid)
    subj = storage.subject_dir(lid)
    fc = meta.get("flashcards") or {}
    enabled = storage.flashcards_enabled(subj)
    count = _count(lid)
    exams_available = len(storage.exam_pdfs(subj)) if enabled else 0
    resets_at = fc.get("resets_at")
    if not enabled:
        status = "off"
    elif lid in _phase:
        status = _phase[lid]
    elif fc.get("status") == "paused":
        status = "paused" if resets_at and resets_at > time.time() else "queued"   # the watcher restarts it
    elif fc.get("status") == "error":
        status = "error"
    elif count:
        status = "ready"
    elif meta.get("kind", "lecture") != "lecture":
        status = "idle"
    elif meta.get("status") != "ready":
        status = "waiting_notes"
    elif not exams_available:
        status = "waiting_exams"
    else:
        status = "queued"                                                   # the watcher starts it within seconds
    return {"enabled": enabled, "status": status, "count": count, "target": COUNT,
            "updated": fc.get("updated") if count else None, "error": fc.get("error") if status == "error" else None,
            "resets_at": resets_at if status == "paused" else None, "exams": fc.get("exams", []) if count else [],
            "exams_available": exams_available}


def running(lid: str) -> bool:
    t = _tasks.get(lid)
    return t is not None and not t.done()


# ---------------------------------------------------------------- generation

def start(lid: str) -> bool:
    """Queue (re)generation for one deck. False if the subject has no flashcards.md, the deck has no slides yet,
    or it is already queued/running."""
    meta = storage.load_meta(lid)
    if running(lid) or not storage.flashcards_enabled(storage.subject_dir(lid)) or not meta.get("n_slides") \
            or meta.get("status") in ("processing", "error"):
        return False
    _phase[lid] = "queued"
    task = jobs.spawn(_run(lid), lid)
    _tasks[lid] = task

    def done(t: asyncio.Task) -> None:
        if _tasks.get(lid) is t:
            _tasks.pop(lid, None)
            _phase.pop(lid, None)
    task.add_done_callback(done)
    return True


def _set(lid: str, **fc) -> None:
    if not storage.exists(lid):
        return
    meta = storage.load_meta(lid)
    meta["flashcards"] = {**(meta.get("flashcards") or {}), **fc}
    storage.save_meta(lid, meta)


async def _run(lid: str) -> None:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(1)
    async with _sem:
        if not storage.exists(lid):
            return
        _phase[lid] = "generating"
        subj = storage.subject_dir(lid)
        try:
            exams, skipped = await asyncio.to_thread(exam_texts, subj)
            slides = storage.load_text(lid)
            system = prompts.flashcards_system(subj, exams)
            content, included = prompts.flashcards_request(lid, slides, COUNT)
            storage.log_request(lid, {"kind": "flashcards", "model": MODEL, "effort": EFFORT,
                                      "included": {**included, "exams": [n for n, _ in exams],
                                                   "exams_left_out": skipped},
                                      "request": llm.describe_content(system, content)})
            cards: list[tuple[str, str]] = []
            for attempt in range(2):                 # one more try if the format came back wrong
                res = await llm.run_with_retries(system, content, model=MODEL, effort=EFFORT)
                storage.record_usage(lid, "flashcards", None, res.model, res.usage, res.cost_usd, res.duration_ms)
                cards = parse(res.text)
                if len(cards) >= max(5, int(COUNT * 0.6)):
                    break
                storage.atomic_write_text(storage.lecture_dir(lid) / "flashcards_raw.txt", res.text)
                log.warning("flashcards %s: only %d cards in the expected format (attempt %d)", lid, len(cards),
                            attempt + 1)
            if not cards:
                raise llm.FatalError("The answer contained no lines in the 'Question >> Answer' format.")
            if not storage.exists(lid):
                return
            storage.atomic_write_text(storage.flashcards_path(lid), "".join(f"{q} {SEP} {a}\n" for q, a in cards))
            _set(lid, status="ready", error=None, resets_at=None, updated=storage.now_iso(), model=MODEL,
                 exams=[n for n, _ in exams], exams_left_out=skipped)
            u = res.usage
            log.info("flashcards %s: %d cards, %d exam(s); in=%s cache_r=%s out=%s %.0fs", lid, len(cards),
                     len(exams), u.get("input_tokens"), u.get("cache_read_input_tokens"), u.get("output_tokens"),
                     res.duration_ms / 1000)
        except asyncio.CancelledError:
            raise
        except llm.UsageLimitError as e:
            when = datetime.fromtimestamp(e.resets_at).strftime("%a %H:%M") if e.resets_at else "later"
            log.warning("flashcards %s paused by the plan limit until %s", lid, when)
            _set(lid, status="paused", resets_at=e.resets_at or int(time.time()) + 15 * 60)
        except Exception as e:
            log.exception("flashcards %s failed", lid)
            _set(lid, status="error", error=str(e)[:300])


def auto() -> None:
    """Called by the folder watcher: start decks that are due (finished lectures with exams, expired pauses)."""
    for lid in storage.list_ids():
        if running(lid):
            continue
        try:
            meta = storage.load_meta(lid)
        except KeyError:
            continue
        if info(lid, meta)["status"] == "queued":
            start(lid)
