# slide-tutor

A local study assistant for lecture slides. Drop a slide PDF into a subject folder and it writes study notes for
**every slide** ahead of time. While you read, a chat panel is **tied to the slide on screen**, so you just type
the question; the slide, its text and its notes are sent along automatically.

It runs on your machine and uses your **Claude subscription** (Pro/Max) through your logged-in Claude Code.
No API key, no per-token billing; usage counts against your plan's limits.

## What matters

- **One folder per subject: your slides in, results out.** Drop PDFs into `lectures/<Subject>/Lectures/` or
  `lectures/<Subject>/Recitations/`; each PDF is picked up within a few seconds. The results land in
  `lectures/<Subject>/Results/`: per deck, the notes as plain Markdown (`notes/001.md`, …) and a printable
  `handout.pdf` that is written automatically once all notes are done. The library groups everything by subject,
  with lectures and recitations in separate sections.
- **Notes are pre-generated and resumable.** Stop the server whenever you like; on restart it continues with the
  missing slides only. Finished notes are never regenerated unless you click **Regenerate** on that slide.
- **Every question carries the slide.** The chat request always contains the current slide's image (1600 px), its
  extracted text, its study notes, the neighbouring slides' text, the lecture outline and your earlier Q&A on that
  slide. You can also drag a rectangle on the slide to point at one part.
- **It's careful about your plan.** Prompts are built so the per-lecture part is cached, notes run 4 at a time,
  and if your plan's usage limit is hit the job pauses and resumes by itself when the window resets. The header
  shows your 5-hour and 7-day utilisation.
