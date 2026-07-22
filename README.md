# 🦍 Alfred V3 — Modular, Hybrid-Intent Building-Aware Search Assistant

> See [ARCHITECTURE.md](ARCHITECTURE.md) for the implementation-aligned system
> design, runtime flows, security boundaries, deployment topology, and evolution
> priorities.

Alfred is an intelligent, Streamlit-based search assistant for the University of Bristol's Campus Division.

It provides **multi-domain, building-aware search** across:
- Building Management Systems (BMS)
- Fire Risk Assessments and Fire Risk Action Items (FRAs)
- Planon property data (conditions, areas, metadata)
- Maintenance requests and job records
- General RAG / semantic search across documentation

Powered by:
- ✅ OpenAI embeddings and answer generation
- ✅ Pinecone vector search
- ✅ A hybrid **rule-based + ML intent classifier** pipeline
- ✅ MSAL (Microsoft Entra ID) authentication and role-based access control

---

## 🧠 Features

Alfred V3 pairs a hybrid query-routing front end with a transactional,
concurrency-safe ingestion pipeline:

**Query side**
- Hybrid rule-based + ML intent routing via the `NLPIntentClassifier`
- Building / business-term extraction and spell checking in preprocessing
- Chain-of-responsibility handlers, one per intent
- Unified structured + semantic retrieval through the `search_core` package
- OpenAI-backed answer summarisation for semantic and maintenance results

**Ingestion side**
- Secure local file ingestion (`security/file_operations_validator.py`)
- FRA action-plan parsing with structured extraction and triage scoring
- Atomic supersession handling when newer FRAs replace older ones
- Vector storage via Pinecone, embedding via OpenAI
- Redis-backed job registry, locks, and file registry
- Thread-safe stats, caching, and vector buffering
- Dry-run mode for safe validation
- Parallel IO + parsing workers with batched, retry-aware upserts
- Prometheus (`.prom`) metric files and a JSONL event sink for observability

### `query_core/intent_classifier.py` — NLPIntentClassifier

A context-aware intent classifier using a **quantized all-MiniLM-L6-v2 CT2 encoder**
(`michaelfeil/ct2fast-all-MiniLM-L6-v2`) that:

**Core behaviour**
- Loads a pre-trained model from local `models/all-MiniLM-L6-v2/` or auto-downloads
  from Hugging Face (with offline flags set in `main.py`)
- Auto-extracts a zipped model at startup if present
- Generates and caches intent embeddings (`intent_embeddings_cache.json` /
  `intent_embeddings_cache.npz`, regenerated on first run)
- Returns calibrated confidence scores using **softmax normalisation**
- Provides both semantic and pattern-based classification with automatic fallback

**Advanced capabilities**
- **Context-aware biasing**: adjusts confidence based on `QueryContext`
  (detected buildings, business terms)
- **Hybrid classification**: combines semantic similarity (70% mean + 30% max
  example) with pattern matching
- **Confidence threshold**: `INTENT_CONFIDENCE_THRESHOLD` (default `0.65`) triggers
  pattern fallback for low-confidence predictions
- **Graceful degradation**: falls back to pattern-only mode if the CT2 encoder is
  unavailable

**Intent types** (see `query_core/query_types.py`):
- `CONVERSATIONAL` (greetings, help requests)
- `MAINTENANCE` (PPM, jobs, requests)
- `RANKING` (largest, top N, comparisons)
- `PROPERTY_CONDITION` (derelict, condition A–D)
- `COUNTING` (how many, count)
- `SEMANTIC_SEARCH` (BMS config, FRA process, HVAC systems)

**Classification behaviour**
- Semantic confidence ≥ threshold → semantic classification with context biasing
- Semantic confidence < threshold → pattern-based classification fallback
- Context biasing nudges scores based on detected buildings and business terms
- If a handler declines during routing, the `QueryManager` escalates automatically

---

## 🧠 Core Architecture Overview

Alfred's query architecture follows a **modular, layered design**:

