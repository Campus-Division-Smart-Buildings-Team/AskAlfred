# Outstanding tasks

This file records completion evidence after reviewing
`plan/failure_and_degraded_states_plan.md` against the current implementation.
Repository implementation and automated coverage are complete; the remaining
items require operational rollout in a target environment.

## Repository work complete

The remaining code and automated-test debt has been completed:

- `SILENT_FAILURE_BASELINE` is empty and the AST acceptance test now requires it
  to remain empty. Pinecone catalogue/query failures, structured retrieval,
  document-date fallback, maintenance CSV parsing, and inline upserts no longer
  translate broad exceptions into an empty/nominal-success result. The six
  additional historical scanner entries were removed at the same time.
- Every P0/P1 register entry is exercised through its terminal status, stable
  failure code, registered retryability, low-cardinality telemetry, and safe
  presenter treatment. The old schema-only ownership test is no longer the
  acceptance owner.
- Automated fault-injection coverage now reaches every named Phase 5 seam:
  OpenAI embedding/answer, Pinecone index-open/query, Redis, auth callback,
  registry write, queue drain, and FRA rollback.
- A rollback-mechanism outage now enters `critical_inconsistent`, blocks the
  affected FRA scope, emits integrity telemetry, and raises the stable critical
  failure type. Auth callback failures now emit a dedicated terminal auth metric.
- `ops/rollout_evidence.example.json` and
  `tools/validate_rollout_evidence.py` make the remaining deployment evidence a
  deterministic completion gate rather than an informal checklist.
- The app-side observability runtime is built and wired. `core/observability_runtime.py`
  runs a single process-wide service-metrics publisher (started from `main.py`)
  that atomically snapshots request-outcome, auth-outcome, and component-readiness
  telemetry to a Prometheus textfile (`SERVICE_METRICS_FILE`), alongside bounded
  rotating file logging (`ASKALFRED_LOG_FILE`). This produces scrape-ready metrics
  and the generated `ops/askalfred_alerts.yml` rules, but does not itself deploy or
  connect an external Prometheus/Alertmanager/Grafana stack — see the split rollout
  item below. Covered by `tests/test_observability_runtime.py` and the exporter/alert
  tests in `tests/test_phase5_operational_rollout.py`.

## Operational rollout still required

These activities are explicitly acknowledged as deployment-time work in the
plan and have not been completed by repository code alone:

