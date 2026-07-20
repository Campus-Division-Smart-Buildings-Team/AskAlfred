# AskAlfred failure and degraded states plan

## Purpose

This document is the failure-state register and remediation plan for the
AskAlfred Streamlit application and its ingestion pipeline.

It has four goals:

1. Identify every system-level failure, rejection, empty, partial, degraded,
   unavailable, and inconsistent-data state currently represented in the code.
2. Describe what the user or operator sees today.
3. Identify cases where developer-oriented or exception text reaches the UI.
4. Define an implementation and test plan that makes failures distinguishable,
   safe, observable, and recoverable.

This register covers `main.py`, `auth/`, `query_core/`, `query_handlers/`,
`search_core/`, `building/`, `security/`, `core/`, `interfaces/`, `ingest/`,
`fra/`, `ui/`, and the local ingestion CLI.

## Target failure taxonomy

All user-facing operations should end in exactly one of these states:

| State | Meaning | User treatment |
|---|---|---|
| `success` | The requested operation completed with a trustworthy result. | Show the result. |
| `empty` | The operation completed successfully and there is genuinely no matching authorised data. | Explain that no matching data was found and suggest a useful next action. |
| `low_confidence` | Results exist but do not meet the relevance threshold. | Show qualified results and explain uncertainty. |
| `rejected` | The request was intentionally refused because of validation, authentication, authorisation, or rate limiting. | Explain what the user can do next without exposing security rules. |
| `degraded` | The operation completed through a reduced-capability fallback. | Show the result and a concise capability warning when the degradation could materially affect it. |
| `partial` | Some independent sources/items succeeded and others failed. | Show verified results, name the unavailable scope in user terms, and offer retry. |
| `unavailable` | A required dependency or capability could not complete the operation. | State that the service is temporarily unavailable and offer retry. |
| `failed` | The operation failed for a non-transient or unknown reason. | Show a safe generic message and a support/correlation reference. |
| `critical_inconsistent` | A transactional operation and its rollback both failed, so stored state may be inconsistent. | Block further affected writes, alert operators, and require reconciliation. |

`empty` must never be used as a substitute for `unavailable`, `failed`,
`rejected`, or `partial`.

## Current state register

Priority meanings:

- **P0**: security, data-integrity, or materially misleading result.
- **P1**: major user journey is blocked or incorrectly represented.
- **P2**: degraded experience, weak recovery, or observability gap.
- **P3**: wording, consistency, or operator-experience improvement.

### 1. Startup, configuration, and shared services

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| START-01 | P1 | The local model ZIP is corrupt or contains an unsafe path. | `_safe_extract_zip` raises during module import, before `main()` can render controlled UI. Streamlit may show its framework exception page. | Catch archive validation/extraction at controlled startup, log details, and either enter pattern-only mode or show a stable `unavailable` page if the archive is mandatory. |
| START-02 | P2 | CT2 runtime is missing, the local intent model cannot load, the offline Hugging Face fallback cannot load, or intent embeddings cannot initialise. | The classifier logs the failure and runs in pattern-only mode. The user is not told that routing quality is degraded. | Record `intent_classifier=degraded`; warn only when the fallback can materially change the answer; expose the state to health telemetry. |
| START-03 | P3 | Intent embedding cache is corrupt, stale, unreadable, or cannot be saved. | Cache load/save failure is logged; embeddings are regenerated or remain uncached. | Keep this as transparent degradation, add a metric, and avoid a user warning unless startup becomes unavailable. |
| START-04 | P1 | Pinecone cannot populate the building cache or no configured index contains building data. | The app retries on future reruns and shows that building detection is limited to “pattern matching.” | Use user language such as “Building-name recognition is temporarily limited”; distinguish dependency outage from genuinely empty index data. |
| START-05 | P2 | Redis client or ping fails while rate limiting is initialised. | The app falls back to an in-memory limiter. Limits become process-local and reset on restart. | Mark rate limiting `degraded`, emit a metric/alert, and retain in-memory fallback. |
| START-06 | P0 | Redis fails during a distributed rate-limit check or lease operation. | Checks and lease operations fail open, allowing requests or concurrent work. | Choose policy by operation: query limiting may fail open with alerting; ingestion/FRA exclusivity must fail closed or pause because data integrity is at risk. |
| START-07 | P2 | Redis is not configured or unavailable to the optional service-status widget. | The widget reports `Not configured`, `No response`, or `Unavailable`. | Keep the state but translate it into impact, e.g. “Some safeguards are running in local mode.” |
| START-08 | P3 | OpenAI or Pinecone status-page fetch fails, times out, or returns invalid JSON. | The UI shows `Unknown: {exception}`. | Show “Status could not be checked” without exception text; log the cause and preserve the last known status with its timestamp. |
| START-09 | P1 | OpenAI or Pinecone credentials are missing or a client cannot initialise. | Client creation raises `ConfigError`; query paths may eventually return a handler apology or top-level search error. | Perform readiness checks, map each missing required dependency to `unavailable`, and show operators the detailed configuration cause only in logs/admin diagnostics. |
| START-10 | P2 | Redis host/port or timeout environment values are invalid. | Client creation raises detailed `ConfigError`; rate limiting may fall back while ingestion cannot start. | Validate configuration once at startup, publish component readiness, and separate optional UI dependencies from mandatory ingestion dependencies. |
| START-11 | P3 | Service health changes after a cached check. | Status data is cached for 60 seconds and can be stale. | Label status as last checked, preserve last known state, and never use the status page as proof that the current request succeeded or failed. |

### 2. Authentication and access control

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| AUTH-01 | Expected | Authentication is required and no session exists. | The app stops at the Microsoft sign-in gate. | Keep as `rejected/authentication_required`; preserve the intended destination and provide a clear sign-in action. |
| AUTH-02 | P1 | Required Azure environment variables are absent. | The UI displays the `ConfigError`, including environment-variable names, and stops. | Show users “Sign-in is temporarily unavailable”; log missing variable names and expose them only in operator diagnostics. |
| AUTH-03 | P2 | Microsoft returns an OAuth error or the user cancels sign-in. | A generic “Microsoft sign-in was not completed” message is shown. | Keep the safe message; optionally distinguish cancellation from service failure when this can be done without exposing provider detail. |
| AUTH-04 | P2 | The cached OAuth flow expires or the Streamlit session is lost during redirect. | The user sees that the authentication session expired and is asked to retry. | Keep; recreate a clean flow and provide a single retry button. |
| AUTH-05 | P1 | Token acquisition raises due to network, provider, or callback validation failure. | A generic retry message is stored and displayed. | Keep safe handling; add stable error code, correlation ID, and retry telemetry. |
| AUTH-06 | P1 | MSAL returns a non-dictionary or an error result. | Invalid response or generic provider failure is shown. | Map to `failed/auth_provider_response`; retain provider details only in sanitised logs. |
| AUTH-07 | P0 | The returned ID token lacks a stable user identifier. | `_build_auth_context_from_claims` raises `ConfigError` outside the token-acquisition `try`, potentially producing an unhandled Streamlit exception. | Catch claim validation, clear the unusable session, show a safe sign-in failure, and alert on the identity-provider contract violation. |
| AUTH-08 | P0 | An authenticated token has no tenant ID. | Access filtering uses a synthetic deny-all tenant. Searches generally look like genuine no-result states. | Return `rejected/access_context_invalid` before retrieval and show “Your account could not be assigned data access”; never represent it as empty data. |
| AUTH-09 | P0 | An authenticated user has no roles. | The transitional policy applies only tenant filtering, which can expose role-restricted documents that would be hidden from a user with non-matching roles. | Complete the role rollout and fail closed for role-restricted content; document any intentionally tenant-wide content class. |
| AUTH-10 | P0 | A vector lacks required ACL metadata while an access filter is active. | The match is silently removed. If all matches are removed, the result appears empty. | Track `filtered_missing_acl`, quarantine/re-ingest noncompliant vectors, and distinguish “no authorised results” from service failure without revealing hidden document existence. |
| AUTH-11 | P1 | The user's tenant or roles do not match any result. | Matches are filtered out and the user sees no results. | Use a privacy-preserving `empty_authorised_scope` message; do not disclose that inaccessible documents exist. |
| AUTH-12 | P2 | A signed-out or expired session attempts to submit a query. | A defence-in-depth warning asks the user to sign in. | Keep, clear stale session state, and route back to the sign-in action. |
| AUTH-13 | P0 | Authentication is disabled or the session is anonymous, so no access filter is built. | `build_access_filter` returns `{}` for unauthenticated sessions, and `filter_authorized_matches` treats an empty filter as "no enforcement": every ACL-tagged document, including tenant- and role-restricted vectors, is returned. This is the current behaviour for any anonymous/dev session and is distinct from AUTH-08/AUTH-09 (which concern authenticated sessions). | Decide the anonymous-access posture per deployment; where authentication is mandatory, fail closed when no access context exists. Never let an empty/absent filter mean "return everything" in a production tenant; distinguish "authentication disabled by config" from "authenticated session with missing tenant." |

