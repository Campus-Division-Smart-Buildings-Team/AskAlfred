# Dependency and retrieval-source classification

This is the Phase 0 classification artifact required by the failure and
degraded-states delivery plan. It records the current deployment contract; any
new retrieval source must be classified here before it is added to
`TARGET_INDEXES`.

## UI query dependencies

| Dependency | Classification | Impact when unavailable |
|---|---|---|
| Microsoft Entra ID | Mandatory in production | Authentication is unavailable; retrieval must not start. |
| OpenAI embeddings | Mandatory for semantic and vector-backed structured queries | The affected query is unavailable unless an independent source can answer it. |
| Pinecone | Mandatory for semantic and vector-backed structured queries | A failed required source produces `partial` or `unavailable`, never `empty`. |
| `testacl` Pinecone index | Required | This is the only configured entry in `TARGET_INDEXES`; its failure makes vector-backed retrieval unavailable. |
| Local intent classifier | Optional | Routing falls back to rules/patterns and is `degraded` when answer selection may be affected. |
| Building directory cache | Optional | Building-name recognition continues with reduced recall and is `degraded` for building-scoped queries. |
| Redis query rate limiter | Optional | Query limiting runs process-locally and emits a degraded-service event. |
| Third-party status pages | Optional | Status is reported as unchecked; request outcomes do not depend on it. |

## Ingestion dependencies

| Dependency | Classification | Impact when unavailable |
|---|---|---|
| Local source filesystem | Mandatory | The affected file/run is rejected or failed according to validation and retryability. |
| OpenAI embeddings | Mandatory | The affected item/run is unavailable or failed; partial embeddings must not be reported as success. |
| Pinecone target index | Mandatory | Vector writes cannot complete. |
| Ingest file/job registry | Mandatory | New writes pause or become explicit partial/reconciliation work; idempotency must not fail open. |
| Redis FRA supersession lease | Mandatory for FRA supersession | Supersession pauses; integrity-critical exclusivity must fail closed. |
| Durable FRA transaction journal | Mandatory for FRA supersession | No mutation may start without a durable journal record. |
| Metrics/event sink | Optional for data mutation | The operation may complete as degraded, with an operator alert for observability loss. |

## Production authentication decision

Anonymous access is a development-only posture. Production deployments require
authentication even if `REQUIRE_AUTH` is accidentally disabled. An absent
authenticated access context must therefore fail before retrieval in
production; an empty access filter must never become an unfiltered production
query.