1. Monitoring stack. The app-side exporter is **built** (see "Repository work
   complete" above): `core/observability_runtime.py` publishes scrape-ready
   Prometheus textfiles and the repository ships `ops/askalfred_alerts.yml`. Still
   **pending deployment**: stand up Prometheus, Alertmanager, and Grafana in the
   target environment, point Prometheus at the exported textfile metrics, load
   `ops/askalfred_alerts.yml` into Alertmanager, and build/connect Grafana
   dashboards. No compose/`prometheus.yml`/dashboard definitions live in the
   repository yet, and the rollout-evidence gate still requires
   `monitoring.{prometheus,alertmanager,grafana}_connected`.
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

- **VECTOR-13:** Stale-writer rejections by the file registry's token guard are
  now observable. The atomic mark-state Lua script returns a stable rejection
  reason (`stale_terminal_token`, `state_precedence`, or `stale_processing_token`)
  and `RedisIngestFileRegistry.mark_state` emits a low-cardinality
  `ingest_stale_writer_total{reason}` metric on every rejection — at the single
  registry choke point, so it covers all callers rather than only the batch-level
  path that previously string-matched the exception. The guard decision is
  mirrored by a pure `classify_mark_state_transition` contract function that is
  the single tested definition (the Lua cannot run without a live Redis). The
  exception message keeps its legacy `token mismatch` text so existing batch-level
  counters are unchanged. Covered by a full terminal-transition matrix (every
  current→new pair with matching and stale tokens, plus the processing-token
  case) driven through `mark_state` via a faithful in-memory fake that reuses the
  contract function.
- **VECTOR-15:** Metrics and event-export errors now cross a shared ingestion
  observability boundary instead of being reduced to warnings. A failure records
  stable run stats, `ingest_integrity_total{event=observability,state=...}` and
  `service_degraded_total{component=observability,code=observability.export_failed}`
  telemetry, marks observability readiness degraded, and annotates the
  `IngestReport`; otherwise healthy runs finish `degraded` while worse data-path
  outcomes retain their primary status. All ingestion event producers now use
  this boundary, so alerting remains independent of vector writes. The JSONL
  sink durably flushes failed events to the separately configurable
  `EVENT_SPOOL_FILE` (default `logs/ingest_event_spool.jsonl`) and replays the
  spool before the next live event with at-least-once delivery; metrics remain
  replaceable snapshots and are not spooled. Covered by behavioral tests for
  metrics degradation, retained event failures, and ordered spool replay.
- **VECTOR-06:** The upsert path now enforces a single aggregate retry budget
  (`INGEST_UPSERT_MAX_TOTAL_RETRIES`, default `6`) across both retries and
  recursive batch splits. Previously each split reset the per-batch retry
  allowance (`INGEST_RETRY_ATTEMPTS`), so a persistently retryable batch could
  accumulate a large, effectively unbounded total number of retries down its
  split tree. `UpsertQueueItem` now carries a `retries_consumed` count that a
  split passes to both children (and a retry increments), so splitting no longer
  refreshes the budget; the inline path threads the same count and now also
  propagates `split_depth` through its recursion. `UpsertPolicy.next_action`
  fails with the stable terminal reason `upsert_retry_budget_exhausted` once the
  lineage spends the budget, in both the worker and inline paths. Exhaustion is
  observable via an `upsert_retry_budget_exhausted_total` run stat and
  low-cardinality `ingest_integrity_total{event=upsert,state=retry_budget_exhausted}`
  telemetry; retry replay cost is published as `upsert_idempotent_rewrites_total`
  (idempotent-by-ID rewrites counted per retried batch). Covered by policy-level
  budget tests plus worker tests for split inheritance, terminal budget
  exhaustion, and the idempotent-rewrite metric.
- **VECTOR-04:** A provider embedding response whose length differs from the
  request is now treated as a contract breach rather than an immediate failure.
  `OpenAIEmbedder.embed_texts` re-issues the batch once (the call is idempotent,
  so the retry is always safe); a healthy retry recovers every embedding, and a
  mismatch that survives the single retry is recorded per item as
  `response_size_mismatch` so the file finishes `partial` (some chunks succeeded)
  or `failed` (none did) through the existing downstream handling —
  `INGEST_EMBEDDING_RESPONSE_INVALID` in the acceptance contract. The retry is
  bounded to one attempt, not the transient-error retry budget, and does not
  trigger adaptive batch reduction. `EmbeddingsResult` now carries
  `response_mismatch_retries` and `response_mismatch_batches`. At the ingestion
  boundary, `embed_texts_batch` increments `embed_response_mismatch_retries_total`
  and, on a persistent mismatch, emits an operator alert:
  `embed_response_mismatch_total` run stats, low-cardinality
  `ingest_integrity_total{event=embedding,state=response_mismatch}` telemetry, an
  operator-facing error log, and an `embed_response_size_mismatch` event via the
  event sink. Covered by embedder retry/recovery and persistence tests plus an
  ingestion-boundary alert test.
- **INGEST-08:** A file that produces no usable vectors now finishes
  `needs_review` with a distinct, operator-actionable reason instead of a
  generic `no_usable_vectors`: `empty_document` when nothing could be extracted,
  and `unsupported_layout` when text was recovered but yielded no vectors. The
  FRA `fra_no_action_plan` review reason is retained and, like the other two,
  kept separate from a technical `failed`; all three are stable constants in
  `core/ingest_outcomes.py`. Terminal writes record the reason on the registry
  `error` field, in a per-reason `ThreadSafeStats` tally (counted once per
  file), and as low-cardinality `ingest_review_total{reason}` telemetry. The run
  report and CLI summary surface a `files_needs_review` count and a per-reason
  breakdown. Covered by terminal-state and CLI-summary tests for all three
  cases.
- **ROUTE-10:** Conversation-memory persistence failures now mark the turn
  `degraded/conversation_memory` with safe structured metadata and prevent it
  from being cached as healthy. A session-scoped marker prevents stale context
  from being inherited; standalone questions continue without a notice, while
  an explicit later follow-up receives concise guidance to restate the missing
  details. Memory reads and cached-result persistence use the same boundary.
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

Repository completion criteria are satisfied when the automated suite passes:

- Every P0/P1 item has behavioral outcome, retryability, telemetry, and user-copy
  coverage.
- Dependency failures are not translated into genuine empty results.
- Material fallbacks are typed on the affected outcome or, for ingestion
  integrity boundaries, on the terminal run/file state.
- The silent-failure baseline is empty and guarded against regression.
- Every named fault-injection seam has automated integration coverage.

Full backlog closure additionally requires a populated deployment evidence
manifest to pass:

`python tools/validate_rollout_evidence.py <rollout-evidence.json>`

That manifest proves monitoring connectivity, the live non-production fault
matrix, traffic-baseline comparison, operator approvals, and post-remediation
ACL conformance at or above `ACL_CONFORMANCE_THRESHOLD`. The example manifest
is deliberately incomplete and must never be used as rollout evidence.