### 3. Input validation, request rejection, and throttling

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| INPUT-01 | Expected | Input is empty or whitespace. | “Please enter a question.” | Keep as `rejected/empty_query`. |
| INPUT-02 | Expected | Input is shorter than the configured minimum. | The exact minimum character count is shown. | Use a simple prompt for more detail; keep exact limits in accessibility/help text if useful. |
| INPUT-03 | Expected | Input exceeds the configured maximum. | The exact maximum is shown. | Keep as actionable validation, optionally include current length. |
| INPUT-04 | P2 | A prompt-injection pattern is detected. | The UI says the query contains “invalid patterns.” | Use neutral wording (“I can’t process that wording; please ask only about…”) and do not reveal detection rules. |
| INPUT-05 | P2 | The special-character ratio exceeds the threshold. | The UI exposes counts, percentages, and security-oriented terminology. | Replace with user-friendly wording and log technical ratios separately. |
| INPUT-06 | P2 | The suspicious-character ratio exceeds the threshold. | The UI says “dangerous chars” and exposes the count. | Replace with neutral wording; never label user text “dangerous” in the UI. |
| INPUT-07 | P2 | Too many characters repeat in sequence. | The UI exposes the measured maximum. | Say the question could not be read and ask the user to remove repeated characters. |
| INPUT-08 | Expected | Per-user query limit is exceeded. | The UI shows a retry countdown. | Keep; add `Retry-After` semantics to any future API and ensure reset calculation is stable. |
| INPUT-09 | P2 | Redis fails during a query rate-limit check. | The operation is allowed with only a server log. | Keep fail-open only if explicitly accepted; emit `rate_limit_backend_degraded` and surface it to operators. |
| INPUT-10 | P2 | A semantically searchable query passes security minimums but is too short for semantic search. | The semantic handler asks for more detail and returns no results with nominal success. | Represent this consistently as `rejected/insufficient_detail`, not `success`. |

### 4. Preprocessing, intent routing, and conversation state

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| ROUTE-01 | P2 | Spell check, building extraction, business-term extraction, or complexity analysis raises. | The error is logged and routing continues with reduced context. | Record which preprocessor degraded; warn only if the missing context materially narrows or changes the answer. |
| ROUTE-02 | P3 | Extracted building text is invalid or maintenance-like. | The building is silently cleared and the query proceeds without that filter. | Record `building_scope_discarded`; ask for clarification when the query clearly depends on a building. |
| ROUTE-03 | P2 | Building cache is unavailable. | Pattern/n-gram extraction continues with reduced recall. | Attach `degraded_components=["building_directory"]` to the result and provide a concise warning for building-scoped queries. |
| ROUTE-04 | P2 | A handler's `can_handle()` raises. | The candidate is skipped and routing continues. | Record partial routing failure; do not treat the ultimate answer as fully healthy if the failed handler could have been authoritative. |
| ROUTE-05 | P2 | The ML intent classifier raises. | Routing falls back to rule-based or semantic handling. | Return `degraded` with classifier failure metadata that is safe for telemetry, not UI exception text. |
| ROUTE-06 | Expected | ML confidence is below the routing threshold. | The query routes to semantic search. | Keep as normal routing, not a failure, but retain confidence for diagnostics. |
| ROUTE-07 | Expected | The ML-selected handler declines the query or has no dedicated handler. | Semantic search is used. | Keep as normal fallback and distinguish it from a handler exception. |
| ROUTE-08 | P1 | Custom handler configuration omits/disables the semantic fallback or produces no route handler. | A later `None.handle`/attribute failure can escape to top-level error handling. | Validate the handler graph at startup and require exactly one terminal fallback. |
| ROUTE-09 | P2 | A handler raises during execution. | Most handlers catch it and return a generic answer with `success=False`; the UI renders this like a normal assistant message. | Make the UI branch on structured status, render consistent error treatment, and provide retry. |
| ROUTE-10 | P2 | Session memory persistence fails. | The current answer succeeds; follow-up context may be lost with no user indication. | Mark the turn `degraded/conversation_memory`; notify only on a follow-up where context loss matters. |
| ROUTE-11 | P3 | Conversation summarisation fails. | The failure is logged and chat continues using the older summary. | Keep graceful degradation; metric and bounded retry are sufficient. |
| ROUTE-12 | P2 | A failed `QueryResult` is cached. | If caching is enabled, transient failures can be replayed until TTL expiry because caching does not require `success=True`. | Cache only `success`, genuine `empty`, and intentionally cacheable rejections; never cache transient `failed` or `unavailable` results. |

