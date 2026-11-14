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


def tutor_instructions(subject) -> str:
    """prompts/tutor_instructions.md (all subjects) followed by <Subject>/tutor_instructions.md, if present."""
    return "\n\n".join(x for x in [_read_prompt("tutor_instructions.md"),
                                    storage.subject_prompt(subject, storage.SUBJECT_TUTOR)] if x)


_PRECEDENCE = {
    "notes": "Follow them for sources, notation, depth and explanation style. Where they ask for a different answer "
             "structure or length, the study-notes format above takes precedence (the notes are printed one page "
             "per slide).",
    "chat": "Follow them; they take precedence over the general answer-length guideline above.",
}


def _static_context(lid: str, slides: list[dict], task: str) -> str:
    meta = storage.load_meta(lid)
    parts = []
    tutor = tutor_instructions(storage.subject_dir(lid))
    if tutor:
        parts.append(f"<tutor_instructions>\nThe student's own instructions for this course. {_PRECEDENCE[task]}\n\n"
                     f"{tutor}\n</tutor_instructions>")
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
        _static_context(lid, slides, "notes"),
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
    return "\n\n".join([CHAT_RULES, _read_prompt("output_format.md"), _static_context(lid, slides, "chat")])


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


# ---------------------------------------------------------------- flashcards

FLASHCARD_RULES = """You write exam-preparation flashcards for one lecture of a university course. You get:
- the student's flashcard instructions: follow them exactly, including the card format;
- the student's tutor instructions for the course (sources, notation, depth), if any;
- past exams of the course, often with solutions: use them to judge which concepts get tested, how questions are \
phrased and how hard they are. Write new cards in that style about THIS lecture's material; past-exam topics that \
this lecture doesn't cover are not part of the deck;
- the lecture: every slide's extracted text plus study notes generated from the slide images (the notes describe \
diagrams and work through the formulas; where notes and slide disagree, the slide wins).

Output rules (the file is imported line by line):
- One card per line: `Question >> Answer`. Nothing else: no numbering, bullets, headings, blank lines, preamble or \
closing remarks.
- `>>` appears exactly once per line, as the separator. Never use it inside a question or answer (write ≫ for \
"much greater than").
- A card never spans lines. Math as inline LaTeX in $...$ (no $$ display math), otherwise plain text.
- Every question must make sense on its own, without the slides at hand: name the method, quantity or setting it \
is about instead of saying "this slide" or "the figure"."""


def flashcards_system(subject, exams: list[tuple[str, str]]) -> str:
    """Identical for every deck of a subject (as long as the files don't change), so it is prompt-cached."""
    parts = [FLASHCARD_RULES,
             f"<flashcard_instructions>\n{storage.subject_prompt(subject, storage.SUBJECT_FLASHCARDS)}\n"
             "</flashcard_instructions>"]
    tutor = tutor_instructions(subject)
    if tutor:
        parts.append(f"<tutor_instructions>\n{tutor}\n</tutor_instructions>")
    if exams:
        body = "\n\n".join(f'<exam file="{name}">\n{text}\n</exam>' for name, text in exams)
        parts.append(f"<past_exams>\n{body}\n</past_exams>")
    else:
        parts.append("<past_exams>\n(none provided: judge exam relevance from the slides' emphasis and learning "
                     "objectives)\n</past_exams>")
    return "\n\n".join(parts)


def _trim_notes(notes: str, limit: int) -> str:
    """Notes shortened to about `limit` chars: leading paragraphs plus the "Takeaway:" and "⚠ check:" paragraphs."""
    if len(notes) <= limit:
        return notes
    paras = [p for p in re.split(r"\n\s*\n", notes.strip()) if p.strip()]
    keep_always = [p for p in paras if re.match(r"\W*(Takeaway|⚠)", p.strip())]
    out, used = [], sum(len(p) for p in keep_always)
    for p in paras:
        if p in keep_always:
            continue
        if used + len(p) > limit and out:
            out.append("[…]")
            break
        out.append(p)
        used += len(p)
    return "\n\n".join(out + keep_always)


LECTURE_CHARS = 220_000   # budget for slide text + notes in one flashcards request (~55k tokens)


def flashcards_request(lid: str, slides: list[dict], count: int) -> tuple[list[dict], dict]:
    meta = storage.load_meta(lid)
    text_total = sum(len(s["text"] or "") for s in slides)
    per_slide = max(800, (LECTURE_CHARS - text_total) // max(1, len(slides)))
    blocks, notes_chars, trimmed = [], 0, 0
    for s in slides:
        notes = storage.read_notes(lid, s["n"]) or ""
        # slides that are mostly a picture get more of their notes: the notes are the only description of it
        limit = per_slide * (2 if len((s["text"] or "").strip()) < 150 else 1)
        short = _trim_notes(notes.strip(), limit) if notes else ""
        trimmed += short != notes.strip()
        notes_chars += len(short)
        blocks.append(f'<slide number="{s["n"]}" title="{s["title"]}"{_src(s)}>\n{s["text"] or "(no text layer)"}\n'
                      + (f"<study_notes>\n{short}\n</study_notes>\n" if short else "") + "</slide>")
    kind = "recitation (exercise session) slides" if meta.get("kind") == "recitation" else "lecture slides"
    lecture = (f'<lecture subject="{storage.subject_of(lid)}" title="{meta["title"]}" type="{kind}" '
               f'slides="{len(slides)}">\n' + "\n".join(blocks) + "\n</lecture>")
    task = (f"Write {count} flashcards for this lecture now ({count} is the student's standard per lecture; if the "
            "material clearly has fewer distinct exam-relevant ideas, write fewer rather than near-duplicates). Cover "
            "the whole lecture, weighted by what the past exams test. Output only the cards, one per line.")
    content = [text_block(lecture), text_block(task)]
    info = {"slides": len(slides), "slide_text_chars": text_total, "notes_chars": notes_chars,
            "notes_trimmed": trimmed, "notes_limit_per_slide": per_slide}
    return content, info
