# Personal Agent

A local, browser-native agent that reads the page you're on and helps you
work with it — summarizing YouTube videos, drafting Gmail replies, and
(the current focus) filling out job-application forms from your own
structured profile. The core loop runs locally: a Chrome extension talks
to a small FastAPI server, which calls a local Ollama model. Nothing is
sent to a hosted LLM.

---

## Project Overview

Job application forms ask for the same handful of facts over and over —
name, email, phone, work authorization, LinkedIn, "why do you want this
role" — but every site labels those questions differently, groups them
into different UI widgets, and often embeds the whole application inside
an iframe under a different domain. Copy-pasting between a résumé and
twelve of these a week is the specific problem this project is trying
to remove.

The system's job is not to submit an application. It's to read the
form, look up what it can from a profile you own, ask a local LLM about
the rest, show you every proposed answer in a side panel, and let you
fix or reject anything before a single keystroke goes into the page.

**Current conceptual flow**

```
FORM
  ↓  extract fields and their available options
  ↓  decide what each field is asking for
  ↓  retrieve relevant profile information
  ↓  generate / resolve the answer
  ↓  user reviews in the side panel
  ↓  fill the form (never submit)
```

The important design constraint: **exact label matching alone is not
enough**. Real forms ask for the same fact in many ways —

- `First Name`
- `Given Name`
- `What should we call you?`

— all mean the same thing. And some questions require combining more
than one profile field, e.g. `First and Last Name` is not a single
stored value.

---

## Current Architecture

Three moving parts on your laptop, plus one local model.

```
┌───────────────────────────────────────────────────────────────┐
│                    Browser (Chrome / Chromium)                │
│                                                               │
│   ┌───────────────────────────────────────────────────┐       │
│   │ Content scripts (all frames)                      │       │
│   │  formdetect.js  — extracts inputs / groups radios │       │
│   │  gmail.js       — extracts open Gmail thread      │       │
│   │  content.js     — assembles page context bundle   │       │
│   └───────────────────────────────────────────────────┘       │
│                       ▲                                       │
│                       │ chrome.tabs.sendMessage(frameId)      │
│                       ▼                                       │
│   ┌───────────────────────────────────────────────────┐       │
│   │ background.js (service worker)                    │       │
│   │  Cmd/Ctrl+Shift+F shortcut → opens side panel     │       │
│   │  Aggregates form fields across ALL frames via     │       │
│   │  chrome.scripting.executeScript({allFrames:true}) │       │
│   │  Routes FILL_FIELD back to the source frame       │       │
│   └───────────────────────────────────────────────────┘       │
│                       ▲                                       │
│                       │ chrome.runtime.sendMessage            │
│                       ▼                                       │
│   ┌───────────────────────────────────────────────────┐       │
│   │ sidepanel.{html,js}  — READY / UNKNOWN rows,      │       │
│   │  editable values, Fill / Skip, auto-fill on READY │       │
│   └───────────────────────────────────────────────────┘       │
└───────────────┬───────────────────────────────────────────────┘
                │  POST /ask   (JSON over 127.0.0.1:8000)
                ▼
┌───────────────────────────────────────────────────────────────┐
│                 FastAPI server  (server/main.py)              │
│                                                               │
│   fast-intent router  ─── "fill this form" ──────┐            │
│   ReAct loop (Ollama chat + tools) ─── everything│else        │
│                                                  │            │
│   Tool registry (server/tools.py):               ▼            │
│     extract_page_text · summarize_transcript ·   │            │
│     read_email_thread · draft_email_reply ·      │            │
│     detect_form_fields · fill_form_field         │            │
│                                                  │            │
│   detect_form_fields:                            │            │
│     ├─ OBVIOUS lookup (server/concepts.py)       │            │
│     └─ ONE batched Qwen call for the rest        │            │
│                                                  ▼            │
│   Profile:     server/profile.py + profile.json               │
│   Memory:      server/memory.py + memory.db (SQLite)          │
│   Tracing:     Langfuse spans on every LLM/tool call          │
└──────────────┬─────────────────────────┬──────────────────────┘
               │                         │
               ▼                         ▼
     Ollama  (localhost:11434)     Langfuse  (localhost:3000)
     qwen2.5:7b  (form + chat)     self-hosted, Docker Compose
     nomic-embed-text  (memory)
```