### 5. Retrieval, structured query, and answer generation

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| SEARCH-01 | P0 | A Pinecone index cannot be opened. | `search_one_index` catches the exception and returns `[]`. | Return a typed source failure; aggregate it into `partial` or `unavailable`, not `empty`. |
| SEARCH-02 | P0 | Query embedding fails because of OpenAI authentication, permission, model, rate limit, timeout, connection, bad request, or unknown error. | Query search commonly returns `[]`; the final response can say that no documents matched. | Preserve typed embedding failure and retryability; map transient failures to `unavailable`, fatal configuration to `failed`, and mixed-model outcomes to `partial`. |
| SEARCH-03 | P0 | A Pinecone namespace query fails. | That namespace returns `[]`; other namespaces continue. | Return per-source status and show a partial-results warning when any failed source could affect completeness. |
| SEARCH-04 | P0 | Every configured index/namespace fails. | The aggregate is empty and is presented as no matching documents. | Return `unavailable/search_backend`, never `empty`. |
| SEARCH-05 | P1 | Some indexes fail and others return results. | Successful results are shown without telling the user that coverage is incomplete. | Return `partial`, list affected source categories in user terms, and retain successful results. |
| SEARCH-06 | P1 | Structured-query index calls fail. | Several structured helpers catch exceptions and return empty lists, counts, or fallback answers. | Apply the same typed source-outcome contract to structured and semantic retrieval. |
| SEARCH-07 | Expected | The primary namespace has no matches but a fallback namespace succeeds. | Results are returned from the fallback. | Treat as success; record fallback source for telemetry. |
| SEARCH-08 | Expected | Retrieval completes and genuinely has no matching authorised documents. | The assistant displays one of several “couldn’t find” or “No … found” messages. Note: `semantic_search` returns `score_too_low=True` on the empty branch, so the retrieval contract currently emits `empty` through the *same* flag as `low_confidence` (SEARCH-09); the legacy UI masks this only because it checks `if not results` first. | Standardise as `empty`, preserve domain-specific guidance, keep it distinct from access or dependency failure, and separate the empty signal from `score_too_low` in the new contract. |
| SEARCH-09 | Expected | Retrieved results are below the relevance threshold. | Results/answer may be shown with a low-score warning. | Standardise as `low_confidence`; explain how to improve the query and never imply factual confidence. |
| SEARCH-10 | Expected | A requested maintenance building/category/status/priority does not match data. | Domain-specific no-data messages and suggestions are shown. | Keep as `empty` with suggestions, after confirming the underlying sources were healthy. |
| SEARCH-11 | Expected | Property condition, ranking area data, or count target has no matching records. | Domain-specific empty messages are shown. | Keep as `empty`, conditional on healthy sources. |
| SEARCH-12 | P1 | `search_core.execute()` returns an unexpected tuple length. | `safe_execute` silently returns empty results and no answer. | Validate the return contract and raise a typed internal contract failure. |
| SEARCH-13 | P1 | Answer-generation API call fails. | Semantic answer helpers return an error sentence as an answer; the enclosing handler can still leave `success=True`. | Return a structured `partial`: retrieval succeeded but answer generation is unavailable; show direct results and offer “Retry summary.” |
| SEARCH-14 | P2 | OpenAI returns no answer content. | The UI may show “No answer generated” or a generic result count. | Treat as `partial/empty_model_response`, display direct results, and log response metadata safely. |
| SEARCH-15 | P3 | Citation-enforcement retry fails after a usable first answer. | The first answer is retained. | Keep as degraded success; flag citation quality and avoid claiming fully verified citation coverage. |
| SEARCH-16 | P2 | Building-focused answer generation fails. | The code falls back to a standard answer-generation path. | Record degraded mode; if the fallback also fails, display direct verified results. |
| SEARCH-17 | P2 | Comparison answer generation fails. | A generic comparison-error answer is returned. | Return retrieved comparison rows directly and mark answer generation partial. |
| SEARCH-18 | P1 | A handler catches an exception and stores `str(e)` in `QueryResult.metadata`. | It is not rendered by the current normal UI, but it is persisted in session history/cache and could be exposed by future debugging/export UI. | Store stable error codes and sanitised operator details separately; do not put raw exception strings in transport/UI result objects. |
| SEARCH-19 | P2 | An unexpected exception escapes the manager. | Production shows a generic “search service” error; development shows sanitised exception text. | Use a stable incident reference, retry action, typed log event, and a single central presenter. |
| SEARCH-20 | P2 | Search returns results but none can be cited reliably. | The answer includes a developer-like statement about the “current source pack.” | Replace with user language such as “I could not verify citations for this summary; please review the source results below.” |

### 6. UI rendering and observability

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| UI-01 | P1 | Any handler returns `success=False`. | The answer is rendered as ordinary assistant markdown; failure status is visible only in development debug data. | Render a consistent error component with retry/help actions and do not present it as a normal answer. |
| UI-02 | P2 | A result has no answer. | Chat history stores “No answer provided,” which reads like an implementation placeholder. | Use direct results, a user-friendly partial-state message, or a typed contract failure. |
| UI-03 | P3 | Legacy search finds no results. | The message contains “Regan has told me to say I don’t know.” | Replace with professional, stable product copy. |
| UI-04 | P2 | Building-cache status lookup raises. | The raw exception is interpolated into an `st.warning`. | Remove exception text; log it with a correlation reference. |
| UI-05 | P2 | An external status-page check raises. | `Unknown: {exception}` is shown and saved in status history. | Store/render a safe status label; keep exception details only in sanitised logs. |
| UI-06 | P2 | Authentication setup raises `ConfigError`. | Missing environment-variable names or other configuration detail is rendered in the main UI/sidebar. | Replace with user-safe auth-unavailable copy and an operator error code. |
| UI-07 | P3 | The app runs in development mode and search raises. | A sanitised exception is intentionally shown. | Keep only behind an explicit local/admin diagnostic capability, not a broadly configurable production flag. |
| UI-08 | P3 | Building cache is unavailable. | The normal user sees implementation wording such as “cache” and “pattern matching.” | Explain capability impact rather than implementation. |
| UI-09 | P3 | Normal sidebar is rendered. | It exposes “Minimum Score Threshold,” index names, namespace names, and two-stage search architecture. These are developer diagnostics rather than user error handling. | Move to an authenticated operator/debug panel; give users task-oriented help instead. |
| UI-10 | P3 | Service status is enabled. | It exposes vendor component names but not what an outage means for the current user operation. | Add impact labels such as “Answer summaries” and “Document search,” while retaining vendor details for operators. |
| UI-11 | P2 | Publication info, snippets, third-party status descriptions, or generated answers contain unexpected content. | Most content is sanitised, but all new failure components must maintain the same boundary. | Route all external/user-derived content through safe rendering and test XSS/Markdown payloads. |
| UI-12 | P2 | A service fails after the status widget reported healthy. | The request-level result and sidebar status can disagree. | Request outcome is authoritative; label the widget as informational and last-checked. |

### 7. Ingestion configuration and file handling

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| INGEST-01 | P1 | Required Pinecone/OpenAI/Redis configuration is absent or invalid. | CLI configuration fails and exits non-zero. | Keep fail-fast; emit structured config error code, safe CLI summary, and operator-only detail. |
| INGEST-02 | P1 | Embedding dimension, chunk sizes, worker counts, batch sizes, timeouts, metadata limits, or upsert strategy are invalid. | Validation raises `ConfigError`. Some overly large worker values are clamped with warnings. | Keep deterministic validation; print all validation issues in one pass where practical. |
| INGEST-03 | P1 | Local path is missing, not a directory, inaccessible, or escapes the approved root. | Ingestion aborts with configuration/security error. | Keep fail-closed and distinguish user path mistakes from security rejection in operator output. |
| INGEST-04 | P0 | A path traverses outside the root, is a symlink, changes during open, or resolves unexpectedly. | File validation rejects it. | Keep fail-closed; preserve security telemetry without printing sensitive absolute paths unnecessarily. |
| INGEST-05 | P1 | File type is unsupported, file is too large, path is not a regular file, or operation rate limit is exceeded. | The file is rejected and can count as failed. | Record a stable per-file reason and continue only where batch policy allows. |
| INGEST-06 | P2 | Text decoding as UTF-8 fails. | Some paths use Latin-1 with ignored decoding errors, potentially losing characters. | Mark extraction degraded and record the encoding fallback; do not silently claim full-fidelity ingestion. |
| INGEST-07 | P1 | File read, PDF/Word extraction, parsing, chunking, or metadata generation fails. | The individual file is marked failed while the batch continues. | Keep isolation; ensure terminal file state, reason code, retryability, and summary are consistent. |
| INGEST-08 | P2 | No usable text or no FRA action-plan section is found. | The file may produce no vectors or parser warnings rather than a clear terminal reason. | Define `empty_document`, `unsupported_layout`, and `fra_no_action_plan` outcomes separately from technical failure. |
| INGEST-09 | P1 | Per-file elapsed time exceeds `max_file_seconds` before or after embedding. | Registry is marked `failed/file_timeout` and an `IngestError` is raised for that file. | Keep; make timeout retryability explicit and ensure partial vectors cannot later overwrite failure with success. |
| INGEST-10 | P1 | Process memory/buffer pressure exceeds configured protection. | Buffer/resource errors can fail the file or batch. | Emit a resource-exhaustion code, backpressure metrics, and safe tuning guidance. |
| INGEST-11 | P2 | Dry-run mode is active. | External writes and skip-existing behaviour are intentionally changed. | Report as `dry_run`, never as ingestion success, and clearly state that no corpus data changed. |