```
            ┌────────────────────────┐
            │      Streamlit UI      │
            └──────────┬─────────────┘
                       │
            ┌──────────▼─────────────┐
            │     QueryManager       │
            │  Hybrid Intent Router  │
            └──────────┬─────────────┘
                       │
       ┌───────────────┼────────────────────┐
       │ Rule Layer → Regex/Keyword Matching │
       │ ML Layer  → NLPIntentClassifier     │
       └───────────────┬────────────────────┘
                       │
    ┌──────────────────▼────────────────────┐
    │           Handlers Layer               │
    │ (Conversational / Property /           │
    │  Maintenance / Counting / Ranking /    │
    │  SemanticSearch)                       │
    └───────────────────┬────────────────────┘
                       │
                       ▼
            ┌────────────────────────┐
            │   search_core package  │
            └────────────────────────┘
```

---

## 🧠 Ingestion Architecture Overview

Alfred's ingestion path is driven by **document type** and the chosen upsert strategy:

```
            ┌────────────────────────┐
            │     Local Files        │
            └──────────┬─────────────┘
                       │
            ┌──────────▼─────────────┐
            │ secure file listing    │
            └──────────┬─────────────┘
                       │
            ┌──────────▼─────────────┐
            │   Building Resolution  │
            │(Property CSV + aliases)│
            └──────────┬─────────────┘
                       │
            ┌──────────▼─────────────┐
            │  DocumentProcessor     │
            │  (extract + chunk +    │
            │   FRA vector path)     │
            └──────────┬─────────────┘
                       │
            ┌──────────▼─────────────┐
            │   ingest orchestration │
            │    (IO/parse pools)    │
            └──────────┬─────────────┘
                       │
            ┌──────────▼─────────────┐
            │  upsert coordination   │
            │ (batch + flush policy) │
            └──────────┬─────────────┘
                       │
            ┌──────────▼─────────────┐
            │ Upsert: worker or inline│
            └──────────┬─────────────┘
                       │
                       ▼
            ┌────────────────────────┐
            │     Pinecone Index     │
            └────────────────────────┘
```

Files are processed by `ingest/document_processor.py`, which extracts text, chunks
it, and (for FRA candidates) routes through the FRA vector extraction path before
returning vectors. The upsert layer then batches and flushes vectors either
**inline** (direct upsert in the main thread) or via **worker** threads
(queue + worker), selected by the `UPSERT_STRATEGY` setting, with both paths
ultimately writing to Pinecone.

---

## 📁 Project Layout

```
AskAlfred/
├── main.py               # Streamlit entry point (poetry run streamlit run main.py)
├── core/                 # Shared infrastructure: clients, env bootstrap, sessions,
│                         #   Redis locks, Pinecone utils, date utils, exceptions
├── auth/                 # Authentication & authorisation: MSAL, credential manager,
│                         #   auth context, access control
├── security/             # Input/file validation, log & CSV sanitisation,
│                         #   context sanitisation, rate limiting
├── query_core/           # Query engine: QueryManager, intent classifier, query
│                         #   context/result/route/types
├── query_handlers/       # Chain-of-responsibility handlers, one per intent
├── query_preprocessors/  # Building/business-term extraction, spell check, complexity
├── search_core/          # Structured + semantic retrieval, answer generation,
│                         #   search instructions, structured-query detection
├── domain/               # Business terminology and maintenance-data helpers
├── building/             # Building cache, normalisation, resolution, filename parsing
├── fra/                  # Fire Risk Assessment parsing, triage and enrichment
├── ingest/               # Document ingestion pipeline (orchestration, transactions)
├── interfaces/           # Abstract ports (embedder, vector store, registries, sink)
├── ui/                   # Streamlit UI components and emoji constants
├── config/               # Settings and constants
├── cli/                  # Batch ingest / building resolution entry points
├── scripts/              # Security scan entry point (poetry script: security-scan)
├── tools/                # Developer tools and one-off analysis scripts
└── tests/                # Pytest suite
```

> `Alfred/`, `data/`, `logs/`, `models/`, `*.prom` metric files, and the intent
> embedding caches are local/runtime artefacts and are git-ignored.

