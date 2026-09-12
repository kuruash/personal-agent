# Architecture

Browser-native local Jarvis: Chrome extension + FastAPI backend + native
Ollama. Fully local; no cloud APIs in the core loop.

## Repository layout

```
personal-agent/
├── extension/                        Chrome MV3 extension (flat by design)
│   ├── manifest.json
│   ├── background.js                 service worker: IPC hub + backend calls
│   ├── content.js                    per-frame content-script dispatcher
│   ├── formdetect.js                 generic form discovery + fill
│   ├── gmail.js                      Gmail thread extract + compose insert
│   ├── sidepanel.html
│   ├── sidepanel.js                  side-panel UI + streaming handler
│   └── fixtures/                     offline HTML for DOM heuristics
│
├── server/
│   ├── main.py                       FastAPI app + Langfuse init + warmup
│   │
│   ├── api/                          transport layer
│   │   ├── schemas.py                AskRequest, ContextPayload
│   │   └── routes/
│   │       ├── health.py             GET /health
│   │       ├── ask.py                POST /ask (ReAct + fast-intent + memory)
│   │       └── form_stream.py        POST /ask/form_stream (NDJSON)
│   │
│   ├── workflows/form_answering/     the form-answering pipeline
│   │   ├── __init__.py               re-exports: Route, answer_form, answer_form_streaming
│   │   ├── orchestrator.py           streaming lifecycle
│   │   ├── router.py                 Route enum + route_field
│   │   ├── direct.py                 DIRECT_VALUE resolution
│   │   ├── boolean.py                DIRECT_BOOLEAN resolution
│   │   ├── semantic.py               evidence packaging (dedupe_by_related)
│   │   ├── prompt_builder.py         batched Qwen prompt + response parsing
│   │   ├── validation.py             answer validation, option-fit, phone reformat
│   │   ├── models.py                 plan_entry shape + final_counts
│   │   └── constants.py              tuning thresholds
│   │
│   ├── domain/profile/               the user's profile domain
│   │   ├── profile.json              source of truth
│   │   ├── repository.py             load_profile, get_path, collection accessors, fingerprint
│   │   ├── direct_lookup.py          DirectMatch, direct_lookup (worked-at, skill, degree, cert)
│   │   └── indexer.py                build_documents (profile → retrieval docs)
│   │
│   ├── retrieval/                    search over profile documents
│   │   ├── normalize.py              tokenize / normalize_text / contains_term / TECH_ALIASES
│   │   ├── lexical.py                BM25 scorer
│   │   ├── vector.py                 Chroma vector index
│   │   └── hybrid.py                 weighted combiner (0.60/0.30/0.10)
│   │
│   ├── llm/
│   │   └── ollama_client.py          HTTP wrappers, embed, MODEL, keep_alive, perf metadata
│   │
│   ├── tools/                        tool registry for the ReAct loop
│   │   ├── context.py                Context dataclass
│   │   ├── registry.py               TOOLS list, IMPLS map, tool_by_name, ollama_tool_specs
│   │   └── impls.py                  extract_page_text, summarize_transcript, ...
│   │
│   ├── memory/                       past-interaction memory
│   │   └── store.py                  SQLite + cosine recall
│   │
│   ├── memory.db                     SQLite interactions (bind-mounted)
│   └── data/chroma/                  Chroma HNSW index (bind-mounted)
│
├── scripts/
│   ├── ask_profile.py                REPL for the profile QA pipeline
│   └── eval_retrieval.py             vector vs hybrid eval harness
│
├── Dockerfile / compose.yaml         FastAPI + Langfuse stack (Ollama stays native)
├── dev.sh / dev-stop.sh              one-command startup / teardown
├── README.md
└── CLAUDE.md
```

## Backend layers