### 8. Embedding, vector upsert, verification, and registries

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| VECTOR-01 | P1 | OpenAI embedding is rate-limited or has transient connection/API timeout errors. | Batches retry with backoff and adaptively shrink. Items can fail after retries. | Keep retry/backoff with jitter; expose retry counts and terminal retryability. |
| VECTOR-02 | P1 | Embedding authentication/permission fails or the model is not found. | The error is fatal for remaining items in that embedding call. | Abort affected ingestion scope promptly, mark configuration failure, and avoid retry storms. |
| VECTOR-03 | P1 | Embedding request is invalid/conflicting/unprocessable. | Batch reduction may isolate items; failed items are skipped. | Record per-item reason; mark the source file `partial` if other vectors succeed. |
| VECTOR-04 | P1 | Embedding response count differs from input count. | All affected indexes receive `response_size_mismatch`. | Treat as provider contract failure; retry once safely, then mark partial/failed with alerting. |
| VECTOR-05 | P1 | Only some chunks/risk items embed successfully. | Failed items are skipped and successful vectors proceed, risking an apparently successful but incomplete file. | Make `partial` a first-class terminal state with failed-item counts and never promote it to `success`. |
| VECTOR-06 | P1 | Pinecone upsert encounters a retryable error. | The batch retries; after retries it may split to isolate bad vectors. | Keep; add idempotency/retry metrics and a maximum total retry budget. |
| VECTOR-07 | P1 | Upsert encounters a permanent/non-retryable error or a singleton still fails after split. | Batch/file state is marked failed. | Keep; ensure all affected files are terminally failed and are eligible for targeted retry. |
| VECTOR-08 | P1 | Upsert worker stop event is set while batches are buffered/queued. | Pending batches are drained and marked failed with worker/shutdown reason. | Keep; produce a complete shutdown report and make rerun safe. |
| VECTOR-09 | P1 | Upsert queue does not drain before the join timeout. | Remaining batches are failed as `queue_drain_timeout`; worker threads may remain alive. | Treat as run-level `partial/failed`, alert if threads survive, and prevent a nominal-success exit. |
| VECTOR-10 | P1 | Keyboard interrupt occurs while draining. | Stop is set, queued work is failed, and workers are joined. | Keep graceful cancellation; use a distinct `cancelled` operator outcome while files remain failed/retryable. |
| VECTOR-11 | P0 | Upsert reports success but FRA verification cannot fetch all expected IDs after retries. | Missing IDs are returned to transactional handling; affected state may be failed/rolled back. | Preserve `verification_failed`, never mark the file successful, and distinguish read-after-write lag from persistent absence. |
| VECTOR-12 | P1 | File-registry write or token-guarded transition fails. | Failure is logged; the vector operation may have succeeded while registry state is stale. | Mark the run `partial/registry_unavailable`, reconcile from vector IDs/events, and alert. |
| VECTOR-13 | P0 | A stale worker tries to overwrite a newer file state. | Processing tokens are intended to reject stale transitions. | Keep token enforcement and add explicit stale-writer metrics/tests for every terminal transition. |
| VECTOR-14 | P2 | Job-registry read/write fails. | Work may proceed without complete deduplication/status records; failures are logged. | Decide per job type whether to fail closed; FRA supersession must not proceed without safe exclusivity. |
| VECTOR-15 | P2 | Metrics export or ingestion event emission fails. | Ingestion continues and only logs a warning. | Keep data path independent, but mark observability degraded and spool/retry events locally if required. |
| VECTOR-16 | P0 | FRA verification fetch fails (Pinecone read outage/timeout) rather than the vectors being absent. | `_verify_fra_vectors_present` catches the fetch error and folds the batch into "still missing", so a read-side outage is indistinguishable from a failed upsert and drives `_handle_verification_failure` — rolling back a correct write. This extends VECTOR-11 beyond read-after-write lag to a dependency outage during verification. | Distinguish verification-read failure from vector absence: on fetch failure treat the check as `unavailable`/retryable and never roll back a successful upsert; only persistent absence after healthy reads is `verification_failed`. |

### 9. FRA parsing and supersession transaction

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| FRA-01 | P2 | FRA parser cannot locate an action-plan section or row boundaries. | It returns no items with low confidence/warnings. | Record a non-technical review outcome (`needs_review`) rather than silently treating the document as having no risks. |
| FRA-02 | P2 | Some FRA fields cannot be parsed or enriched. | Defaults such as `Unknown` and confidence warnings are used. | Persist field-level provenance/confidence and expose to operator review, not end-user error UI. |
| FRA-03 | P1 | Supersession job already succeeded or is currently processing. | Duplicate work is skipped. | Keep idempotent success/active states; distinguish active lease from abandoned work. |
| FRA-04 | P0 | Redis supersession lock cannot be acquired, times out, or is lost. | `DeadlockError`/external-service failure can abort the transaction. | Fail closed, do not mutate supersession state without ownership, and alert on lost locks. |
| FRA-05 | P1 | Supersession query/embedding fails. | Some code falls back to a zero vector; registry may record failure. | Avoid approximate zero-vector selection for integrity-critical mutation; use deterministic metadata queries or abort safely. |
| FRA-06 | P0 | Only some prior FRA risk items are marked superseded. | Job registry may be marked `partial`. | Stop subsequent commit or record an explicit compensating transaction; require reconciliation before declaring new FRA current. |
| FRA-07 | P0 | New vectors fail after older items were marked superseded. | The transaction attempts to restore previous items. | Keep compensating rollback and verify every restored ID. |
| FRA-08 | P0 | Rollback restores zero or only some superseded items. | A `RollbackError`, critical log/event, or partial restoration can occur; data may be inconsistent. | Enter `critical_inconsistent`, block further supersession for the building, page operators, and run deterministic reconciliation. |
| FRA-09 | P1 | Supersession registry/alert update fails after mutation. | Data may be correct while job status/alerting is stale. | Write an immutable transaction journal before mutation and reconcile registry status from it. |
| FRA-10 | P1 | Read-after-write verification lags. | Verification retries with backoff before declaring IDs missing. | Keep bounded retries; record latency distribution and do not re-upsert blindly without idempotency. |
| FRA-11 | P0 | An exception outside the caught tuple (bare `Exception`, `KeyError`, unwrapped Redis timeout, `MemoryError`) is raised inside `FraTransaction.execute()`/`verify()`. | `upsert_vectors_atomic` only catches `ExternalServiceError, IngestError, ValidationError, RoutingError, ParseError, ModelNotInitialisedError`; any other exception skips the compensating rollback entirely. Superseded items stay superseded, new vectors may be partly upserted, the lock releases cleanly, and the `critical_inconsistent` path is never reached. | Roll back on *any* exception (catch broadly, restore, then re-raise), and route an incomplete or skipped rollback into `critical_inconsistent` regardless of exception type. |
| FRA-12 | P0 | The process crashes, is OOM-killed, or is terminated between `mark_superseded_risk_items` and `rollback`. | `FraSupersessionTxnLog` holds superseded IDs in an in-process dict only; a crash loses all restoration data, leaving orphaned superseded items with no recovery artifact. This is distinct from FRA-08 (rollback runs but restores only some items). | Write a durable transaction journal (superseded IDs plus tx state) before mutation; on restart, detect open transactions and run deterministic reconciliation from the journal. |
| FRA-13 | P1 | The job registry is unavailable when checking whether a supersession already succeeded. | `_filter_supersede_requests_with_registry` sets `existing = None` on lookup error and proceeds, so a registry-read outage fails open and a supersession that already completed can be re-run. | Fail closed for integrity-critical supersession: if idempotency/exclusivity cannot be confirmed, pause or defer the mutation rather than re-running it. |

