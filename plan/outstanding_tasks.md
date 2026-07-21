# Outstanding tasks

This file records the work that remains after reviewing
`plan/failure_and_degraded_states_plan.md` against the current implementation.
The plan currently marks Phases 0–5 as complete, but the items below are either
not implemented, only partially implemented, or still require operational
rollout.

## Structured-outcome gaps

### ROUTE-10 — Represent conversation-memory failure

- Mark the turn `degraded/conversation_memory` when persistence fails.
- Notify the user only when a later follow-up depends on the missing context.
- Current behavior only logs the persistence error.

## Ingestion and vector gaps

### INGEST-08 — Complete empty/review outcomes

- Add distinct `empty_document` and `unsupported_layout` outcomes.
- Keep `fra_no_action_plan` separate from technical extraction failure.
- Add terminal-state and CLI-summary tests for all three cases.
- Only `fra_no_action_plan` currently has a distinct review reason.

### VECTOR-04 — Retry and alert on embedding response mismatch

- Retry a provider response-size mismatch once when safe.
- Then mark the file partial or failed and emit an operator alert.
- Evidence: `interfaces/embedder.py:178` immediately records
  `response_size_mismatch` without the planned retry or alert.

### VECTOR-06 — Bound aggregate upsert retries

- Add a maximum total retry budget across retries and recursive batch splits.
- Publish idempotency and retry metrics.
- Verify that exhausting the budget produces an explicit terminal outcome.

### VECTOR-13 — Add stale-writer telemetry

- Emit a stable metric whenever a processing token rejects a stale terminal
  transition.
- Test every terminal transition, not only selected state-precedence cases.

### VECTOR-15 — Handle observability failures explicitly

- Mark metrics or event-export failures as observability degradation.
- Add durable local spooling and replay where event retention is required.
- Current metrics export failure handling only logs a warning.

## Remaining silent-failure debt

Remove or replace every entry still permitted by
`tests/test_failure_acceptance_inventory.py::SILENT_FAILURE_BASELINE`.
The allowlist currently contains broad-exception paths that return `[]`, `None`,
or nominal success, including:

- `core/pinecone_utils.py::list_index_names`
- `core/pinecone_utils.py::query_all_chunks`
- `search_core/structured_queries.py::_query_index_with_batches`
- `core/date_utils.py::_fetch_document_chunks`
- `ingest/document_content.py::extract_maintenance_csv`
- `ingest/upsert_handler.py::Dispatcher._execute_inline`

This debt conflicts with the Phase 5 exit criterion that silent-empty failure
paths have been removed and with the overall definition of done.

## Test coverage gaps

- Replace schema-only P0/P1 acceptance ownership with behavioral fault tests.
- Ensure each P0/P1 test triggers the named failure and asserts its terminal
  status, stable failure code, retryability, telemetry, and user treatment.
- Expand Phase 5 fault-injection integration tests beyond Pinecone index-open
  and Redis to every named seam.

## Operational rollout still required

These activities are explicitly acknowledged as deployment-time work in the
plan and have not been completed by repository code alone:

1. Deploy and connect Prometheus, Alertmanager, and Grafana to the exported
   metrics and `ops/askalfred_alerts.yml`.
2. Run the full fault-injection matrix in a live non-production environment for
   OpenAI embedding/answers, Pinecone index/query, Redis, auth callback,
   registry write, queue drain, and FRA rollback.
3. Capture a real traffic baseline and compare empty, partial, unavailable,
   failed, and degraded rates using `tools/compare_outcome_rates.py`.
4. Record operator approval of the stable user-facing copy and alert thresholds.
5. Run the AUTH-10 ACL audit against every target namespace, review the
   privacy-safe report, quarantine or re-ingest every non-conformant vector, and
   capture evidence that the deployment meets `ACL_CONFORMANCE_THRESHOLD`.

## Recently completed

