"""On-disk storage: one folder per lecture.

    lectures/                     (LECTURES_DIR, default ./lectures)
      <any folder name>/          one lecture; you choose the name
        <slides>.pdf              the slide deck: the PDF directly in the folder (largest one if several)
        context/*.pdf             optional extra material (previous lecture, exercise sheet)
        notes/NNN.md              generated study notes (plain Markdown, editable)
        handout.pdf               written by "Download PDF"
        .tutor/                   app state: meta.json, text.json, usage.json, slides/, chat/, logs/, context-text/

New folders are discovered by scan(). Lecture ids (used in URLs) are a slug of the folder name, stored in meta so
they stay stable. All writes are atomic (tmp file + os.replace). Everything that mutates meta runs on the single
asyncio event-loop thread without an await between read and write.
"""
from __future__ import annotations

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


# ---------------------------------------------------------------- folder index

_index: dict[str, Path] = {}      # lecture id -> lecture folder


def folder(lid: str) -> Path:
    try:
        return _index[lid]
    except KeyError:
        raise KeyError(lid) from None


def lecture_dir(lid: str) -> Path:
    """App-state directory of a lecture (<folder>/.tutor)."""
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


def pick_deck(d: Path) -> Path | None:
    pdfs = _pdfs(d)
    return max(pdfs, key=lambda p: p.stat().st_size) if pdfs else None


def _unique_id(base: str, own: Path) -> str:
    lid, i = base, 2
    while lid in _index and _index[lid] != own:
        lid = f"{base}-{i}"
        i += 1
    return lid


def scan() -> list[str]:
    """Discover lecture folders. Registers new ones (writes meta) and returns their ids so the caller can start
    processing. Folders whose PDF is still being copied are skipped until it has settled."""
    new: list[str] = []
    seen: set[Path] = set()
    for d in sorted(LECTURES.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        seen.add(d)
        meta = read_json(d / APP_DIR / "meta.json")
        if meta:
            lid = meta.get("id") or slugify(d.name)
            other = _index.get(lid)
            if other is not None and other != d and (other / APP_DIR / "meta.json").exists():
                lid = _unique_id(slugify(d.name), d)        # folder was copied: give the copy its own id
                meta["id"] = lid
                write_json(d / APP_DIR / "meta.json", meta)
            _index[lid] = d                                  # (a renamed folder keeps its id)
            continue
        deck = pick_deck(d)
        if not deck or not _settled(deck):
            continue
        lid = _unique_id(slugify(d.name), d)
        _index[lid] = d
        register(lid, deck.name, title=d.name)
        new.append(lid)
    for lid, d in list(_index.items()):                    # folder deleted/renamed outside the app
        if d not in seen:
            _index.pop(lid, None)
    return new


def register(lid: str, deck_name: str, title: str, autostart: bool | None = None) -> dict:
    meta = {"id": lid, "title": title, "deck": deck_name, "created": now_iso(), "n_slides": 0,
            "status": "processing", "prep": default_prep(), "context_files": [],
            "autostart": (os.environ.get("AUTO_PREPARE", "1") != "0") if autostart is None else autostart}
    save_meta(lid, meta)
    return meta


def new_folder_for_upload(filename: str) -> Path:
    """lectures/<pdf name without extension>[ (2)]/ for a lecture uploaded through the browser."""
    base = Path(safe_filename(filename)).stem.strip() or "lecture"
    d, i = LECTURES / base, 2
    while d.exists():
        d = LECTURES / f"{base} ({i})"
        i += 1
    d.mkdir(parents=True)
    return d


def add_to_index(d: Path) -> str:
    lid = _unique_id(slugify(d.name), d)
    _index[lid] = d
    return lid


def delete_lecture(lid: str) -> Path:
    """Move the whole lecture folder to lectures/.deleted/ (recoverable; never touched by scan)."""
    d = folder(lid)
    trash = LECTURES / ".deleted"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{d.name} {datetime.now().strftime('%Y-%m-%d %H%M%S')}"
    shutil.move(str(d), str(dest))
    _index.pop(lid, None)
    return dest


# ---------------------------------------------------------------- paths

def deck_path(lid: str) -> Path:
    return folder(lid) / load_meta(lid)["deck"]


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