---

## ⚙️ Key Components

| Module | Purpose |
|--------|---------|
| `main.py` | Streamlit entry point. Initialises caches, handles UI, logging, and session state. |
| `query_core/intent_classifier.py` | `NLPIntentClassifier` — CT2 encoder with context-aware biasing and calibrated confidence. |
| `query_core/query_manager.py` | Routes user input to the appropriate handler using a priority-based system; integrates the intent classifier. |
| `query_core/query_context.py` | Encapsulates query metadata (buildings, business terms, complexity). |
| `query_core/query_types.py` | Enum of supported query intents (CONVERSATIONAL, MAINTENANCE, RANKING, …). |
| `query_core/query_result.py` / `query_route.py` | Standard result schema and routing record. |
| `query_handlers/base_handler.py` | Abstract base for all handlers with consistent logging and metadata extraction. |
| `query_handlers/conversational_handler.py` | Greetings, about queries, and small talk. |
| `query_handlers/counting_handler.py` | Counting queries ("How many buildings have FRAs?"). |
| `query_handlers/maintenance_handler.py` | Maintenance requests, jobs, and categories. |
| `query_handlers/property_handler.py` | Property condition and derelict-building queries. |
| `query_handlers/ranking_handler.py` | "Largest/smallest/top" building queries. |
| `query_handlers/semantic_search_handler.py` | Fallback handler — Pinecone semantic retrieval + OpenAI summarisation. |
| `query_preprocessors/` | Building/business-term extraction, spell checking, complexity analysis. |
| `search_core/search_router.py` | Unified entry point for structured and semantic searches. |
| `search_core/search_instructions.py` | `SearchInstructions` dataclass carrying structured search intent. |
| `search_core/planon_search.py` | Property / Planon structured queries. |
| `search_core/maintenance_search.py` | Structured maintenance vector lookups. |
| `search_core/semantic_search.py` | Semantic vector retrieval. |
| `search_core/generate_semantic_answer.py` / `generate_maintenance_answers.py` | OpenAI answer generation. |
| `search_core/structured_queries.py` | Rule-based detection for counting, ranking, maintenance, and property queries. |
| `search_core/search_utils.py` | Boosting, deduplication, and building-filter utilities. |
| `building/utils.py` (+ `cache.py`, `resolver.py`, `normaliser.py`) | Building cache, alias and fuzzy matching, metadata filters. |
| `core/clients.py` | Centralised OpenAI / Redis client management. |
| `config/constant.py` | Constants for environment, models, and routing configuration. |
| `config/settings.py` | Environment-driven API keys and Pinecone/OpenAI/Redis configuration. |

---

## 🧩 Smart Query Routing

Alfred uses a **Chain of Responsibility pattern** via the `QueryManager`:

1. **Preprocessing**: extracts buildings, business terms, and analyses query complexity
2. **Intent classification**: `NLPIntentClassifier` predicts intent with a confidence score
3. **Handler selection**: each handler declares a `priority` (lower number = higher priority)
4. **Execution**: the `QueryManager` checks each handler's `can_handle()` method in order
5. **Fallback**: `SemanticSearchHandler` handles all remaining unclassified queries

Example:
```text
"Hi Alfred"                                    → ConversationalHandler  (priority: 1)
"Which buildings have maintenance requests?"   → MaintenanceHandler     (priority: 2)
"Top 10 largest buildings"                     → RankingHandler         (priority: 3)
"Which buildings are derelict?"                → PropertyHandler        (priority: 4)
"How many buildings have FRAs?"                → CountingHandler        (priority: 5)
"Describe frost protection in Berkeley Square" → SemanticSearchHandler  (priority: 99)
```

---

## 🧱 search_core Layer

The `search_core` package provides a **unified structured + semantic retrieval system**.

### `SearchInstructions`
```python
@dataclass
class SearchInstructions:
    type: str           # "semantic", "planon", "maintenance"
    query: str
    top_k: int
    building: str | None = None
    document_type: str | None = None
```

Handlers construct a `SearchInstructions` object when a search is needed, and the
router dispatches to the correct backend automatically:

```python
from search_core.search_router import execute

results, answer, pub_date, score_flag = execute(SearchInstructions(
    type="semantic",
    query="Fire Risk Assessment for Senate House",
    top_k=5,
))
```

---

## 🗝️ Building Cache & Matching

The `building/` package is the single source of truth for building identity:

- Alias and canonical name mapping (`building/alias_override.py`, `normaliser.py`)
- Multi-index cache population (`building/cache.py`)
- Fuzzy matching and validation (`building/utils.py`, `validation.py`)
- Building resolution and filename parsing (`resolver.py`, `filename_building_parser.py`)
- Metadata filter generation for Pinecone

The building cache is initialised at app startup so fuzzy and alias-based matches
are available to every handler.

---

## 🔧 Ingestion Pipeline (V3)

The V3 ingestion pipeline focuses on reliability, idempotency, and observability.

**Core design**
- **Interfaces layer** (`interfaces/`) defines ingestion ports: `VectorStore`,
  `Embedder`, `EventSink`, `IngestFileRegistry`, `JobRegistry`.
- **Redis-backed registries** for files and jobs, with status/TTL handling and
  atomic lease semantics.
- **File state machine** with explicit states (discovered → processing → upserted
  → verified → success/failed) and a per-run `processing_token` enforced on state
  transitions.
- **VectorStore abstraction** wraps Pinecone calls and normalises error handling.
- **Embedder wrapper** owns retries/backoff/batch splitting and returns explicit
  index → embedding/error mappings.
- **Upsert scheduling** runs inline or via worker threads (`UPSERT_STRATEGY`).
- **Verification** uses the VectorStore abstraction; failures emit structured events.

**FRA supersession**
- Supersession is handled atomically across `ingest/transaction.py`
  (`FraSupersessionTxnLog`) and `fra/integration.py` (`mark_superseded_risk_items`,
  `restore_superseded_items`), with SETNX-style job semantics to avoid duplicate runs.

**Redis**
- File records are stored as **hashes** (not JSON blobs) with status-based TTLs.

---

## 🔥 FRA (Fire Risk Assessment) Module

The `fra/` package provides structured extraction and prioritisation for FRAs.

| Component (file) | Purpose |
|------------------|---------|
| `FRAActionPlanParser` (`parser.py`) | Extracts risk items from FRA PDFs using regex and structure analysis. |
| `parse_helpers/` (`parse_row.py`, `parse_section.py`, `parse_table.py`) | Low-level table/section/row parsing helpers. |
| `FRATriageComputer` (`triage.py`) | Computes a deterministic numeric risk score for ranking. |
| `FRATriageReporter` (`triage.py`) | Summarises triage outcomes for logging/reporting. |
| `FRAEnricher` (`enrichment.py`) | Enriches extracted items with computed fields (scores, flags, normalised values). |
| `FRAMetadata` (`doc_metadata.py`) | Document-level FRA metadata (building, dates, source). |
| `ParsingConfidence` (`types.py`) | Tracks extraction reliability per field and per document. |

### Triage scoring

`FRATriageComputer` produces a numeric **risk score (0–100), where higher = more
urgent**, used for ranking risk items. The base score is derived from the item's
risk level via `FRA_RISK_BASE_SCORES` (an escalating scale, roughly
1→10, 2→20, 3→40, 4→70, 5→100), and is reduced for completed/closed items. Items
without a job reference are adjusted by `NO_JOB_REF_SCORE_MULTIPLIER`. The exact
weights live in `config/constant.py` (`FRA_RISK_BASE_SCORES`, `FRA_RISK_SCORE_MAX`,
`FRA_PRIORITY_HIGH_RISK_LEVEL`, `FRA_PRIORITY_MEDIUM_RISK_LEVEL`).

Each FRA risk item is embedded and upserted to Pinecone as a vector with
`document_type = "fra_action_item"` metadata (building, risk level, category,
action required, location, dates, and a `superseded` flag), in the FRA risk-items
namespace.

---

## 🔒 Security & Auth

