# Outstanding tasks

This file records the work that remains after reviewing
`plan/failure_and_degraded_states_plan.md` against the current implementation.
The plan currently marks Phases 0–5 as complete, but the items below are either
not implemented, only partially implemented, or still require operational
rollout.

## High-priority implementation gaps

### ROUTE-09 / SEARCH-19 — Convert handler exceptions into typed outcomes

- Guard handler execution inside `QueryManager.process_query`.
- Convert unexpected handler errors into a structured `failed` result with a
  stable failure code and correlation reference.
- Ensure API and non-Streamlit callers receive the same typed result contract.
- Add behavioral tests for exceptions from every handler stage.

### AUTH-10 / Phase 3 item 5 — Remediate non-conformant ACL vectors

- Build an operator workflow that identifies vectors missing the required ACL
  envelope.
- Quarantine or re-ingest each affected vector.
- Record reconciliation status and retain privacy-preserving telemetry.
- Define and verify the required deployment conformance threshold.
- Current code measures and alerts on ACL metadata drops, but does not perform
  the required quarantine or re-ingestion.

### START-09 / START-10 — Complete dependency readiness checks

- Validate required OpenAI, Pinecone, and Redis configuration once at startup.
- Publish readiness for each required and optional component.
- Map missing required dependencies to `unavailable` before query execution.
- Keep configuration details in logs and operator diagnostics only.
- Current readiness updates cover rate limiting, the building directory, and
  the intent classifier, but not all required services.

## Structured-outcome gaps

### INPUT-10 — Reject insufficient semantic detail explicitly

- Return `rejected/insufficient_detail` for semantic queries below the handler's
  character or word threshold.
- Do not rely on the default `QueryResult` status of `success`.
- Add tests for both short-query branches.
- Evidence: `query_handlers/semantic_search_handler.py:80` returns a default
  success result for both branches.

### ROUTE-01 — Record preprocessor degradation

- Record which preprocessor failed.
- Attach the degradation to the affected request.
- Warn only when the failure can materially change or narrow the answer.
- Current behavior logs the exception and continues with a nominally healthy
  request.

### ROUTE-02 — Handle discarded building scope

- Record `building_scope_discarded` when an extracted building is rejected.
- Ask for clarification when the query clearly depends on that building.
- Current behavior only records the internal routing note
  `invalid_building_cleared`.

### ROUTE-04 — Preserve handler-negotiation failure

- Record failures from `can_handle()` as partial routing degradation.
- Prevent a fully healthy outcome when the failed handler could have been
  authoritative.
- Current behavior logs the error and skips the handler.

### ROUTE-05 — Attach classifier degradation to the result

- Add the intent classifier to `degraded_components` when its failure affects
  routing.
- Downgrade otherwise trustworthy affected results to `degraded`.
- Current code updates global readiness and fallback telemetry but does not
  consistently change the request result.

### ROUTE-10 — Represent conversation-memory failure

- Mark the turn `degraded/conversation_memory` when persistence fails.
- Notify the user only when a later follow-up depends on the missing context.
- Current behavior only logs the persistence error.

## Ingestion and vector gaps

### INGEST-06 — Report decoding degradation

- Record when Latin-1 or ignored-error decoding is used.
- Mark extraction degraded so ingestion does not claim full fidelity.
- Preserve a stable encoding-fallback reason for operators.
- Evidence: `ingest/document_content.py:254` and related paths still use
  fallback decoding or `errors="ignore"` without an outcome.

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

## Completion criteria

This backlog is complete when:

- Every item above has behavioral automated coverage.
- No dependency failure is translated into a genuine empty result.
- Every material fallback is represented on the affected operation outcome.
- ACL conformance reaches the agreed threshold after quarantine or re-ingestion.
- The silent-failure baseline is empty.
- Monitoring, fault-injection, and traffic-baseline evidence has been captured
  from the target non-production or production-like environment.