### 10. Batch/run-level terminal states

| ID | Priority | Trigger | Current state and behaviour | Required target |
|---|---:|---|---|---|
| RUN-01 | Expected | All eligible files and vectors complete and verify. | Report shows files/vectors and normal completion. | Terminal `success`. |
| RUN-02 | Expected | Files were already successfully ingested or excluded by policy. | Files count as skipped. | Terminal `success_with_skips`, with explicit skip reasons. |
| RUN-03 | P1 | Some files fail while others succeed. | Report includes `files_failed` and a failed-file list, but CLI may still complete the orchestration path. | Terminal run status `partial` and non-zero/defined exit policy suitable for automation. |
| RUN-04 | P1 | Worker-level errors are collected. | Errors are logged; a report is still returned. | Run must be `failed` or `partial`; never report plain success. |
| RUN-05 | P1 | All files fail or no vectors are committed because dependencies are unavailable. | Summary can still say “Ingestion complete.” | Use `failed`/`unavailable`; reserve “complete” for terminal processing, not success wording. |
| RUN-06 | P2 | No files are found. | Run returns zero counts. | Use `empty_input` and decide whether automation should treat it as success or configuration error. |
| RUN-07 | P2 | Run is cancelled by operator. | Pending work is failed/drained. | Use run status `cancelled`; retain per-file retryable failure/cancel status. |
| RUN-08 | P0 | Rollback failure or unresolved registry/vector divergence occurs. | Critical logs/events exist, but there is no unified run state. | Run status `critical_inconsistent`; block normal success exit and create a reconciliation artifact. |

## Developer-oriented text currently reaching the UI

The following are direct UI exposures that should be fixed.

| Exposure | Current location | Why it is unsuitable | Replacement |
|---|---|---|---|
| Raw authentication `ConfigError` text, including missing Azure environment-variable names | `auth/auth_manager.py` in `render_auth_sidebar()` and `ensure_authentication()` | Reveals deployment configuration and gives end users instructions they cannot act on. | “Microsoft sign-in is temporarily unavailable. Please try again later or contact support. Reference: AUTH-SETUP.” |
| Raw building-cache exception via `f"... {e}"` | `ui/ui_components.py` sidebar cache status | Can disclose client, endpoint, index, or implementation details. | “Building-name recognition status could not be checked.” |
| Raw status-page exception via `f"Unknown: {exc}"` | `ui/ui_components.py` service status | Can disclose network/URL/parser details and is stored in status history. | “Status could not be checked”; log the exception separately. |
| Sanitised exception text in development search errors | `main.py::handle_search_error()` | Appropriate only for a tightly controlled local/admin environment; risky if a deployment flag is wrong. | Stable user message plus correlation ID; details in an authenticated diagnostics panel. |
| Special-character percentages/counts and “dangerous chars” | `security/input_validator.py`, displayed by `main.py` | Security implementation detail and accusatory wording. | “I couldn’t process those characters. Please rephrase using ordinary words and punctuation.” |
| “Cache not initialised” and “limited to pattern matching” | `ui/ui_components.py` and `main.py` | Developer terminology rather than capability impact. | “Building-name recognition is temporarily limited; include the full building name in your question.” |
| “Minimum Score Threshold,” index names, namespace names, and two-stage search description | `ui/ui_components.py` normal sidebar | Operational diagnostics are mixed into the user experience. | Move to an operator-only diagnostics panel. |
| “No answer provided” | `main.py` chat-history fallback | Implementation placeholder rather than a handled state. | Direct results or a typed partial/failure message. |
| “Regan has told me to say I don’t know” | `main.py::NO_RESULTS_MESSAGE` | Informal internal wording and inconsistent product voice. | Standard no-results copy with a rephrasing suggestion. |
| “Current source pack” in citation failure copy | `search_core/generate_semantic_answer.py` | Retrieval implementation terminology. | Explain that citations could not be verified and show the available sources. |

The following are not rendered in the normal UI today, but are exposure risks:

- `CountingHandler`, `PropertyHandler`, `RankingHandler`, and
  `SemanticSearchHandler` place raw `str(e)` values into `QueryResult.metadata`.
- Query results and metadata are stored in session history and may be cached.
- A future export, debug expander, analytics event, or API serializer could expose
  those raw values without passing through `sanitise_error`.
- Third-party status descriptions and generated answer strings are displayed and
  must continue to pass through safe rendering.

## Remediation architecture

### A. Introduce structured operation outcomes

Create a shared outcome model, for example:

```python
class OutcomeStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    LOW_CONFIDENCE = "low_confidence"
    REJECTED = "rejected"
    DEGRADED = "degraded"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CRITICAL_INCONSISTENT = "critical_inconsistent"


@dataclass
class FailureInfo:
    code: str
    component: str
    retryable: bool
    correlation_id: str
    safe_context: dict[str, object] = field(default_factory=dict)


@dataclass
class SourceOutcome:
    source: str
    status: OutcomeStatus
    result_count: int = 0
    failure: FailureInfo | None = None
```

Extend or replace `QueryResult.success` with `status`, `failure`,
`degraded_components`, and `source_outcomes`. Retain a compatibility `success`
property temporarily so existing handlers and tests can migrate incrementally.

Do not put raw exception strings in `QueryResult`, session state, cache entries,
metrics labels, or UI-facing event payloads.

### B. Preserve dependency failures through retrieval

Replace `return []` exception paths in semantic and structured search with typed
source outcomes.

Aggregation rules:

1. Healthy sources with zero matches and no failures -> `empty`.
2. Some healthy sources return results and any source fails -> `partial`.
3. Some sources are healthy but empty and others fail -> `partial`, because
   completeness is unknown.
4. Every required source fails -> `unavailable` or `failed`, based on
   retryability.
5. ACL filtering to zero results -> `empty_authorised_scope`, never dependency
   failure and never disclosure of inaccessible document existence.
6. Low score -> `low_confidence`, only after source health is known.