Alfred applies defence-in-depth across the `security/` and `auth/` packages.

### Authentication & access control (`auth/`)
- **MSAL / Microsoft Entra ID** sign-in (`msal_auth.py`, `auth_manager.py`)
- **Auth context** and **role-based access control** (`auth_context.py`, `access_control.py`)
- **Credential manager** for secure, lazily-loaded secrets (`credential_manager.py`)
- Behaviour is driven by `REQUIRE_AUTH`, `ALLOW_ANONYMOUS_DEV`, `AUTH_STRICT_TENANT`,
  `AZURE_TENANT_ID`, `AUTH_REDIRECT_URI`, and `AUTH_SCOPES`.

### Input validation (`security/input_validator.py`)
- Prompt-injection pattern detection, length limits, and ratio-based special-/
  suspicious-character and repeated-character (DoS) checks. It also sanitises
  Pinecone metadata filters (allowed operators only; dangerous keys/values stripped).

```python
from security.input_validator import validate_query_security, get_validation_summary

result = validate_query_security(user_query)
if not result.is_valid:
    logger.warning(f"Blocked query: {result.rejection_reason}")
```

### File operations security (`security/file_operations_validator.py`)
- Path-traversal prevention, symlink protection, extension allow-listing, size
  limits, and filename sanitisation.

```python
from security.file_operations_validator import (
    validate_path_safety,
    is_safe_extension,
    read_file_safe,
    validate_file_safety,
)

safe_path = validate_path_safety(base_directory, user_provided_path)
if is_safe_extension(filename):
    content = read_file_safe(base_directory, relative_path)
```

### Other security layers
- **Rate limiting** (`security/rate_limiter.py`) — Redis-backed request limits.
- **Log sanitisation** (`security/log_sanitiser.py`) — redacts PII/credentials from logs.
- **CSV sanitisation** (`security/csv_sanitiser.py`) — neutralises CSV-injection (formula) payloads.
- **Context sanitisation** (`security/sanitise_context.py`) — safe rendering of search results in the UI.

---

## 📊 Metrics & Observability

- **Prometheus text-file metrics** are written by the ingestion pipeline
  (`ingest/utils.py`) using the `askalfred_ingest_*` namespace, e.g.
  `askalfred_ingest_files_processed`, `askalfred_ingest_files_failed`,
  `askalfred_ingest_total_vectors`, `askalfred_ingest_duration_seconds`,
  `askalfred_ingest_vectors_per_second`. Output files use the `.prom` extension
  (git-ignored) and can be picked up by the Prometheus node-exporter textfile
  collector.
- **Live service metrics** are refreshed by a single process-wide publisher at
  `SERVICE_METRICS_FILE`. They include request outcomes, component readiness,
  and `askalfred_metrics_export_timestamp_seconds` so an operator can detect a
  stale snapshot after the Streamlit process stops. Configure the refresh rate
  with `SERVICE_METRICS_INTERVAL_SECONDS` (default: 15 seconds).
- **JSONL event sink** (`interfaces/event_sink.py`) records structured ingestion
  and verification events; analyse them with `tools/analyse_events_jsonl.py`.
  Event export is gated by `EXPORT_EVENTS`. Failed writes are retained in a
  durable local spool and replayed before the next event; configure its separate
  location with `EVENT_SPOOL_FILE`.

---

## 🧰 Developer Guide

### Environment setup

```bash
poetry install
poetry run streamlit run main.py
```

A pinned `requirements.txt` is committed for external tooling; regenerate it with
`poetry export -f requirements.txt -o requirements.txt --without-hashes` rather
than editing it by hand. Python 3.10–3.12 is supported.

### Required environment variables

At minimum, Alfred needs OpenAI, Pinecone, and Redis configuration:

```bash
OPENAI_API_KEY=your_openai_key
PINECONE_API_KEY=your_pinecone_key

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_USERNAME=optional
REDIS_PASSWORD=optional

ANSWER_MODEL=gpt-4o-mini             # OpenAI answer/summarisation model
DEFAULT_EMBED_MODEL=text-embedding-3-small
INDEX_NAME=your_pinecone_index
LOG_LEVEL=INFO
```

