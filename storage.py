"""On-disk storage. One folder per subject; you drop slide PDFs in, the tool writes results next to them.

    lectures/                         (LECTURES_DIR, default ./lectures)
      <Subject>/                      e.g. "DPHPC"; any name (Lectures/ and Recitations/ are created for you)
        Lectures/*.pdf                lecture slides: one PDF = one deck
        Recitations/*.pdf             recitation slides
        Exams/*.pdf                   past exams (+ solutions) for flashcards; any subfolder with "exam" in its name
                                      counts (e.g. "Past Exams"); Exams/ is created once flashcards.md exists
        tutor_instructions.md         optional: your tutor prompt for this subject (notes, chat, flashcards)
        flashcards.md                 optional: your flashcard instructions; turns flashcards on for this subject
        Results/                      written by the tool
          Lectures/<pdf name>/        one per deck ("deck folder")
            notes/NNN.md              study notes (plain Markdown, editable)
            handout.pdf               printable notes, written when the notes are complete
            flashcards.txt            exam flashcards, one per line "Question >> Answer" (subjects with flashcards.md)
            context/*.pdf             optional extra material for this deck
            .tutor/                   app state: meta.json, text.json, usage.json, slides/, chat/, logs/, context-text/
          Recitations/<pdf name>/

scan() discovers new PDFs. A deck's meta records its source PDF name and hash, so renaming the PDF keeps its
results. Deck ids (used in URLs) are fixed when the deck is first seen. All writes are atomic (tmp file +
os.replace). Everything that mutates meta runs on the single asyncio event-loop thread without an await between
read and write.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LECTURES = Path(os.environ.get("LECTURES_DIR", ROOT / "lectures")).expanduser().resolve()
LECTURES.mkdir(parents=True, exist_ok=True)
STATE = ROOT / ".state"           # app-wide state (CLI working dir, plan rate-limit info)
STATE.mkdir(parents=True, exist_ok=True)

APP_DIR = ".tutor"
HANDOUT = "handout.pdf"
USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
SETTLE_SECONDS = 3.0              # a PDF must be unchanged this long before it's picked up (copy in progress)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "lecture")[:60]


def safe_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", base).strip(" .") or "file.pdf"
    return base[:120]


# ---------------------------------------------------------------- deck index

KINDS = {"lecture": "Lectures", "recitation": "Recitations"}   # kind -> source/results subfolder name
RESULTS = "Results"
EXAMS = "Exams"
SUBJECT_TUTOR = "tutor_instructions.md"
SUBJECT_FLASHCARDS = "flashcards.md"
FLASHCARDS = "flashcards.txt"

_index: dict[str, Path] = {}      # deck id -> deck folder (<Subject>/Results/<Kind>/<name>)


def folder(lid: str) -> Path:
    """The deck's results folder."""
    try:
        return _index[lid]
    except KeyError:
        raise KeyError(lid) from None


def lecture_dir(lid: str) -> Path:
    """App-state directory of a deck (<deck folder>/.tutor)."""
    return folder(lid) / APP_DIR


def exists(lid: str) -> bool:
    return lid in _index and (_index[lid] / APP_DIR / "meta.json").exists()


def list_ids() -> list[str]:
    return [lid for lid in _index if exists(lid)]


def _pdfs(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
                      and not p.name.startswith(".") and p.name != HANDOUT)
    except OSError:
        return []


def _settled(p: Path) -> bool:
    try:
        return time.time() - p.stat().st_mtime > SETTLE_SECONDS
    except OSError:
        return False


def _hidden(p: Path) -> bool:
    return p.name.startswith((".", "_"))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pretty_title(stem: str) -> str:
    return stem if " " in stem else re.sub(r"[-_]+", " ", stem).strip() or stem


def subject_dirs() -> list[Path]:
    return sorted((d for d in LECTURES.iterdir() if d.is_dir() and not _hidden(d)), key=lambda d: d.name.lower())


def subject_dir(lid: str) -> Path:
    return folder(lid).parent.parent.parent


def subject_of(lid: str) -> str:
    return subject_dir(lid).name


def source_dir(subject: Path, kind: str) -> Path:
    return subject / KINDS[kind]


def results_dir(subject: Path, kind: str) -> Path:
    return subject / RESULTS / KINDS[kind]