Apply the same contract to Pinecone index open, namespace query, embedding,
structured query, answer generation, and citation generation.

Note: these rules require each retrieval source to be classified as *required*
or *optional*. Today every entry in `TARGET_INDEXES` is treated identically, so
"every required source fails -> unavailable" cannot be evaluated. Producing that
per-index required/optional classification is a prerequisite deliverable (see
Phase 0), not a runtime detail.

### C. Centralise exception classification and user presentation

Create:

- `core/failure_codes.py` for stable codes and retryability.
- `core/error_classifier.py` to map OpenAI, Pinecone, Redis, auth, validation,
  timeout, configuration, and unknown exceptions.
- `ui/error_presenter.py` to map structured failures to user-safe copy and
  actions.

Each displayed failure should contain:

- a plain-language impact statement;
- whether retry is useful;
- one primary next action;
- a short correlation reference for support;
- no exception, credential, endpoint, index, namespace, environment-variable,
  stack, provider payload, or security-rule detail.

Detailed sanitised diagnostics belong in structured logs and an authenticated
operator view.

### D. Separate retrieval from answer generation

Treat retrieval and summarisation as independent stages:

- If retrieval succeeds and answer generation fails, return `partial`.
- Display direct search results with a “Summary temporarily unavailable” notice.
- Provide a summary-only retry that does not repeat retrieval unnecessarily.
- Never encode an error sentence as a nominally successful model answer.

### E. Make access-context failures explicit and private

- Validate tenant and role claims immediately after authentication.
- Fail before retrieval if required access context is missing.
- Complete the role-based access-control rollout and remove the transitional
  roleless broadening.
- Count/quarantine vectors missing the ACL envelope during ingestion.
- Use privacy-preserving empty wording; do not reveal whether inaccessible
  documents exist.
- Treat an empty/absent access filter as "no enforcement" only where anonymous
  access is an accepted deployment posture; where authentication is mandatory,
  an absent access context must fail closed rather than return all ACL-tagged
  documents (AUTH-13).

### F. Strengthen ingestion terminal states

- Use one state machine for file and run outcomes.
- Add explicit `partial`, `cancelled`, `needs_review`, and
  `critical_inconsistent` states where required.
- Ensure a failed chunk/batch cannot later be overwritten by file success.
- Make registry transitions token-guarded and idempotent.
- Treat vector success plus registry failure as partial and reconcile it.
- Make CLI exit codes stable:
  - `0`: success/success-with-skips;
  - `2`: empty input or validation-only outcome, if accepted by automation;
  - `3`: partial;
  - `4`: unavailable/retryable;
  - `5`: failed/non-retryable;
  - `10`: critical inconsistent state.

### G. Protect FRA transactional integrity

- Do not perform integrity-critical supersession using a zero-vector fallback.
- Require lock ownership and a durable transaction journal before mutations.
- Verify both supersession and rollback per vector ID.
- On incomplete rollback, block that building's supersession key, emit a
  critical alert, and generate a reconciliation report containing affected IDs.
- Provide an idempotent reconciliation command and test crash recovery at every
  transaction boundary.
- Roll back on *any* exception raised during execute/verify, then re-raise;
  never let an unclassified exception skip the compensating rollback (FRA-11).
- Persist the supersession journal durably before any mutation so a crash
  mid-transaction is recoverable; the current in-memory log is lost on crash
  (FRA-12).
- Distinguish a verification-read outage from vector absence; do not roll back a
  successful upsert because a fetch failed (VECTOR-16).
- Fail closed when the job registry cannot confirm supersession idempotency; a
  registry-read outage must not silently re-run a completed supersession
  (FRA-13).

### H. Improve degraded-mode observability

Add low-cardinality counters/gauges for:

- request outcomes by status and failure code;
- source outcomes by component;
- fallback activations;
- partial-result queries;
- no-result queries with all sources healthy;
- rate-limit backend degradation/fail-open events;
- ACL-metadata drops;
- file/run terminal states;
- registry divergence;
- rollback and reconciliation state.

Never use exception text, user IDs, queries, document names, or file paths as
metric labels.

## Phase 0 update (completed 2026-07-20)

The baseline and contract milestone is complete.

Implemented:

- Froze this register as the narrative acceptance inventory and added
  `core/failure_acceptance.py` as its executable counterpart. Every one of the
  61 P0/P1 rows has a stable `OutcomeStatus`, `FailureCode`, component owner,
  priority, and deterministic pytest node.
- Added `tests/test_failure_acceptance_inventory.py`. It parses this register
  and requires exact agreement with the executable inventory, so adding,
  removing, or reprioritising a P0/P1 row without updating its outcome contract
  fails CI. Each row is a separately identified parameterised test.
- Added a source-level baseline guard for broad exception handlers that return
  `[]`, `None`, empty tuples/dicts, or nominal success. Existing debt is frozen
  by function fingerprint; any new path fails CI unless it returns an explicit
  outcome. Later phases remove the frozen entries as callers migrate.
- Added the complete shared taxonomy, stable low-cardinality failure-code
  registry, retryability registry, opaque correlation references, per-source
  outcome model, and the temporary `QueryResult.success` compatibility view.
- Added `plan/dependency_and_source_classification.md` and the executable
  `config/source_classification.py` check. All UI-query and ingestion
  dependencies are classified, and every `TARGET_INDEXES` entry must have an
  explicit required/optional classification.
- Resolved AUTH-13: anonymous access is development-only.
  `authentication_required()` always returns true in production regardless of
  an accidentally permissive `REQUIRE_AUTH` setting.

## UI copy remediation update (completed 2026-07-20)

The user-facing copy remediation requested from this register is complete. This
update covers the normal AskAlfred Streamlit application; command-line output,
server logs, and the developer-only profiling tool remain technical by design.

Implemented:

- Replaced raw authentication/configuration errors with stable sign-in copy and
  kept configuration details in sanitised logs (`AUTH-02`, `UI-06`).
- Caught invalid identity claims, cleared the unusable authentication context,
  and returned a controlled sign-in message (`AUTH-07`).
- Replaced security-rule and measurement wording for short, long, injection,
  special-character, suspicious-character, and repeated-character rejections
  (`INPUT-02` through `INPUT-07`).
- Replaced cache, pattern-matching, threshold, index, namespace, score, boost,
  result-ID, debug-handler, and architecture wording on normal UI surfaces.
  Internal search details are no longer rendered in the sidebar or result cards
  (`START-04`, `UI-04`, `UI-08`, `UI-09`).
- Made the optional service-status widget impact-focused. Provider exception
  text and third-party status descriptions are not rendered or retained in UI
  history; safeguards running without the shared service are described as
  local mode (`START-07`, `START-08`, `UI-05`, `UI-10`).
- Replaced the legacy no-results phrase, numeric low-score explanation,
  developer spinners, and implementation placeholders with stable recovery
  guidance (`SEARCH-08`, `SEARCH-09`, `UI-02`, `UI-03`, `UI-07`).
- Render search sources using a document title or filename only. Storage paths,
  indexes, namespaces, scores, boost reasons, and vector IDs are not shown.
  Publication information applies the same path-to-filename boundary.
- Removed raw exception strings from failure `QueryResult.metadata` in the
  counting, property, ranking, and semantic handlers, replacing them with
  stable internal codes.