### Optional / advanced settings

| Group | Variables |
|-------|-----------|
| Environment & local `.env` | `ENVIRONMENT`, `ALLOW_LOCAL_ENV` |
| Auth (MSAL / Entra) | `REQUIRE_AUTH`, `ALLOW_ANONYMOUS_DEV`, `AUTH_STRICT_TENANT`, `AZURE_TENANT_ID`, `AUTH_REDIRECT_URI`, `AUTH_SCOPES` |
| Ingest access defaults | `INGEST_DEFAULT_ACCESS_LEVEL`, `INGEST_DEFAULT_ALLOWED_ROLES` |
| Ingest tuning | `LOCAL_PATH`, `EMBED_MODEL`, `DIMENSION`, `CHUNK_TOKENS`, `CHUNK_OVERLAP`, `EMBED_BATCH`, `UPSERT_BATCH`, `DEDUP_FETCH_BATCH`, `MAX_FILE_MB`, `SKIP_EXISTING`, `SKIP_SUCCESSFUL_ONLY`, `UPSERT_STRATEGY` |
| Concurrency | `MAX_IO_WORKERS`, `MAX_PARSE_WORKERS`, `UPSERT_WORKERS`, `MAX_PENDING_VECTORS`, `UPSERT_FLUSH_SECONDS` |
| OpenAI timeouts | `OPENAI_TIMEOUT`, `OPENAI_CONNECT_TIMEOUT`, `OPENAI_READ_TIMEOUT`, `OPENAI_WRITE_TIMEOUT`, `OPENAI_POOL_TIMEOUT` |
| Safety limits | `MAX_FILE_SECONDS`, `MAX_MEMORY_MB`, `MAX_METADATA_SIZE`, `DRY_RUN` |
| Redis behaviour | `DECODE_RESPONSES`, `HEALTH_CHECK_INTERVAL`, `REDIS_DB`, `REDIS_SSL`, `REDIS_SOCKET_TIMEOUT`, `REDIS_SOCKET_CONNECT_TIMEOUT`, `REDIS_HEALTH_CHECK_INTERVAL` |
| Observability | `EXPORT_EVENTS`, `EXPORT_EVENTS_FILE`, `EVENT_SPOOL_FILE`, `PROMETHEUS_METRICS_FILE`, `SERVICE_METRICS_FILE`, `SERVICE_METRICS_INTERVAL_SECONDS`, `ASKALFRED_LOG_FILE`, `ASKALFRED_LOG_MAX_BYTES`, `ASKALFRED_LOG_BACKUP_COUNT`, `PROGRESS_LOG_INTERVAL` |
| Feature flags | `ENABLE_SERVICE_STATUS`, `VALIDATE_BUSINESS_TERMS` |

**Local `.env` loading**
- Repository `.env` loading is **disabled by default**.
- `.env` is only loaded when `ALLOW_LOCAL_ENV=true` **and** `ENVIRONMENT=development`.
- `.env` is ignored in `staging` and `production`, even with `ALLOW_LOCAL_ENV=true`.
- Real environment variables always take precedence over values in `.env`.

### Model files

The `NLPIntentClassifier` expects:
- **Local model**: `models/all-MiniLM-L6-v2/` (auto-extracted from a `.zip` if present)
- **Cache**: `intent_embeddings_cache.json` / `.npz` (auto-generated on first run; not committed)
- **Fallback**: auto-downloads from Hugging Face if the local model is not found

### Client management (`core/clients.py`)

```python
from core.clients import ClientManager, get_oai, get_redis

openai = get_oai()                 # OpenAI client with configured timeouts
redis = ClientManager.get_redis()  # Redis client with connection pooling
```

### Logging
- Configured globally in `main.py` via `logging.basicConfig()`.
- All handlers inherit their logger from `BaseQueryHandler`.
- The Streamlit log level is forced to INFO via `STREAMLIT_LOG_LEVEL=info`.
- When `ASKALFRED_LOG_FILE` is set, the app attaches one UTF-8 rotating file
  handler shared across Streamlit sessions. Defaults are 10 MiB per file and
  five backups; every handler uses `SanitisedFormatter` before persistence.