- **INGEST-06:** Text extraction now reports lossy decoding. When UTF-8 fails
  and a Latin-1 or `errors="ignore"` fallback is used (txt/md, json, doc, and
  the CSV raw-text fallbacks), `extract_text_with_provenance` returns a stable
  reason (`encoding_fallback_latin1` / `encoding_fallback_ignore`) that survives
  the extract/chunk process-pool boundary. The reason is recorded via
  `note_file_outcome(file_id, "degraded", reason)`, so the file finishes on a
  new `degraded` terminal status instead of claiming full-fidelity `success`;
  `partial` (missing vectors) and `needs_review` still outrank it. The registry
  Lua and `record_file_terminal` prevent a later `success` from erasing a
  `degraded` file and stop `degraded` from masking a worse `partial`/`failed`.
  Run aggregation surfaces a `DEGRADED` run status (exit code `0`, since all
  vectors were committed) and an operator-visible `files_degraded` count. The
  legacy `extract_text` string API is retained for non-ingestion callers.
- **ROUTE-04:** Exceptions from rule-layer and ML handler negotiation now create
  safe request-scoped records with the failed handler and phase, routing notes,
  and fallback telemetry; exception details remain in logs only. A rule failure
  is material when that handler outranks the selected fallback, while an
  ML-selected handler failure is always material. Material uncertainty
  downgrades otherwise trustworthy results to `degraded`; lower-priority or
  transient failures are retained without a user warning, and affected requests
  are never cached. Normal handler rejection remains a healthy fallback and is
  distinguished from `handler_error` in route metadata.
- **ROUTE-02:** Invalid or maintenance-like extracted building scope is now
  removed without clearing a separate valid explicit filter and recorded as
  `building_scope_discarded` in request context, routing notes, fallback
  telemetry, and result metadata. Queries with an invalid explicit filter or
  unmistakable natural-language building scope stop before retrieval with a
  typed `rejected/input.building_scope_invalid` clarification; incidental
  maintenance terms continue without an unnecessary warning.
- **ROUTE-01:** Preprocessor exceptions now record a stable component name in
  request context, routing notes, fallback telemetry, result metadata, and
  `degraded_components`, while later preprocessors continue. Missing building or
  business-term context downgrades otherwise trustworthy non-conversational
  results to `degraded`; existing building scope and conversational answers are
  retained without a user warning. Spell-check failures now reach this shared
  boundary, and failed-preprocessing requests are not cached.
- **START-09 / START-10:** A controlled-startup dependency check
  (`core/startup_readiness.py`, wired into `main.py`) validates OpenAI and
  Pinecone credentials and the Redis host/port/timeout configuration once and
  publishes coarse component readiness for each. Required dependencies (OpenAI,
  Pinecone) that are unconfigured are marked `unavailable`; the optional Redis
  dependency (query rate limiting fails open, ingestion requires it) is marked
  `degraded`. `QueryManager.process_query` now maps a missing required
  dependency to a typed `unavailable/dependency.unavailable` outcome before the
  query executes. The detailed configuration cause stays in logs/operator
  diagnostics; the readiness surface and user-facing result carry only the
  coarse state and stable failure code.
- **INPUT-10:** Semantic queries below either the character or word threshold
  now return a non-retryable `rejected/input.insufficient_detail` outcome.
  Parameterised handler tests cover both threshold branches and ensure neither
  can regress to the default `success` status.
- **ROUTE-09 / SEARCH-19:** `QueryManager.process_query` now converts unexpected
  exceptions from every registered handler into a transport-safe `failed`
  `QueryResult` with the stable `handler.execution_failed` code and an opaque
  correlation reference. Parameterised behavioral coverage verifies the same
  serialisable result contract for UI, API, and direct Python callers.
- **AUTH-10 / Phase 3 item 5:** The ingestion CLI now provides an audit-by-default
  ACL reconciliation workflow across current and legacy namespaces, an explicit
  quarantine action, bounded vector fetching, post-action threshold
  verification, privacy-safe reports and low-cardinality telemetry. The
  deployment threshold defaults to full conformance (`1.0`) and can be set with
  `ACL_CONFORMANCE_THRESHOLD`.

## Completion criteria

This backlog is complete when:

- Every item above has behavioral automated coverage.
- No dependency failure is translated into a genuine empty result.
- Every material fallback is represented on the affected operation outcome.
- ACL conformance reaches the agreed threshold after quarantine or re-ingestion.
- The silent-failure baseline is empty.
- Monitoring, fault-injection, and traffic-baseline evidence has been captured
  from the target non-production or production-like environment.
