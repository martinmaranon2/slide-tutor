"""Prompt assembly. Static, per-lecture parts (style, tutor instructions, outline, context files) live in the
system prompt so they are byte-identical across all slides of a lecture and get prompt-cached; per-slide
content comes after it in the user message (stable per-slide parts first, the question last)."""
from __future__ import annotations

import re

import storage
from llm import image_block, text_block

PROMPTS = storage.ROOT / "prompts"
NEIGHBOURS = 2


def _read_prompt(name: str) -> str:
    p = PROMPTS / name
    text = p.read_text("utf-8") if p.exists() else ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def _static_context(lid: str, slides: list[dict]) -> str:
    meta = storage.load_meta(lid)
    parts = []
    tutor = _read_prompt("tutor_instructions.md")
    if tutor:
        parts.append(f"<tutor_instructions>\n{tutor}\n</tutor_instructions>")
    outline = "\n".join(f"{s['n']}. {s['title']}" for s in slides)
    kind = "recitation (exercise session) slides" if meta.get("kind") == "recitation" else "lecture slides"
    parts.append(f'<lecture subject="{storage.subject_of(lid)}" title="{meta["title"]}" type="{kind}" '
                 f'slides="{len(slides)}">\n<outline>\n{outline}\n</outline>\n</lecture>')
    ctx = storage.context_texts(lid)
    if ctx:
        files = "\n\n".join(f'<context_file name="{name}">\n{text}\n</context_file>' for name, text in ctx)
        parts.append("<reference_material>\nExtra course material the student attached to this lecture "
                     "(e.g. the previous lecture or an exercise sheet). Use it for connections and notation, "
                     f"and say when you draw on it.\n{files}\n</reference_material>")
    return "\n\n".join(parts)


def notes_system(lid: str, slides: list[dict]) -> str:
    return "\n\n".join([
        _read_prompt("notes_style.md"),
        _read_prompt("output_format.md"),
        _static_context(lid, slides),
    ])


CHAT_RULES = """You are my tutor for this course. I'm reading the lecture slides and asking you about the slide I'm \
currently looking at. Every question arrives with that slide attached (image, extracted text, my study notes for \
it), plus the neighbouring slides' text and our earlier conversation about this slide. "This", "here", "the \
formula", etc. refer to the current slide unless I say otherwise.

- Answer the actual question first, then explain. Usually 80–300 words; longer only if the question needs it.
- Show every intermediate math step; never "clearly" or "it follows". Double-check arithmetic.
- Use the slide's notation exactly. If the slide or the notes look wrong or ambiguous, say so with "⚠ check:".
- Mark facts that are not on the slide or in the notes with "(background)".
- If I attach a cropped region ("the part I mean"), focus on that region.
- If I ask for an exam-style question: ask exactly one, at real exam level, and don't reveal the solution. \
When I then send an answer ("Check my answer"), grade it: what's right, what's wrong or missing, then a concise \
model answer."""


def chat_system(lid: str, slides: list[dict]) -> str:
    return "\n\n".join([CHAT_RULES, _read_prompt("output_format.md"), _static_context(lid, slides)])


def _src(s: dict) -> str:
    return ' source="OCR of the slide image, may contain recognition errors"' if s.get("ocr") else ""


def _slide_text_xml(s: dict, tag: str = "slide") -> str:
    return f'<{tag} number="{s["n"]}" title="{s["title"]}"{_src(s)}>\n{s["text"] or "(no text layer)"}\n</{tag}>'


def _neighbours(slides: list[dict], n: int) -> tuple[str, list[int]]:
    nums = [k for k in range(n - NEIGHBOURS, n + NEIGHBOURS + 1) if k != n and 1 <= k <= len(slides)]
    if not nums:
        return "", []
    body = "\n".join(_slide_text_xml(slides[k - 1]) for k in nums)
    return f"<neighbouring_slides>\n{body}\n</neighbouring_slides>", nums


def notes_request(lid: str, n: int, slides: list[dict]) -> tuple[list[dict], dict]:
    s = slides[n - 1]
    png = storage.slide_png(lid, n).read_bytes()
    neigh, nums = _neighbours(slides, n)
    body = "\n\n".join(x for x in [
        f'<current_slide number="{n}" title="{s["title"]}">\n<extracted_text{_src(s)}>\n{s["text"] or "(no text layer)"}\n'
        f"</extracted_text>\n</current_slide>",
        neigh,
        f"Write the study notes for slide {n} of {len(slides)} now. The image above is slide {n}; the extracted text "
        "can miss diagram content and math layout, so read the image for diagrams, plots and formulas. Output only "
        "the notes.",
    ] if x)
    content = [text_block(f"Slide {n} of {len(slides)}: {s['title']}"), image_block(png), text_block(body)]
    info = {"slide_image": str(storage.slide_png(lid, n).relative_to(storage.lecture_dir(lid))),
            "slide_text_chars": len(s["text"]), "neighbours": nums}
    return content, info


def chat_request(lid: str, n: int, slides: list[dict], question: str, history: list[dict],
                 region_png: bytes | None) -> tuple[list[dict], dict]:
    s = slides[n - 1]
    png = storage.slide_png(lid, n).read_bytes()
    notes = storage.read_notes(lid, n)
    neigh, nums = _neighbours(slides, n)
    slide_part = "\n\n".join(x for x in [
        f'<current_slide number="{n}" title="{s["title"]}">\n<extracted_text{_src(s)}>\n{s["text"] or "(no text layer)"}\n'
        f"</extracted_text>\n</current_slide>",
        f"<study_notes_for_this_slide>\n{notes or '(not generated yet)'}\n</study_notes_for_this_slide>",
        neigh,
    ] if x)
    content = [text_block(f"Current slide: {n} of {len(slides)}: {s['title']}"), image_block(png),
               text_block(slide_part)]
    if history:
        turns = []
        for m in history:
            role = "student" if m["role"] == "user" else "tutor"
            extra = " [attached a cropped region of the slide]" if m.get("region") else ""
            turns.append(f'<turn role="{role}">{extra}\n{m["content"]}\n</turn>')
        content.append(text_block("<previous_conversation_about_this_slide>\n" + "\n".join(turns)
                                  + "\n</previous_conversation_about_this_slide>"))
    if region_png:
        content.append(text_block("The part of the slide I mean (cropped from the slide, high resolution):"))
        content.append(image_block(region_png))
    content.append(text_block(f"<question>\n{question}\n</question>"))
    info = {"slide_image": str(storage.slide_png(lid, n).relative_to(storage.lecture_dir(lid))),
            "slide_text_chars": len(s["text"]), "notes_chars": len(notes or ""), "notes_included": bool(notes),
            "neighbours": nums, "history_turns": len(history), "region": bool(region_png)}
    return content, info