---

## 🧪 Testing

```bash
# Run all tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=. --cov-report=html

# Run a specific module
poetry run pytest tests/test_fra_triage.py -v

# Security-focused tests
poetry run pytest tests/test_file_operations_validator.py tests/test_input_validator.py -v
```

Test discovery is configured in `pyproject.toml` (`testpaths = ["tests"]`). The
suite covers auth, the FRA parser/triage, ingestion, building parsing, the query
manager, and the security/sanitisation layers, with shared fixtures in
`tests/conftest.py`.

---

## 🔐 Security Scanning

```bash
# Full security scan (poetry script defined in pyproject.toml)
poetry run security-scan --json --strict
# Equivalent: poetry run python scripts/security_scan.py --json --strict

# Individual tools
poetry run safety scan --target . --policy-file .safety-policy.json
poetry run pip-audit
poetry run bandit -r . -ll
```

The `.github/workflows/security-scan.yml` workflow runs on pull requests and
combines dependency scanning (`safety`, `pip-audit`), static analysis (`bandit`),
and Poetry lock validation.

---

## 🛠️ Tools Directory

Developer and debugging utilities live in `tools/`:

| Tool | Purpose |
|------|---------|
| `extract_pdf_text.py` | Extract raw text from a PDF for debugging. |
| `extract_index_tocsv.py` | Export Pinecone index contents to CSV. |
| `profile_intent.py` | Profile intent-classifier performance. |
| `analyse_events_jsonl.py` | Summarise the JSONL ingestion event log. |
| `parse_goldneyhall_action_plan_from_full_text.py` | Exercise FRA parsing on a sample document. |
| `word_to_pdf.py` | Convert Word documents to PDF. |
| `readme_to_pdf.py` | Render this README to PDF. |
| `redis_test.py` | Quick Redis connectivity check. |

The `cli/` package provides operational entry points: `cli/local_batch_ingest.py`
(batch ingestion) and `cli/resolve_buildings.py` (building resolution).

Batch ingestion uses stable automation exit codes: `0` success (including
skips), `2` empty/validation-only, `3` partial, `4` retryable dependency
unavailable, `5` failed/cancelled, and `10` critical inconsistent. Recovery
commands are:

```powershell
python -m cli.local_batch_ingest --reconcile-fra
python -m cli.local_batch_ingest --reconcile-fra TRANSACTION_ID
python -m cli.local_batch_ingest --reconcile-registry
```

FRA reconciliation reads the durable Redis transaction journal. Registry
reconciliation replays the local JSONL divergence spool (configured with
`REGISTRY_RECONCILIATION_FILE`). Both commands are idempotent and retain
unresolved records for a later retry.

---

## 🧪 Example Queries

| Query | Predicted intent | Handler |
|-------|------------------|---------|
| "Hi Alfred" | CONVERSATIONAL | ConversationalHandler |
| "Which buildings have FRAs?" | COUNTING | CountingHandler |
| "Show maintenance for Senate House" | MAINTENANCE | MaintenanceHandler |
| "Top 5 largest buildings by area" | RANKING | RankingHandler |
| "Which buildings are derelict?" | PROPERTY_CONDITION | PropertyHandler |
| "Show the AHU logic in Senate House" | SEMANTIC_SEARCH | SemanticSearchHandler |

---

## 🧩 Design Principles

- **Separation of concerns** — handlers decide *what* to do; `search_core` decides *how*.
- **Extensibility** — add new handlers (e.g. an `EnergyHandler`) without touching core logic.
- **Transparency** — every query logs its route and detection path.
- **Consistency** — all results conform to the `QueryResult` schema.
- **Context awareness** — intent classification considers extracted buildings and business terms.
- **Graceful degradation** — falls back to pattern matching when the ML model is unavailable.

---

## 📝 License

Internal use only — University of Bristol Smart Buildings Team
© 2025 University of Bristol