def is_exam_dir(d: Path) -> bool:
    """Past exams live in any subject subfolder with "exam" in its name: Exams/, Past Exams/, old exams/ …"""
    return "exam" in d.name.lower() and d.is_dir() and not _hidden(d)


def exam_dirs(subject: Path) -> list[Path]:
    return sorted(d for d in subject.iterdir() if is_exam_dir(d)) if subject.is_dir() else []


def exam_pdfs(subject: Path) -> list[Path]:
    return [p for d in exam_dirs(subject) for p in _pdfs(d)]


def subject_prompt(subject: Path, name: str) -> str:
    """A subject-level prompt file (tutor_instructions.md / flashcards.md) without HTML comments; "" if absent."""
    p = subject / name
    try:
        text = p.read_text("utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def flashcards_enabled(subject: Path) -> bool:
    return (subject / SUBJECT_FLASHCARDS).is_file()


def subjects() -> list[dict]:
    """[{name, lecture: n, recitation: n}] for every subject folder, including empty ones."""
    counts: dict[tuple[Path, str], int] = {}
    for lid in list_ids():
        key = (subject_dir(lid), load_meta(lid).get("kind", "lecture"))
        counts[key] = counts.get(key, 0) + 1
    return [{"name": d.name, **{k: counts.get((d, k), 0) for k in KINDS}} for d in subject_dirs()]


def loose_pdfs() -> list[str]:
    """PDFs the app ignores: not inside a subject's Lectures/ or Recitations/ folder."""
    out = [p.name for p in _pdfs(LECTURES)]
    allowed = {n.lower() for n in KINDS.values()} | {RESULTS.lower()}
    for subj in subject_dirs():
        out += [f"{subj.name}/{p.name}" for p in _pdfs(subj)]
        for d in sorted(subj.iterdir()):
            if d.is_dir() and not _hidden(d) and d.name.lower() not in allowed and not is_exam_dir(d):
                out += [f"{subj.name}/{d.name}/{p.name}" for p in _pdfs(d)]
    return out


def safe_dirname(name: str) -> str:
    """A folder name from user input; ValueError if nothing usable is left."""
    base = re.sub(r"[^\w .()&+,-]+", "_", name.replace("/", "-").replace("\\", "-")).strip(" ._")
    if not re.search(r"[^\W_]", base):                 # needs at least one letter or digit
        raise ValueError("invalid folder name")
    return base[:80]


def _unique_dir(parent: Path, name: str) -> Path:
    d, i = parent / name, 2
    while d.exists():
        d = parent / f"{name} ({i})"
        i += 1
    return d


def _unique_id(base: str, own: Path) -> str:
    lid, i = base, 2
    while lid in _index and _index[lid] != own:
        lid = f"{base}-{i}"
        i += 1
    return lid


def scan() -> list[str]:
    """Discover decks. Registers new PDFs (creates their deck folder + meta) and returns their ids so the caller can
    start processing. PDFs still being copied are skipped until they have settled."""
    new: list[str] = []
    seen: set[Path] = set()
    for subj in subject_dirs():
        for kind in KINDS:
            src = source_dir(subj, kind)
            src.mkdir(exist_ok=True)                      # show the user where PDFs go
            if flashcards_enabled(subj) and not exam_dirs(subj):
                (subj / EXAMS).mkdir(exist_ok=True)
            res = results_dir(subj, kind)
            known: dict[str, Path] = {}
            orphans: list[tuple[Path, dict]] = []         # results whose source PDF is gone (renamed/removed)
            if res.is_dir():
                for d in sorted(res.iterdir()):
                    meta = read_json(d / APP_DIR / "meta.json") if d.is_dir() and not _hidden(d) else None
                    if not meta:
                        continue
                    if (src / meta.get("source", "")).is_file():
                        known[meta["source"]] = d
                        _keep(d, meta)
                        seen.add(d)
                    else:
                        orphans.append((d, meta))
            for pdf in _pdfs(src):
                if pdf.name in known or not _settled(pdf):
                    continue
                digest = _sha256(pdf)
                match = next((o for o in orphans if o[1].get("sha256") == digest), None)
                if match:                                 # the PDF was renamed: move its results along
                    orphans.remove(match)
                    d, meta = match
                    target = _unique_dir(res, safe_dirname(pdf.stem)) if d.name != safe_dirname(pdf.stem) else d
                    if target != d:
                        os.rename(d, target)
                    meta.update(source=pdf.name, title=pretty_title(pdf.stem))
                    write_json(target / APP_DIR / "meta.json", meta)
                    _keep(target, meta)
                    seen.add(target)
                    continue
                lid = new_deck(subj, kind, pdf, digest)
                seen.add(_index[lid])
                new.append(lid)
    for lid, d in list(_index.items()):                   # deck folder or source PDF removed outside the app
        if d not in seen:
            _index.pop(lid, None)
    return new


def _keep(d: Path, meta: dict) -> None:
    """Index an existing deck folder; a copied folder (same id already indexed elsewhere) gets its own id."""
    lid = meta.get("id") or slugify(d.name)
    other = _index.get(lid)
    if other is not None and other != d and (other / APP_DIR / "meta.json").exists():
        lid = _unique_id(lid, d)
        meta["id"] = lid
        write_json(d / APP_DIR / "meta.json", meta)
    _index[lid] = d


def new_deck(subj: Path, kind: str, pdf: Path, digest: str | None = None, autostart: bool | None = None) -> str:
    """Create the deck folder for a source PDF and register it (status "processing")."""
    d = _unique_dir(results_dir(subj, kind), safe_dirname(pdf.stem))
    (d / APP_DIR).mkdir(parents=True)
    lid = _unique_id(slugify(f"{subj.name} {pdf.stem}" if kind == "lecture" else f"{subj.name} {kind} {pdf.stem}"), d)
    _index[lid] = d
    save_meta(lid, {
        "id": lid, "title": pretty_title(pdf.stem), "kind": kind, "source": pdf.name,
        "sha256": digest or _sha256(pdf), "created": now_iso(), "n_slides": 0, "status": "processing",
        "prep": default_prep(), "context_files": [],
        "autostart": (os.environ.get("AUTO_PREPARE", "1") != "0") if autostart is None else autostart})
    return lid


def save_upload(subject: str, kind: str, filename: str, data: bytes, autostart: bool) -> str:
    """Store a browser upload as <Subject>/<Kind>/<file>.pdf and register it, like a dropped-in PDF."""
    subj = LECTURES / safe_dirname(subject)
    src = source_dir(subj, kind)
    src.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    target, i = src / name, 2
    while target.exists():
        target = src / f"{Path(name).stem} ({i}).pdf"
        i += 1
    atomic_write_bytes(target, data)
    return new_deck(subj, kind, target, autostart=autostart)


def delete_lecture(lid: str) -> Path:
    """Move the source PDF and the deck folder to lectures/.deleted/ (recoverable; never touched by scan)."""
    d, src = folder(lid), deck_path(lid)
    kind = KINDS.get(load_meta(lid).get("kind", "lecture"), "Lectures")
    dest = LECTURES / ".deleted" / f"{subject_of(lid)} - {kind} - {d.name} {datetime.now().strftime('%Y-%m-%d %H%M%S')}"
    dest.mkdir(parents=True)
    if src.exists():
        shutil.move(str(src), str(dest / src.name))
    shutil.move(str(d), str(dest / RESULTS))
    _index.pop(lid, None)
    return dest


# ---------------------------------------------------------------- paths

def deck_path(lid: str) -> Path:
    """The source PDF (<Subject>/Lectures|Recitations/<file>.pdf)."""
    meta = load_meta(lid)
    return source_dir(subject_dir(lid), meta.get("kind", "lecture")) / meta["source"]


def slide_png(lid: str, n: int, size: str = "lg") -> Path:
    suffix = "" if size == "lg" else "_sm"
    return lecture_dir(lid) / "slides" / f"{n:03d}{suffix}.png"


def notes_path(lid: str, n: int) -> Path:
    return folder(lid) / "notes" / f"{n:03d}.md"


def chat_path(lid: str, n: int) -> Path:
    return lecture_dir(lid) / "chat" / f"{n:03d}.json"


def chat_images_dir(lid: str) -> Path:
    return lecture_dir(lid) / "chat" / "images"


def context_dir(lid: str) -> Path:
    return folder(lid) / "context"


def context_text_path(lid: str, name: str) -> Path:
    return lecture_dir(lid) / "context-text" / (name + ".txt")


def handout_path(lid: str) -> Path:
    return folder(lid) / HANDOUT


def flashcards_path(lid: str) -> Path:
    return folder(lid) / FLASHCARDS


# ---------------------------------------------------------------- meta

def default_prep() -> dict:
    return {"done": 0, "total": 0, "running": False, "errors": {}, "generating": [],
            "message": None, "paused_until": None}


def load_meta(lid: str) -> dict:
    meta = read_json(lecture_dir(lid) / "meta.json")
    if meta is None:
        raise KeyError(lid)
    meta.setdefault("prep", default_prep())
    meta.setdefault("context_files", [])
    return meta


def save_meta(lid: str, meta: dict) -> None:
    write_json(lecture_dir(lid) / "meta.json", meta)


def update_meta(lid: str, **changes) -> dict:
    meta = load_meta(lid)
    meta.update(changes)
    save_meta(lid, meta)
    return meta


def update_prep(lid: str, **changes) -> dict:
    meta = load_meta(lid)
    meta["prep"].update(changes)
    save_meta(lid, meta)
    return meta


# ---------------------------------------------------------------- slides, text, notes

def load_text(lid: str) -> list[dict]:
    """[{n, title, text, ocr?}] for every slide."""
    return read_json(lecture_dir(lid) / "text.json", default=[])


def has_notes(lid: str, n: int) -> bool:
    p = notes_path(lid, n)
    return p.exists() and p.stat().st_size > 0


def read_notes(lid: str, n: int) -> str | None:
    p = notes_path(lid, n)
    return p.read_text("utf-8") if p.exists() else None


def write_notes(lid: str, n: int, markdown: str) -> None:
    atomic_write_text(notes_path(lid, n), markdown)


def count_notes(lid: str, n_slides: int) -> int:
    return sum(1 for n in range(1, n_slides + 1) if has_notes(lid, n))


# ---------------------------------------------------------------- context files

def context_pdfs(lid: str) -> list[Path]:
    return _pdfs(context_dir(lid))


def context_texts(lid: str) -> list[tuple[str, str]]:
    """[(name, extracted_text)] for the context PDFs currently in the folder (alphabetical)."""
    out = []
    for p in context_pdfs(lid):
        t = context_text_path(lid, p.name)
        if t.exists():
            out.append((p.name, t.read_text("utf-8")))
    return out


# ---------------------------------------------------------------- chat

def load_chat(lid: str, n: int) -> list[dict]:
    return read_json(chat_path(lid, n), default=[])


def append_chat(lid: str, n: int, message: dict) -> None:
    msgs = load_chat(lid, n)
    msgs.append(message)
    write_json(chat_path(lid, n), msgs)


def clear_chat(lid: str, n: int) -> None:
    chat_path(lid, n).unlink(missing_ok=True)


# ---------------------------------------------------------------- usage

def _empty_totals() -> dict:
    return {"calls": 0, **{f: 0 for f in USAGE_FIELDS}, "cost_usd": 0.0}


def load_usage(lid: str) -> dict:
    u = read_json(lecture_dir(lid) / "usage.json")
    if not u:
        u = {"totals": _empty_totals(), "by_kind": {}, "calls": []}
    return u


def record_usage(lid: str, kind: str, slide: int | None, model: str, usage: dict | None,
                 cost_usd: float | None, duration_ms: int | None) -> None:
    u = load_usage(lid)
    usage = usage or {}
    entry = {"ts": now_iso(), "kind": kind, "slide": slide, "model": model,
             **{f: int(usage.get(f) or 0) for f in USAGE_FIELDS},
             "cost_usd": float(cost_usd or 0.0), "duration_ms": duration_ms}
    u["calls"].append(entry)
    for bucket in (u["totals"], u["by_kind"].setdefault(kind, _empty_totals())):
        bucket["calls"] += 1
        for f in USAGE_FIELDS:
            bucket[f] += entry[f]
        bucket["cost_usd"] = round(bucket["cost_usd"] + entry["cost_usd"], 6)
    write_json(lecture_dir(lid) / "usage.json", u)


def usage_summary(lid: str) -> dict:
    u = load_usage(lid)
    return {**u["totals"], "by_kind": u["by_kind"]}


# ---------------------------------------------------------------- request log (verification)

def log_request(lid: str, record: dict) -> None:
    p = lecture_dir(lid) / "logs" / "requests.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), "t": time.time(), **record}, ensure_ascii=False) + "\n")