- Failed manager results now use the error component and stable retry copy
  rather than ordinary assistant markdown (`UI-01`). Correlation references
  and structured retry actions remain part of the outcome-contract work below.
- Updated answer-generation instructions so product/vendor terminology and
  relevance scores are not encouraged in generated user answers.

Regression coverage:

- `tests/test_user_facing_copy.py` checks path-safe source labels,
  impact-focused service status, and known developer-copy regressions.
- Authentication tests cover invalid claims returning safe UI copy.
- Validation and low-confidence tests assert user wording and reject technical
  ratios, character labels, and threshold language.
- Repository scans found no direct exception interpolation in normal Streamlit
  rendering, no known cache/index/namespace/score-threshold UI phrases, and no
  raw handler exceptions in `QueryResult.metadata`.
- `ruff check .` passes.
- The full test run reached 358 passed and 5 skipped; the five tests blocked by
  permissions on the default user temp directory all passed when rerun with a
  workspace-local `--basetemp`. The focused UI/auth/validation suite reports
  103 passed.

This update itself completed the copy-remediation scope, not the entire
failure-state programme. The Phase 0 and Phase 1 sections below record the
subsequent outcome contract, correlation references, operator diagnostics, and
tenant/role validation. Typed retrieval-source outcomes and ingestion/FRA
integrity work remain open in later phases.

## Phase 1 update (completed 2026-07-20)

Building on the Phase 0 outcome contract, this milestone lands the central
presenter and the pre-retrieval access-context gate.

Implemented:

- Added `ui/error_presenter.py`: a single mapping from every `OutcomeStatus`
  (with optional failure-code overrides) to user-safe copy — a plain-language
  impact statement, one primary next action, whether retry helps, and an opaque
  correlation reference. It never interpolates exception, credential, endpoint,
  index, namespace, environment-variable, or provider detail; the only machine
  value it exposes is the `alf-xxxxxxxxxxxx` support reference. `unavailable`,
  `failed`, and `critical_inconsistent` always carry a reference even when no
  structured failure object is attached (Phase 1 items 1–2, UI-01).
- Failed manager results now render through the presenter instead of a single
  flat "search service" error, so status-specific copy, actions, and a support
  reference reach the user (`main.py::handle_query_with_manager`).
- Added `validate_access_context()` and enforced it before retrieval in both
  `QueryManager.process_query` and the legacy search path. An authenticated
  session with no usable tenant now returns `rejected/access_context_invalid`;
  an authenticated session with no usable app role returns
  `rejected/access_role_context_invalid`. Both receive privacy-preserving copy
  ("Your account could not be assigned data access") before retrieval. The
  access-filter builder also returns a deny-all filter for either condition as
  defence in depth (AUTH-08, AUTH-09, Phase 1 item 5).
- Authentication callback, provider-response, claim-validation, and
  configuration failures now create a structured `FailureInfo`, persist its
  stable code/retryability/reference for the current sign-in attempt, and emit
  a sanitised key/value log containing the stable code, component, and
  correlation reference. User surfaces show only safe copy and the opaque
  `alf-xxxxxxxxxxxx` support reference (AUTH-02, AUTH-05 through AUTH-07).
- Moved retrieval/cache/stats diagnostics behind an operator-only panel gated on
  an Entra ID app role (Phase 1 item 6). `OPERATOR_ROLES` (env `OPERATOR_ROLES`,
  default `data_steward`, comma-separated, case-sensitive) names the granting
  role; `auth.access_control.is_operator()` and the session-aware
  `current_user_is_operator()` fail closed for anonymous, roleless, or
  non-matching sessions. `ui.ui_components.render_operator_diagnostics()`
  surfaces index/namespace names, the score threshold, building-cache internals,
  and query-manager stats only to operators. `_store_auth_context` now logs the
  resolved roles and operator status at debug level so the claim can be verified.
  The app role is assigned in Entra and asserted by the ID token `roles` claim,
  so no in-app role selector is required. This gate is orthogonal to the
  document-level `allowed_roles` ACL, though a shared role value would also
  participate in retrieval filtering.

Regression coverage:

- `tests/test_error_presenter.py`: every `OutcomeStatus` maps to a component,
  message, and action; presented copy contains none of a forbidden-substring
  set (exception/tenant/index/namespace/provider/config/etc.); reference rules
  for retryable vs. terminal states; access rejection stays privacy-preserving.
- `tests/test_access_context_validation.py`: authenticated sessions without a
  tenant or without usable roles are rejected non-retryably before handler
  execution; blank tenant/role strings are treated as missing; the explicit
  anonymous development posture still proceeds.
- Authentication tests assert stable failure codes, retryability, correlation
  references, sanitised structured logs, and safe callback/claim UI state.
- `tests/test_operator_access.py`: operator role grants access; anonymous,
  roleless, non-matching, and case-mismatched roles fail closed; empty
  `OPERATOR_ROLES` grants no one; a different configured value gates instead of
  `data_steward` (env-configurability).
- Focused Phase 0/1 suite: 134 passed. Full suite: 474 passed, 5 skipped, no
  failures. The skips are Windows platform/capability cases (case-insensitive
  environment variables and unavailable symlink creation). `ruff check .`
  passes.

Phase 1 is complete. Correlation references now reach both query and sign-in
failure surfaces. Typed retrieval-source outcome migration remains Phase 2.

## Delivery plan

### Phase 0: Baseline and contracts

1. Freeze this register as the acceptance inventory.
2. Add characterization tests for every P0/P1 current behaviour.
3. Define `OutcomeStatus`, stable failure codes, retryability, and correlation IDs.
4. Document which dependencies are mandatory for UI query, optional for UI
   query, and mandatory for ingestion. Produce, as a named artifact, the
   per-index required/optional classification the Section B aggregation rules
   depend on.
5. Resolve whether `authentication_required()` can be false in production; this
   determines whether AUTH-13 is a live P0 or a dev-only posture.

Exit criteria:

- Every P0/P1 state has a named code and an owning test.
- No new broad `except` path may convert a failure directly to `[]`, `None`, or
  a nominal success without an explicit degraded outcome.

### Phase 1: UI safety and authentication

1. Add the central error presenter.
2. Replace all direct exception/configuration interpolation in Streamlit.
3. Replace technical input-validation, cache, no-answer, and no-results copy.
4. Catch token-claim validation failures.
5. Validate missing tenant/roles before retrieval.
6. Move index, namespace, threshold, cache, and architecture diagnostics behind
   an authenticated operator/debug panel.
7. Remove raw exceptions from `QueryResult.metadata`.

Exit criteria:

- Automated scan finds no `st.*(str(error))`, `f"...{e}"`, or
  `f"...{exc}"` failure rendering.
- Production and development user surfaces show no stack/config/provider
  details.
- Authentication contract violations produce controlled UI and structured logs.

### Phase 2: Query and retrieval truthfulness

1. Migrate semantic search to `SourceOutcome`.
2. Migrate structured search to the same contract.
3. Aggregate multi-index outcomes as success/empty/partial/unavailable.
4. Update handlers and `QueryManager` to preserve structured status.
5. Update Streamlit rendering to branch on status.
6. Prevent failed/unavailable results from being cached.
7. Separate answer generation from retrieval and support direct-results fallback.

Exit criteria:

- Simulated all-index outage never produces no-results copy.
- Simulated one-index outage with other results produces a partial warning.
- Genuine healthy zero matches produces only the empty state.
- Answer-generation outage still shows retrieved results.