**Form field extraction.** `extension/formdetect.js` walks
`input`/`textarea`/`select` in the DOM, collapses radios and checkboxes
into groups (by shared `name`, by `role="radiogroup"` / `<fieldset>`
ancestor, or by a per-question wrapper for sites like Microsoft Forms
that use per-radio unique names), and resolves each field's label
through a strategy chain: `label[for]` → wrapping `<label>` → row-label
→ `aria-labelledby` → site-specific ancestor selectors
(`FALLBACK_LABEL_SELECTORS`).

**Iframe handling.** Many job platforms (SmartRecruiters, Greenhouse
embeds, etc.) render the application inside a cross-origin child
iframe. The manifest declares content scripts with `all_frames: true`;
the background worker uses
`chrome.scripting.executeScript({target: {tabId, allFrames: true}})`
to run `detectFormFields()` in every frame and annotates each returned
field with the producing `frameId`. `FILL_FIELD` messages are routed
back to the same frame — detection and fill must agree on frame
identity or the selector misses.

**Backend / API.** `server/main.py` exposes `POST /ask`. It first tries
a small fast-intent router (regex + a "does this apply" predicate) —
`"fill this form"` short-circuits directly into `detect_form_fields`,
skipping memory recall and the LLM chat round. Anything else runs the
ReAct tool loop against Ollama's `qwen2.5:7b`, retrieving relevant past
interactions from local memory first.