```
    api          ← FastAPI routes (thin, no business logic)
     ↓
  workflows      ← form_answering pipeline (routing + orchestration)
     ↓
  domain    retrieval    llm    memory    tools
    profile      normalize        ollama_client   store    registry
    indexer      lexical                                    impls
    lookup       vector                                     context
                 hybrid
```

**Dependency rules**

- `api/*` may import from `workflows/`, `tools/`, `memory/`, `llm/`.
- `workflows/form_answering/*` may import from `domain/`, `retrieval/`, `llm/`.
- `retrieval/*` may import from `domain/profile/` and `llm/ollama_client.embed`.
  It must **not** import from `api/`, `workflows/`, or `tools/`.
- `llm/*` may only import third-party libs; no upward dependencies.
- `domain/profile/*` may only import stdlib and from within its own package.
- `tools/*` may import from `llm/`, `workflows/form_answering/`. It must not
  reach into `retrieval/` or `domain/` directly — those are consumed via the
  form-answering workflow.

No circular imports.

## Form-answering workflow

Detected field
  ↓
`workflows/form_answering/router.py::route_field(field)`  →  one of:
  ```
  DIRECT_VALUE      DIRECT_BOOLEAN      SEARCH        REASON        UNKNOWN
  ```
  ↓
appropriate answerer:
- `direct.py::answer_direct_value(field, concept)` — profile schema lookup
- `direct.py::answer_certifications_list(field, certs)` — join cert list
- `boolean.py::answer_direct_boolean(field, extra)` — Yes/No option pick
- `orchestrator.py` collects SEARCH / REASON fields, runs `retrieve_hybrid`,
  packages evidence via `semantic.dedupe_by_related`, and sends ONE batched
  Qwen call built by `prompt_builder.build_batch_prompt` and parsed by
  `prompt_builder.parse_batch_response`.
  ↓
`validation.py::validate_qwen_answer(answer, field)` — option-fit or length check
  ↓
`models.plan_entry(...)` → panel-ready row with `state ∈ {ready, unknown,
retrieving, generating}`

The streaming lifecycle in `orchestrator.py::answer_form_streaming`:

```
meta   →   phase:direct_done   →   phase:generating   →   field ×N   →   done
```

## Retrieval workflow

`retrieval/hybrid.py::retrieve_hybrid(question, top_k)` blends three signals:

```
hybrid = 0.60 * vector + 0.30 * lexical + 0.10 * metadata
```

- `retrieval/vector.py`   — Chroma cosine similarity over nomic-embed-text
  vectors. Reindex is lazy, keyed off `profile_fingerprint()`.
- `retrieval/lexical.py`  — BM25 (k1=1.5, b=0.75) with entity-boost by
  re-including metadata list-values in the surface text.
- `retrieval/hybrid.py`   — whole-word metadata boost against
  `technologies / domains / items / employer / title / name / school / field`.

Weights are env-overridable via `PROFILE_VECTOR_WEIGHT`, `PROFILE_LEXICAL_WEIGHT`,
`PROFILE_METADATA_WEIGHT`.

## LLM workflow

`llm/ollama_client.py` owns all Ollama HTTP calls:

- `_ollama_generate(prompt)`     — plain /api/generate (transcript summary, email polish)
- `_ollama_json(prompt, num_predict)` — /api/generate with `format=json` (form batch)
- `embed(text)`                  — /api/embeddings via nomic-embed-text (L2-normalized)
- `ollama_perf_metadata(data)`   — extracts Ollama's per-call load / prefill /
  generate durations for Langfuse spans

Model: `qwen2.5:7b`. Keep-alive: `OLLAMA_KEEP_ALIVE` (default `30m`).

## Browser workflow

1. User clicks Ask in the side panel → `sidepanel.js` sends `ASK` to `background.js`.
2. `background.js` fetches page context (top frame only) and form fields
   (all frames via `chrome.scripting.executeScript({allFrames:true})`) and
   posts to the backend.