### Phase 3: Degraded services and access control

1. Publish component readiness and degraded-mode telemetry.
2. Make Redis fail-open/fail-closed behaviour explicit per operation.
3. Surface material building-directory/classifier degradation appropriately.
4. Complete role-based fail-closed access policy. *(Completed early in Phase 1.)*
5. Identify and re-ingest/quarantine vectors without required ACL metadata.
6. Resolve the anonymous-access posture and fail closed on absent access context
   where authentication is mandatory (AUTH-13).

Exit criteria:

- Query throttling degradation is observable.
- Integrity-critical leases never fail open.
- Missing tenant/role context cannot masquerade as normal no results.
- ACL conformance can be measured and reaches the agreed deployment threshold.
- The anonymous/unfiltered retrieval path cannot expose ACL-restricted
  documents in mandatory-auth deployments.

### Phase 4: Ingestion and FRA state integrity

1. Implement unified file/run terminal states and CLI exit codes.
2. Make partial embedding/upsert outcomes explicit.
3. Reconcile vector success with registry-write failure.
4. Harden queue timeout, worker shutdown, and lingering-thread outcomes.
5. Add the durable FRA transaction journal, block-on-inconsistency state, and
   reconciliation command (FRA-12).
6. Roll back FRA supersession on any exception during execute/verify, and treat
   verification-read outages as retryable rather than absence (FRA-11,
   VECTOR-16).

Exit criteria:

- Every discovered file reaches one unambiguous terminal state.
- A partial file/run cannot be reported as success.
- Crash/fault-injection tests prove stale workers cannot overwrite newer state.
- Incomplete FRA rollback produces `critical_inconsistent`, blocks further
  affected writes, and generates an actionable alert/artifact.
- An unclassified exception during FRA execute/verify still triggers rollback
  and, if rollback is incomplete, `critical_inconsistent`.
- A verification-read outage never rolls back a successful upsert.
- A crash mid-supersession is recoverable from the durable journal.

### Phase 5: Operational rollout

1. Add dashboards and alerts for the new outcome metrics.
2. Run fault injection in a non-production environment for OpenAI, Pinecone,
   Redis, auth callback, registry, queue, and rollback paths.
3. Roll out behind feature flags for the new result contract and UI presenter.
4. Compare empty/partial/unavailable rates against baseline.
5. Remove compatibility fields and legacy tuple-return paths after all callers
   migrate.

Exit criteria:

- Operators can distinguish data-empty, access-empty, partial, unavailable, and
  failed requests without inspecting stack traces.
- User copy is stable, tested, and approved.
- Legacy boolean-only success and silent-empty failure paths are removed.

## Delivery risks and sequencing

These risks apply to the plan itself and must be managed during delivery.

- **The result contract is the linchpin.** `OutcomeStatus`/`SourceOutcome` and
  the temporary `success` compatibility property gate Phases 2–3. A
  half-migrated contract is worse than either end state; land the model plus its
  compat shim and tests as a discrete hardening milestone before any caller
  migration.
- **Integrity gaps can defeat the machinery meant to fix them.** FRA-11
  (unclassified exception skips rollback) can bypass the `critical_inconsistent`
  handling built in Phase 4, so fix FRA-11, FRA-12, and VECTOR-16 early rather
  than at the end of the phase.
- **AUTH-13 is deployment-conditional.** Whether the anonymous/unfiltered path
  is a live P0 depends on whether `authentication_required()` can be false in
  production. Resolve that in Phase 0; if mandatory auth is guaranteed, the fix
  is simply to fail closed on absent access context.
- **Required/optional source classification is a prerequisite.** The Section B
  aggregation rules cannot be tested until each index is classified; this is a
  named Phase 0 artifact, not a runtime detail.
- **The register is a snapshot and will drift.** Several rows cite line-level
  behaviour. Pin each P0/P1 row to the characterization test id that owns it so
  drift is caught by tests, not by re-reading the code.
- **Rollout needs a presenter kill-switch.** Phase 5 flags the new result
  contract and presenter; define what renders if the presenter itself raises, so
  a bug in error presentation cannot become a new unhandled-exception surface.

## Required test matrix

### UI and security tests

- Every `OutcomeStatus` maps to one expected component, message, and action.
- Production messages contain no exception strings, secrets, endpoints,
  environment-variable names, index names, namespace names, stack fragments, or
  provider payloads.
- Development details require an authenticated operator capability.
- Query, document metadata, generated answer, status description, and failure
  context remain safe against HTML/Markdown/script injection.
- Error responses receive a correlation ID without exposing user/session data.

### Authentication and access tests

- Missing auth configuration.
- Provider cancellation/error.
- Expired/missing OAuth flow.
- Token exchange timeout/exception.
- Invalid provider response.
- Missing stable user ID.
- Missing tenant ID.
- Missing, matching, and non-matching roles.
- Missing ACL metadata and cross-tenant metadata.
- Anonymous/auth-disabled session does not receive ACL-restricted documents in
  deployments where authentication is mandatory (AUTH-13).

### Query and retrieval tests

- Preprocessor failure.
- Classifier/model failure and pattern fallback.
- Handler negotiation failure and execution failure.
- Pinecone index-open failure.
- One/all namespace failures.
- OpenAI embedding rate limit, timeout, authentication, invalid request, missing
  model, unexpected error, and response-size mismatch.
- Healthy genuine no results.
- Access-filtered no results.
- Low-confidence results.
- Partial multi-index results.
- Unexpected router return contract.
- Empty model answer.
- Answer-generation and citation-retry failures.
- Session-memory and summary failures.
- Failure-cache exclusion.

### Ingestion and transaction tests

- Invalid configuration and local path.
- Traversal, symlink, changed file, file type, file size, read, decode, and
  extraction failures.
- No text, no FRA action plan, and low-confidence parsing.
- Per-file timeout and memory pressure.
- Partial embeddings and fatal embedding configuration errors.
- Upsert retry, split, singleton failure, shutdown, cancellation, queue timeout,
  and lingering worker.
- Verification delay and persistent missing IDs.
- Registry/job/event/metrics failure.
- Stale processing token.
- Supersession duplicate, lock timeout/loss, partial mutation, successful
  rollback, partial rollback, failed rollback, and reconciliation.
- Verification-read outage does not trigger rollback of a successful upsert
  (VECTOR-16).
- Unclassified exception during FRA execute/verify still triggers rollback and
  critical-inconsistent handling (FRA-11).
- Crash between supersession and rollback is recoverable from a durable journal
  (FRA-12).
- Job-registry read outage does not re-run a completed supersession (FRA-13).
- Run exit code for success, skips, empty input, partial, unavailable, failed,
  cancelled, and critical inconsistent.

## Definition of done

The failure-handling work is complete when:

1. Users can distinguish invalid requests, no authorised matching data,
   low-confidence results, partial results, and temporary service outages.
2. No dependency exception is silently translated into a genuine no-results
   state.
3. No raw exception or deployment/configuration detail is present in normal UI,
   session result metadata, exports, or API-ready result objects.
4. Every material fallback is represented as an explicit degraded or partial
   outcome and is observable.
5. Every ingestion file and run has an unambiguous terminal state.
6. FRA rollback failure blocks affected writes and has a tested reconciliation
   procedure.
7. P0 and P1 failure paths have deterministic automated tests and operator
   alerts where appropriate.