**Profile storage and lookup.** See [Profile System](#profile-system)
and [Field Matching / Answer Resolution](#field-matching--answer-resolution).

**Local LLM.** `qwen2.5:7b` served by Ollama at `127.0.0.1:11434`. Kept
resident between requests via `OLLAMA_KEEP_ALIVE` (defaults to `30m`)
so the second request in a session doesn't pay the model-load penalty.

**Semantic embeddings.** Used only for the *memory* subsystem (past-
interaction recall via cosine similarity in SQLite +
`nomic-embed-text`). Form-fill does NOT use embeddings today — the
form-fill path is deterministic OBVIOUS lookup + one batched Qwen call.

**Langfuse.** Wraps `/ask`, tool executions, Ollama calls, and memory
recall in spans/generations. Self-hosted locally at
`http://localhost:3000` via the upstream Langfuse Docker Compose.

**User review / fill.** The side panel renders one row per field with a
badge (READY / UNKNOWN), an editable value, a Fill button, a Skip
button, and a collapsed "Details" disclosure (source, selector).
READY rows auto-fill into the page after the pipeline returns; the user
can still edit and re-Fill, or Skip. The extension never submits the
form.

---

## Profile System

**Source of truth:** `server/profile.json`.

**Loader:** `server/profile.py` — one small module. Two functions:
`load_profile()` reads the JSON, and `get_path(profile, "dotted.path")`
walks nested keys, returning `None` for missing keys, `null`, or
empty-after-strip strings. `False` and `0` survive because they're
legitimate answers (e.g. `requires_sponsorship: false`).

**Shape.** A hierarchical document. Roughly:

```jsonc
{
  "schema": {
    "identity":    { "first_name": "…", "last_name": "…", "full_name": "…", ... },
    "contact":     { "personal_email": "…", "phone": "…", ... },
    "address":     { "street": "…", "city": "…", "state": "…", ... },
    "web_presence":{ "linkedin_url": "…", "github_url": "…", ... },
    "education":   { "current": { "school": "…", "field": "…", ... } },
    "employment":  { "current_employer": "…", "current_title": "…", ... },
    "eligibility": { "requires_sponsorship": true, ... },
    ...
  },
  "collections": {
    "work_experiences": [ { "employer": "…", "title": "…", ... }, ... ],
    "education_history": [ ... ],
    "skills": [ "Python", "TypeScript", ... ],
    ...
  },
  "generation_context": {
    "professional_summary": "…free text used as grounding for Qwen…",
    "career_interests": "…",
    ...
  },
  "preferences": { ... }
}
```

**How unknowns are handled.** `null` means "the user has deliberately
not filled this in." The loader returns `None`; `detect_form_fields`
either surfaces the row as UNKNOWN (empty input, user types) or asks
Qwen to answer from `generation_context` if the question is
free-response. Never substitutes a similar value.

**Adding a new profile field.** Two steps:

1. Add the value to `server/profile.json` under the appropriate
   section.
2. If the field is one that appears on nearly every form and has an
   unambiguous label (name, email, phone, …), add an entry to the
   `OBVIOUS` dict in `server/concepts.py` with its dotted path and any
   aliases / autocomplete tokens. **You do not need to do this for
   most fields** — the Qwen path reads the whole profile as context
   and will produce an answer for anything the profile can support.

---

## Field Matching / Answer Resolution

### What's implemented today

`server/tools.py :: _detect_form_fields` runs two paths per field:

1. **OBVIOUS.** `server/concepts.py :: match_obvious(label, autocomplete)`
   returns an id iff the field's HTML `autocomplete` token or the
   normalized full-label alias unambiguously names a standard profile
   slot (`first_name`, `email`, `phone`, `city`, `linkedin_url`, …). No
   substring guessing — the full normalized label must equal a listed
   alias. Value is fetched via `get_path` and, for phones, reformatted
   to match any format hint present in the field's label / placeholder
   (`###-###-####`, `(###) ###-####`, etc.).

2. **Batched Qwen call.** All fields the OBVIOUS pass didn't resolve
   are collected and sent to `qwen2.5:7b` in a *single* prompt via
   `server/concepts.py :: build_prompt`. The prompt ships the full
   profile JSON as context and asks for a JSON map `{index: answer}`.
   `parse_response` extracts the answers; each answer is validated
   against the field's options (if any) before being marked READY.

Empty Qwen answers and answers that fail the option-fit check become
UNKNOWN. Nothing is written to the page until the side panel renders
them and either the user clicks Fill, or the row is safely
auto-fillable (READY, non-empty, option-fit passed).

### What we're moving toward

The current architecture solves the "does the OBVIOUS list cover it —
yes/no" question quickly, and delegates the rest to a single LLM call
that produces answer *strings*. It works, but the LLM sometimes has to
guess when the *right* answer is a combination or transformation of
profile fields.

The direction we want to move:

```
Form question
  ↓  deterministic matching when obvious
  ↓  semantic retrieval of potentially relevant profile fields
  ↓  ONE Qwen reasoning call — decide what info is needed, how to combine it
  ↓  structured interpretation (fields + operation)
  ↓  deterministic answer construction
  ↓  confidence
  ↓  user review
```

Concretely, for a question like `First and Last Name`, we want Qwen to
return a *plan* rather than a free-text answer:

```jsonc
{
  "fields": ["schema.identity.first_name", "schema.identity.last_name"],
  "operation": "combine",
  "separator": " ",
  "confidence": 0.96
}
```

The server then constructs the answer deterministically from the plan
(`Ashish Kurumeti`), which is auditable and can't hallucinate.
Similarly:

- `What year do you expect to graduate?` → plan: extract year from
  `schema.education.current.graduation_date`.
- `How can we reach you by phone?` → plan: retrieve
  `schema.contact.phone` even though the label doesn't say "Phone
  Number."

Semantic retrieval (embeddings) has a role in the "retrieve
potentially relevant profile fields" step — narrowing what the LLM
has to reason over — but similarity is never the final authority.

This structured-plan architecture is on the roadmap; it is not
implemented today.

---

## Local Development

### Prerequisites

- **Docker Desktop** running (macOS/Windows) or Docker Engine (Linux).
- **Ollama** installed natively (macOS: `brew install ollama`).
  Native, not Docker — Metal cannot be passed through to Docker on
  macOS. `dev.sh` will start Ollama for you if it isn't already
  running.
- **Chrome / Chromium** for the extension.
- (Optional, for native no-Docker workflow) Python 3.10+ with a
  `venv/` and `server/requirements.txt` installed.

### One-command startup

```bash
./dev.sh
```

`dev.sh` verifies Docker, starts Ollama if needed, pulls `qwen2.5:7b`
and `nomic-embed-text` if missing, copies `.env.example → .env` on
first run, and brings the whole stack up:

- FastAPI server:   http://localhost:8000
- Langfuse UI:      http://localhost:3000
- MinIO console:    http://localhost:9001  (login `minio` / `miniosecret`)
- Ollama (native):  http://localhost:11434

On the very first boot Langfuse auto-provisions an org, project, API
keys, and user login from `LANGFUSE_INIT_*` in `.env`. Log in at
http://localhost:3000 with the email/password from `.env`. The API
keys the personal-agent container uses are also from `.env` — no
copy-paste required.

**Shutdown:**
```bash
./dev-stop.sh          # equivalent to: docker compose down
```
Volumes (Postgres, ClickHouse, MinIO) and host-side files
(`server/memory.db`, `server/profile.json`) all persist across
restarts.

**Logs:**
```bash
docker compose logs -f                     # everything
docker compose logs -f personal-agent      # FastAPI only
docker compose logs -f langfuse-web        # Langfuse UI/ingest
docker compose logs -f langfuse-worker
docker compose logs -f postgres clickhouse redis minio
```

**Load the Chrome extension** once, on any development machine:
- `chrome://extensions`
- Enable Developer mode
- Load unpacked → select the `extension/` directory
- Pin it; open the side panel from its action button
- Optional: rebind the shortcut at `chrome://extensions/shortcuts`
  (defaults to Cmd+Shift+F on macOS, Ctrl+Shift+F elsewhere)

Code changes to `server/*.py` hot-reload — `uvicorn --reload` watches
the bind-mounted `./server` directory inside the container. Edits to
`server/profile.json` are picked up on the next `/ask` request.
Container image only needs rebuilding when `server/requirements.txt`
changes:

```bash
docker compose up -d --build personal-agent
```

### Native (no-Docker) workflow — optional

Still supported. You need Ollama running natively (`ollama serve`),
a running Langfuse *somewhere* reachable, and:

```bash
set -a; source server/.env; set +a
venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
```

Set `OLLAMA_BASE_URL=http://127.0.0.1:11434` in `server/.env` for the
native path (the code default already points there, so leaving it
unset works too).

### Using the form-fill

On a job application page, press **Cmd+Shift+F** (Ctrl+Shift+F on
non-mac). The side panel opens (if not already), the form is detected
across all frames, OBVIOUS fields are filled deterministically, the
remaining fields go out in one Qwen call, and READY rows auto-fill
into the page. UNKNOWN rows wait for you to type a value and click
Fill. Nothing submits.

---

## Configuration

Runtime configuration lives in two files, neither committed:

- **`.env`** (repo root) — used by Docker Compose. `dev.sh` seeds it
  from `.env.example` on first run.
- **`server/.env`** (optional) — used only when running the server
  natively without Docker.

Compose variables (all have working defaults in `.env.example`):

| Variable                              | Purpose                                          |
|---------------------------------------|--------------------------------------------------|
| `OLLAMA_BASE_URL`                     | Where the FastAPI container reaches Ollama. Default `http://host.docker.internal:11434`. |
| `OLLAMA_KEEP_ALIVE`                   | Ollama model keep-alive window. Default `30m`.   |
| `LANGFUSE_INIT_ORG_ID` / `_NAME`      | Org auto-created on Langfuse's first boot.       |
| `LANGFUSE_INIT_PROJECT_ID` / `_NAME`  | Project auto-created on first boot.              |
| `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`    | Project keys — the personal-agent container reads these directly, so no post-boot copy-paste. |
| `LANGFUSE_INIT_PROJECT_SECRET_KEY`    |                                                  |
| `LANGFUSE_INIT_USER_EMAIL` / `_NAME` / `_PASSWORD` | UI login credentials, auto-created on first boot. |
| `LANGFUSE_SALT`                       | Langfuse internal secret. Any long random string.|
| `LANGFUSE_NEXTAUTH_SECRET`            | Langfuse internal secret. Any long random string.|
| `LANGFUSE_ENCRYPTION_KEY`             | 32-byte hex string. Any value works locally.     |

**How the FastAPI container reaches Ollama.** The container has
`extra_hosts: host.docker.internal:host-gateway` set in `compose.yaml`
and reads the URL from `OLLAMA_BASE_URL`. Docker Desktop on macOS
resolves `host.docker.internal` to the host machine automatically, so
container-side requests to `http://host.docker.internal:11434` hit the
native Ollama process.

**How Langfuse tracing is wired.** Inside the container the
personal-agent process posts traces to `http://langfuse-web:3000` (the
Docker service name), while your browser still uses
`http://localhost:3000` for the Langfuse UI. Both hit the same
Langfuse process.

Hardcoded (not env-configurable; change in code):
- Model: `qwen2.5:7b` (`server/tools.py`) and `nomic-embed-text`
  (`server/memory.py`)
- FastAPI bind port: `8000`

Never put profile PII, Langfuse keys, or API tokens in
`server/profile.json`. It's meant to be hand-edited and stays out of
version control.

---

## Repository Structure

```
personal-agent/
├── README.md
├── CLAUDE.md                     # working notes for the AI pair-programmer
├── compose.yaml                  # personal-agent + Langfuse v3 stack
├── Dockerfile                    # FastAPI image (Python 3.11, uvicorn --reload)
├── .dockerignore
├── dev.sh                        # one-command startup
├── dev-stop.sh                   # docker compose down (data preserved)
├── .env.example                  # template for the compose stack .env
├── server/
│   ├── main.py                   # FastAPI app: /ask, /health, fast-intent router, ReAct loop
│   ├── tools.py                  # Tool registry: page/PDF, YouTube, Gmail, form-fill
│   ├── concepts.py               # OBVIOUS registry + one-shot Qwen prompt & parse
│   ├── profile.py                # Profile loader: load_profile, get_path
│   ├── profile.json              # ★ user profile — source of truth (hand-edit; git-ignored)
│   ├── memory.py                 # SQLite + embeddings for prior-interaction recall
│   ├── memory.db                 # SQLite database (created on first run; git-ignored)
│   ├── requirements.txt          # Python deps
│   ├── .env.example              # template for optional native-dev .env
│   └── .env                      # secrets (NOT committed)
└── extension/
    ├── manifest.json             # MV3; commands, permissions, content scripts
    ├── background.js             # service worker: shortcut, frame aggregation, routing
    ├── content.js                # per-frame: GET_PAGE_CONTEXT, FILL_FIELD, INSERT_DRAFT
    ├── formdetect.js             # DOM → field descriptors + fillField writer
    ├── gmail.js                  # Gmail thread extraction + compose insertion
    ├── sidepanel.html            # panel markup + styles
    ├── sidepanel.js              # panel logic: render, auto-fill, Fill/Skip
    └── fixtures/                 # offline HTML for developing detection heuristics
```

---

## Current Status

### Implemented today

- Chrome MV3 extension with side panel and Cmd/Ctrl+Shift+F shortcut.
- Form field extraction with radio/checkbox grouping, including a
  rescue pass for Microsoft-Forms-style per-radio unique names.
- Cross-frame form detection: `all_frames: true` + `executeScript` +
  frameId-annotated fields and frameId-routed fills.
- Profile loader with dotted-path access.
- Deterministic OBVIOUS resolution for standard fields (name, email,
  phone, address parts, LinkedIn / GitHub / website).
- Single batched Qwen call for everything else, with option-fit
  validation before any answer is marked READY.
- Phone-format adapter that respects a field's own format hint.
- Side panel with READY / UNKNOWN states, editable values, Fill / Skip,
  and Details disclosure.
- Auto-fill of READY rows on pipeline completion.
- Never-submit guarantee: no code path clicks a submit button or calls
  `form.submit()`.
- Memory: past-interaction recall via SQLite + `nomic-embed-text`.
- Gmail: thread extraction, two-pass draft (generate + polish),
  explicit Insert-into-compose confirmation.
- YouTube: transcript extraction + chunked summarization.
- Langfuse tracing on `/ask`, tools, and Ollama calls.

### Planned / next steps

- **Structured-plan Qwen output** for ambiguous questions —
  `{fields, operation, confidence}` instead of a free-text answer, so
  composite / derived answers (`First and Last Name`, `graduation
  year`) are constructed deterministically from named profile fields.
- **Semantic retrieval over profile concepts** to narrow the LLM's
  context — embeddings for retrieval, not as the final authority.
- **Confidence scoring** surfaced in the panel so the user knows which
  answers merit closer review before Fill.
- **Broader platform / iframe coverage** — Greenhouse, Lever, Workday,
  and other ATS-specific quirks; nested iframes and shadow DOM
  traversal when detection returns empty.
- **Better validation before filling** — pattern / min-max / required
  cross-checks at the browser boundary.
- **Field-intent understanding** independent of exact wording, moving
  further away from any manually curated alias list.

The user-in-control principle stays: every proposed answer surfaces in
the panel, edits are possible, and submission is always a manual
action.