3. For form-fill patterns it uses `/ask/form_stream` and relays each NDJSON
   event to the side panel via `FORM_STREAM_EVENT`.
4. `sidepanel.js::handleFormStreamEvent(event)` renders rows incrementally.
   READY rows auto-fill via `FILL_FIELD` — background routes each write to the
   originating frame using `frameId` + `shadowPath`.

No auto-submit anywhere. Every write is preceded by an explicit fill click
(or the auto-fill of a READY row, which the user can still edit or skip).

## Where do I look for what?

| Question                                        | File                                                         |
|-------------------------------------------------|--------------------------------------------------------------|
| How is a field routed?                          | `server/workflows/form_answering/router.py::route_field`     |
| How is first name / email / phone answered?     | `server/workflows/form_answering/direct.py::answer_direct_value` |
| How is "have you used X" answered?              | `server/workflows/form_answering/boolean.py::answer_direct_boolean` + `server/domain/profile/direct_lookup.py::direct_lookup` |
| Where is BM25 computed?                         | `server/retrieval/lexical.py::score_all`                     |
| Where is Chroma queried?                        | `server/retrieval/vector.py::retrieve`                       |
| Where are the retrieval channels combined?      | `server/retrieval/hybrid.py::retrieve_hybrid`                |
| Where is Qwen called for form answers?          | `server/llm/ollama_client.py::_ollama_json` (via `orchestrator.py`) |
| Where is Qwen called for the ReAct loop?        | `server/api/routes/ask.py::_chat`                            |
| Where is the batched prompt built?              | `server/workflows/form_answering/prompt_builder.py::build_batch_prompt` |
| Where is Qwen's answer validated?               | `server/workflows/form_answering/validation.py::validate_qwen_answer` |
| Where is streaming produced?                    | `server/workflows/form_answering/orchestrator.py::answer_form_streaming` |
| Where is streaming consumed by the extension?   | `extension/background.js` (parses NDJSON) → `extension/sidepanel.js::handleFormStreamEvent` |
| Where are memory recalls done?                  | `server/memory/store.py::recall`                             |
| Where is the profile read?                      | `server/domain/profile/repository.py::load_profile`          |
| Where are profile → retrieval docs built?       | `server/domain/profile/indexer.py::build_documents`          |
| Where is the tool registry?                     | `server/tools/registry.py`                                   |
| Where is form detection in the browser?         | `extension/formdetect.js::detectFormFields`                  |
| Where is a form field written?                  | `extension/formdetect.js::fillField`                         |
| Where are Gmail selectors?                      | `extension/gmail.js`                                         |
| Where are the Ollama URL / model / keep-alive?  | `server/llm/ollama_client.py`                                |
| Where is the FastAPI app constructed?           | `server/main.py`                                             |

## Preserved contracts

Nothing about the following changed during the reorganization:

- Endpoint paths, request bodies, and response shapes (`GET /health`,
  `POST /ask`, `POST /ask/form_stream`).
- NDJSON event names and payloads (`session`, `meta`, `phase:direct_done`,
  `phase:generating`, `field`, `done`).
- Chrome runtime message names (`ASK`, `INSERT_DRAFT`, `FILL_FIELD`,
  `RUN_FILL`, `FORM_STREAM_EVENT`, `GET_PAGE_CONTEXT`).
- Route enum values (`direct_value` / `direct_boolean` / `search` / `reason` /
  `unknown`) and field-state names (`ready` / `unknown` / `retrieving` /
  `generating`).
- Model (`qwen2.5:7b`), embedding model (`nomic-embed-text`), keep-alive.
- All retrieval score constants: `MIN_SIM=0.55`, `_MIN_EVIDENCE_SCORE=0.40`,
  BM25 `K1=1.5 / B=0.75`, hybrid weights `0.60 / 0.30 / 0.10`, and
  `_QWEN_NUM_PREDICT_*` caps.
- Profile schema and `profile.json` contents.
