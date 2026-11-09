# slide-tutor HTTP API (contract between backend `app.py` and `static/` frontend)

Server: FastAPI on http://127.0.0.1:8000. All JSON. Slide numbers `n` are **1-based**.

## Pages (served as static HTML files)
| Route | File |
|---|---|
| `GET /` | `static/index.html` — library (upload + list of lectures) |
| `GET /lecture/{id}` | `static/reader.html` — reader (slide + Notes/Ask panel) |
| `GET /lecture/{id}/print` | `static/print.html` — print/handout view |
| `GET /static/...` | everything under `static/` (CSS, JS, `vendor/`) |

Pages get the lecture id from `location.pathname` (`/lecture/<id>` or `/lecture/<id>/print`).

Lectures are folders under `lectures/` (see README). The id is a slug of the folder name, fixed when the folder is
first seen and stored in `.tutor/meta.json`. Folders created on disk are picked up by a 5-second scan, so a new
lecture can appear in `GET /api/lectures` without any upload call.

## Shared shapes

```jsonc
// UsageTotals
{ "calls": 12, "input_tokens": 340, "cache_creation_input_tokens": 9000,
  "cache_read_input_tokens": 88000, "output_tokens": 15000, "cost_usd": 0.41,
  "by_kind": { "notes": { /* same fields, no by_kind */ }, "chat": { /* ... */ } } }

// Prep (notes pre-generation job state)
{ "done": 7, "total": 24, "running": true,
  "errors": { "5": "error message" },          // slide number (string key) -> last error
  "generating": [8, 9, 10],                    // slides currently being generated
  "message": "optional human-readable status",  // e.g. "Usage limit reached — resumes 14:00"
  "paused_until": 1794224400 }                  // unix seconds or null

// LectureSummary
{ "id": "lecture-07-caches", "title": "Lecture 07 caches", "filename": "lecture-07-caches.pdf",
  "folder": "/Users/me/slide-tutor/lectures/Lecture 07 caches",   // the lecture's folder on disk
  "created": "2026-11-09T12:00:00Z", "n_slides": 24,
  "status": "processing|ready_to_prepare|preparing|paused|ready|incomplete|error",
  "prep": Prep, "context_files": [ { "name": "prev-lecture.pdf", "chars": 41000 } ],
  "usage": UsageTotals }
```

Status meanings: `processing` = PDF being rendered; `ready_to_prepare` = rendered, notes not started;
`preparing` = notes job running; `paused` = plan usage limit hit (see `prep.message`, `prep.paused_until`; auto-resumes);
`ready` = every slide has notes; `incomplete` = job finished but some slides failed (`prep.errors`);
`error` = PDF processing failed (`prep.message`).

## Endpoints

| Method & path | Request | Response |
|---|---|---|
| `GET /api/status` | – | `{ "playwright_pdf": bool, "notes_model": str, "chat_model": str, "concurrency": int, "lectures_dir": str, "rate_limit": { "five_hour": {"utilization": 0.45, "resets_at": 1794224400, "status": "allowed"}, "seven_day": {...} } \| null }` |
| `GET /api/lectures` | – | `[LectureSummary]` newest first |
| `POST /api/lectures` | multipart: `file` (PDF, required), `context` (0..n PDFs), `autostart` (`"true"`/`"false"`, default true) | `LectureSummary` (status `processing`) |
| `GET /api/lectures/{id}` | – | `LectureSummary` + `"slides": [{"n":1,"title":"...","has_notes":true}]` |
| `DELETE /api/lectures/{id}` | – | `{"ok": true, "moved_to": str}` (the folder is moved to `lectures/.deleted/`, not erased) |
| `POST /api/lectures/{id}/context` | multipart: `context` (1..n PDFs) | `LectureSummary` |
| `DELETE /api/lectures/{id}/context/{name}` | – | `LectureSummary` |
| `POST /api/lectures/{id}/prepare` | – | `LectureSummary` (starts/resumes the notes job; skips slides that already have notes; no-op if running) |
| `GET /api/lectures/{id}/progress` | – | `{ "status", "prep": Prep, "usage": UsageTotals }` — poll every ~2 s while `status` is `processing`/`preparing`/`paused` |
| `GET /api/lectures/{id}/slides/{n}/image?size=lg\|sm` | – | PNG. `lg` = 1600 px wide (main view), `sm` = 320 px wide (thumbnails) |
| `GET /api/lectures/{id}/slides/{n}/notes` | – | `{ "n", "title", "markdown": str\|null, "status": "done\|pending\|generating\|error", "error": str\|null }` |
| `POST /api/lectures/{id}/slides/{n}/regenerate` | – | `{"ok": true}`; afterwards the slide's notes status is `generating` until done |
| `GET /api/lectures/{id}/notes` | – | `[{ "n", "title", "markdown": str\|null }]` for all slides (print view) |
| `GET /api/lectures/{id}/slides/{n}/chat` | – | `{ "messages": [ChatMessage] }` |
| `DELETE /api/lectures/{id}/slides/{n}/chat` | – | `{"ok": true}` (clears that slide's thread) |
| `POST /api/lectures/{id}/slides/{n}/ask` | JSON `{ "question": str, "region": [x0,y0,x1,y1] \| null }` | `text/event-stream` (see below) |
| `GET /api/lectures/{id}/chat-images/{file}` | – | PNG (region crops referenced from chat messages) |
| `GET /api/lectures/{id}/pdf` | – | `application/pdf` attachment (server-side Playwright render of the print view). 501 if Playwright/Chromium unavailable |

```jsonc
// ChatMessage
{ "role": "user|assistant", "content": "markdown text", "ts": "2026-11-09T12:00:00Z",
  "region": { "rect": [0.1,0.2,0.5,0.6], "image": "/api/lectures/<id>/chat-images/005-1731150000.png" } | null }
```

`region` = rectangle as **fractions of the slide image** (0..1, origin top-left, x0<x1, y0<y1).

### SSE stream from `/ask`
POST with `fetch` and read `response.body` (EventSource can't POST). Frames are `event: <name>\ndata: <json>\n\n`:
- `start` `{ "slide": n, "user_message": ChatMessage }`
- `delta` `{ "text": "..." }` — append to the answer being displayed (many of these)
- `done` `{ "message": ChatMessage, "usage": {...} }` — final assistant message (already persisted server-side)
- `error` `{ "message": "..." }`

The user message is persisted when the stream starts; the assistant message when it ends. Asking on slide 5 and then
navigating to slide 6 must not break: the stream for slide 5 keeps running and its result belongs to slide 5's thread.

### Print view contract
`print.html` must set `document.body.dataset.ready = "1"` once every page's images are loaded, math is rendered and
the font-fitting loop has finished. The server-side PDF export waits for `body[data-ready="1"]`.