- **Exam flashcards, per subject.** Add your own flashcard instructions and past exams to a subject, and every
  lecture gets 50 `Question >> Answer` cards written in the style of those exams (see [Flashcards](#flashcards-and-per-subject-instructions)).
- **Nothing leaves your machine except the model calls.** Lectures, notes and chats are files on disk;
  `lectures/` is git-ignored.

## Setup

1. **Claude Code installed and logged in** with your Claude account: run `claude` once and use `/login`.
   `claude auth status` should show `"authMethod": "claude.ai"`.
2. **[uv](https://docs.astral.sh/uv/)**: `brew install uv`. It fetches Python 3.12 and all dependencies on first run.
3. Optional:
   - `uv run playwright install chromium` enables the server-side **Download PDF** button. The browser's own
     *Print → Save as PDF* works without it.
   - `brew install tesseract` adds OCR for slides that are only a picture (e.g. a pasted recap slide), so they get a
     real title and text.

```bash
git clone git@github.com:martinmaranon2/slide-tutor.git
cd slide-tutor
uv run app.py            # → open http://localhost:8000
```

Without uv (Python ≥ 3.12): `pip install -r requirements.txt && python app.py`.

## How to use it

### 1. Add slides

Either **drop PDFs into a subject folder**:

```
lectures/
  DPHPC/                                ← a subject: any name
    Lectures/                           ← lecture slides (created for you)
      lecture08.pdf
      lecture09.pdf
    Recitations/                        ← recitation / exercise-session slides
      recitation08.pdf
    Results/                            ← written by the app
      Lectures/
        lecture08/
          notes/001.md …
          handout.pdf
      Recitations/
        recitation08/ …
  Analysis II/
    Lectures/  Recitations/  Results/
```

To start a new subject, just create its folder (`lectures/Analysis II/`); `Lectures/` and `Recitations/` appear
inside it within a few seconds.

Or **upload in the browser**: the library page (`/`) has a form with the subject (pick one or type a new name) and
the type (lecture or recitation slides). It saves the PDF into the same folders.

The app renders the slides, extracts the text and starts writing notes. A progress bar shows *n / total*.

- **Context files** (previous lecture, exercise sheet, script chapter) can be attached per deck in the library, or
  dropped into the deck's `Results/…/<deck>/context/` folder. Their text is given to the model as reference material
  for the notes and the chat, and the notes say when they draw on it. Add them before notes are generated, or
  regenerate afterwards.
- To add context first and start later, run with `AUTO_PREPARE=0` (or untick *Start preparing notes immediately*
  when uploading), then press **Prepare lecture**.
- PDFs anywhere else (directly in `lectures/`, in a subject folder, or another subfolder except an exams folder) are
  ignored; the library lists them so you can move them.
- The library has a tab per subject, so you only see that subject's decks; it remembers your last choice.

### 2. Read

Open a lecture from the library.

- ←/→ (or the buttons) to move, the jump box for a slide number, the thumbnail strip at the bottom. The app
  remembers where you stopped in each lecture.
- **Drag the divider** between the slide and the right-hand panel to make the panel wider (slide smaller) or
  narrower. The width is remembered; double-click the divider to reset it.
- **Notes** tab: the study notes for this slide, with math (KaTeX), code and tables rendered. **Regenerate** rewrites
  just this slide's notes.

### 3. Ask

The **Ask** tab is a separate thread per slide.

- `/` focuses the input. Enter sends; Shift+Enter adds a new line.
- **Drag a rectangle** on the slide to attach that region, re-rendered sharply from the PDF, to your next question
  ("what does this arrow mean?").
- Quick buttons:
  - *Explain simpler*
  - *Exam question*: one exam-level question, without the solution.
  - *Check my answer*: grades what you wrote, then gives a model answer.

### 4. Print

**Print** opens a handout with one A4 page per slide: the slide on top (110 mm wide), notes below. The font shrinks
per page until the notes fit, down to 8 pt; longer notes continue on a second page rather than being cut. Use
*Print → Save as PDF*, or **Download PDF**. The same handout is kept as `handout.pdf` in the deck's results folder:
written automatically when the deck's notes are complete (and refreshed after a Regenerate), if Chromium for
Playwright is installed (see Setup).

### Removing a deck

**Delete** in the library moves the PDF and its results to `lectures/.deleted/` (recoverable). Deleting the PDF
yourself removes the deck from the library but keeps its results.

## Flashcards and per-subject instructions

A subject folder can hold two optional prompt files of your own, next to `Lectures/` and `Recitations/`:

```
lectures/Image Analysis & Computer Vision/
  tutor_instructions.md      your tutor prompt for this subject: used for its notes, chat and flashcards
  flashcards.md              your flashcard instructions: turns flashcards on for this subject
  Exams/                     past exams and their solutions (PDF); any subfolder with "exam" in its name
                             works, e.g. Past Exams/ (Exams/ is created if there is none)
```

With `flashcards.md` in place, every lecture of the subject gets **50 exam-style flashcards** in
`Results/Lectures/<deck>/flashcards.txt`, one card per line as `Question >> Answer`, ready to import into a
flashcard app. They are written automatically once all of the lecture's notes are done **and** `Exams/` contains at
least one PDF. The model gets your flashcard instructions, your tutor instructions, the text of the past exams (scans
are OCR'd if Tesseract is installed) and the lecture's slide text with its notes, so the cards follow what the
exams actually test.

- In the library, each deck shows its flashcard status. **Flashcards** opens them (click a card to reveal the
  answer, or download the `.txt`). Recitations, and lectures before any exam is added, get theirs from the button.
- Finished cards are never redone automatically. After adding exams or changing `flashcards.md`, press
  **Regenerate flashcards**.
- It is one request per deck, at most one deck at a time. `FLASHCARDS_COUNT`, `FLASHCARDS_MODEL` and
  `FLASHCARDS_EFFORT` (default 50, the notes model, `high`) change the defaults.

`tutor_instructions.md` is added after the app-wide `prompts/tutor_instructions.md`. For the notes, the notes format
(length, no headings) still wins where the two disagree, since the notes are printed one page per slide; in the chat,
your instructions win. Both files are re-read on every request. `lectures/` is git-ignored, so your prompts and
exams stay on your machine.

## What's in a results folder

```
lectures/<Subject>/Results/Lectures/<deck>/        (or Results/Recitations/<deck>/)
  notes/NNN.md          generated notes: plain Markdown, edit them freely
  handout.pdf           all slides + notes, one A4 page per slide
  flashcards.txt        exam flashcards, "Question >> Answer" per line (subjects with flashcards.md)
  context/*.pdf         extra material for this deck (optional)
  .tutor/               app state (deleting it resets chats and usage stats; slides are re-rendered, notes kept)
    meta.json  text.json  usage.json
    slides/NNN.png  slides/NNN_sm.png      rendered slides and thumbnails
    chat/NNN.json  chat/images/            chat threads and region crops
    context-text/                          extracted text of context PDFs
    logs/requests.jsonl                    what was sent with each request (see below)
```

**Renaming** a PDF is fine: its results folder is renamed to match and nothing is regenerated (the app recognises
the file by its content). Renaming a subject folder is fine too. A deck's title is its PDF name.

## Customising

| What | Where |
|---|---|
| Notes style (length, structure, "⚠ check:", "Takeaway:") | `prompts/notes_style.md` |
| Your own instructions (course, exam format, language, …), used for notes **and** chat | `prompts/tutor_instructions.md` (all subjects), `lectures/<Subject>/tutor_instructions.md` (one subject) |
| Flashcards for a subject | `lectures/<Subject>/flashcards.md` (see [Flashcards](#flashcards-and-per-subject-instructions)) |
| Markdown/LaTeX output rules | `prompts/output_format.md` |
| Chat behaviour | `CHAT_RULES` in `prompts.py` |

Prompt files are re-read on every call, so you don't need to restart. Notes already written keep the old style;
use Regenerate to redo them.

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `LECTURES_DIR` | `./lectures` | where subject folders live (e.g. a folder in your study directory) |
| `AUTO_PREPARE` | `1` | `0`: new folders are processed but notes wait for **Prepare lecture** |
| `NOTES_MODEL` / `CHAT_MODEL` | `claude-sonnet-5-5` | model for notes / chat |
| `NOTES_EFFORT` / `CHAT_EFFORT` | `medium` | `low` … `max`; higher means more thinking and more plan usage |
| `CONCURRENCY` | `4` | parallel note generations, shared by all lectures |
| `FLASHCARDS_COUNT` / `FLASHCARDS_MODEL` / `FLASHCARDS_EFFORT` | `50` / notes model / `high` | flashcards per deck, and the model for them |
| `MAX_ATTEMPTS` | `5` | tries per slide on overload errors (backoff 2 s, 4 s, 8 s, …) |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | server address |

Example: `LECTURES_DIR=~/Courses/HPC/lectures NOTES_EFFORT=high uv run app.py`

An `ANTHROPIC_API_KEY` in your environment is ignored on purpose, so the subscription is used. Set
`SLIDE_TUTOR_ALLOW_API_KEY=1` if you'd rather bill an API key.

## Usage and limits

For reference: a 109-slide lecture took about 640k input tokens (most of them cached) and 160k output tokens with
Sonnet at medium effort, and moved the plan's 5-hour utilisation by about 8 percentage points. The library shows each lecture's token totals
and an *API-equivalent* $ figure. That figure is informational only; on a subscription you don't pay per token.

When the plan limit is reached, the lecture shows **Paused** with the reset time and continues automatically.

## How it works

- **Isolated model calls.** Each request runs Claude Code headless as a single turn with a custom system prompt and
  **no tools, MCP servers, skills, settings, CLAUDE.md or session history**. Your own Claude Code setup doesn't leak
  into the notes, and the model can't touch your files.
- **Prompt caching.** The per-lecture part (notes style, your instructions, outline, context files) is the system
  prompt, byte-identical for every slide, so it's cached. The first slide runs alone to warm the cache, then the rest
  run in parallel.
- **Crash safety.** Notes are written atomically. If the server is killed hard, leftover Claude Code processes are
  cleaned up on the next start, so they don't keep using your plan.
- **Verification log.** `.tutor/logs/requests.jsonl` records, for every request, the slide image (SHA-256), text and
  notes lengths, neighbouring slides, chat history length and whether a region was attached.
- **Not available on a subscription:** the Message Batches API.

Code: `app.py` (FastAPI routes, streaming chat, PDF export) · `jobs.py` (notes job, folder watcher) ·
`flashcards.py` (flashcard job, past-exam text) · `llm.py`
(Claude Code / Agent SDK wrapper, retries, limits) · `prompts.py` · `pdfproc.py` (PyMuPDF rendering, text, OCR) ·
`storage.py` (subject folders, deck index) · `static/` (plain HTML/CSS/JS; marked, KaTeX, highlight.js and DOMPurify are
vendored so it works offline) · HTTP API in `docs/API.md`.

## Tests

```bash
uv run python tests/make_test_pdf.py                 # synthetic 6-slide deck → tests/fixtures/test_lecture.pdf
uv run python tests/frontend/check_render.py          # Markdown/KaTeX rendering checks (no model calls)
uv run python tests/screenshots.py <lecture-id>       # screenshots of library, reader and print view
```

To try the app without a real lecture, copy the test deck into a subject: `mkdir -p lectures/Demo/Lectures && cp
tests/fixtures/test_lecture.pdf lectures/Demo/Lectures/`.

## Troubleshooting

- *"Claude Code CLI not found"* or authentication errors: run `claude`, log in, and check `claude auth status`.
- *A new PDF doesn't show up*: it must be directly inside `lectures/<Subject>/Lectures/` or `…/Recitations/`, and
  is picked up a few seconds after it has finished copying. Folder names starting with `.` or `_` are ignored.
- *A slide's notes failed*: the error is shown on that slide. Press **Prepare lecture** to retry failed slides, or
  **Regenerate** for one slide.
- *Paused*: your plan's usage window is used up, and the job resumes after the reset time shown.
- Server log: the terminal running `app.py`. Set `LOG_LEVEL=DEBUG` for more detail.
