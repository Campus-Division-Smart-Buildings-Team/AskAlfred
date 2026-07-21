#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Query Manager - Centralised query orchestration for AskAlfred.

"""

import copy
import json
import logging
import time
from typing import Any, Optional

from building.utils import BuildingCacheManager
from building.validation import INVALID_BUILDING_NAMES
from config import (
    QUERY_CONF_THRESHOLD,
    QUERY_FOLLOWUP_MAX_TOKENS,
    QUERY_FOLLOWUP_ML_CONF_THRESHOLD,
    QUERY_MANAGER_CONFIG,
    QUERY_RULE_OVERRIDE_THRESHOLD,
)
from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus, is_successful
from core.session_manager import SessionManager
from core.startup_readiness import missing_required_query_dependency
from core.telemetry import (
    COMPONENT_BUILDING_DIRECTORY,
    COMPONENT_INTENT_CLASSIFIER,
    get_readiness,
    get_telemetry,
)
from query_core.intent_classifier import NLPIntentClassifier
from query_core.query_context import (
    ACCESS_CONTROL_COMPONENT,
    QueryContext,
    auth_is_mandatory,
    build_access_filter,
    validate_access_context,
)
from query_core.query_result import QueryResult
from query_core.query_route import QueryRoute
from query_core.query_types import QueryType
from query_handlers import (
    ConversationalHandler,
    CountingHandler,
    MaintenanceHandler,
    PropertyHandler,
    RankingHandler,
    SemanticSearchHandler,
)
from query_handlers.handler_failures import handler_failed_result
from query_preprocessors import (
    BuildingExtractor,
    BusinessTermExtractor,
    QueryComplexityAnalyser,
    SpellCheckPreprocessor,
)
from security.log_sanitiser import sanitise_error
from ui.emojis import EMOJI_CAUTION, EMOJI_CROSS, EMOJI_TICK, EMOJI_TIME

# ============================================================================
# FOLLOWUP CONFIGs
# ============================================================================

# Only these terminal outcomes are safe to serve from cache. Transient failures
# (failed/unavailable) and incomplete/degraded results must not be replayed.
_CACHEABLE_STATUSES = frozenset(
    {
        OutcomeStatus.SUCCESS,
        OutcomeStatus.EMPTY,
        OutcomeStatus.LOW_CONFIDENCE,
    }
)

_QUERY_ROUTING_COMPONENT = "query_routing"
_DEPENDENCY_READINESS_COMPONENT = "dependency_readiness"

_PREPROCESSOR_COMPONENTS = {
    SpellCheckPreprocessor: "spell_check_preprocessor",
    BuildingExtractor: "building_extractor",
    BusinessTermExtractor: "business_term_extractor",
    QueryComplexityAnalyser: "query_complexity_analyser",
}
_MATERIAL_CONTEXT_PREPROCESSORS = frozenset(
    {
        "building_extractor",
        "business_term_extractor",
    }
)

_HANDLER_TYPES = {
    handler_type.__name__: handler_type
    for handler_type in (
        ConversationalHandler,
        MaintenanceHandler,
        RankingHandler,
        PropertyHandler,
        CountingHandler,
        SemanticSearchHandler,
    )
}

FOLLOWUP_PREFIXES = {
    "and",
    "also",
    "what about",
    "those",
    "them",
    "that",
    "this",
    "these",
    "any more",
    "more about",
    "more on",
    "tell me more",
}

FOLLOWUP_SUFFIXES = {"too", "as well", "also"}

FOLLOWUP_EXACT = {"and", "also", "what about", "tell me more", "more"}

FOLLOWUP_PRONOUNS = {"it", "this", "that", "those", "them", "these"}

# ============================================================================
# QUERY MANAGER
# ============================================================================


class QueryManager:
    """
    Orchestrates the full query lifecycle:
      • Build QueryContext
      • Run preprocessors
      • Execute handler chain
      • Cache responses
      • Track performance stats
    """

    # Default Routing Thresholds for Configuration
    DEFAULT_CONFIG = {
        # Thresholds for hybrid routing. Keys map to internal variables.
        "RULE_OVERRIDE_THRESHOLD": QUERY_RULE_OVERRIDE_THRESHOLD,
        "CONF_THRESHOLD": QUERY_CONF_THRESHOLD,
    }

    def __init__(
        self,
        config: Optional[dict] = None,
        intent_classifier: Optional[NLPIntentClassifier] = None,
    ):
        """
        Args:
            config (dict | None):
                Optional handler configuration. If None, default handlers are used.
            intent_classifier (NLPIntentClassifier | None):
                Optional shared classifier instance. Pass one (e.g. from a
                st.cache_resource factory) to avoid reloading the CT2 model
                per QueryManager; if None, a new classifier is created.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config = config
        self._handler_configuration_errors: list[str] = []

        # Merge routing config with defaults for tunability
        self.routing_config = self.DEFAULT_CONFIG.copy()
        # Allows routing thresholds to be passed in a 'routing' dict or at the top level
        if config:
            self.routing_config.update(config.get("routing", {}))
            # Also allow direct top-level overrides (for simplicity)
            for key in self.DEFAULT_CONFIG:
                if key in config:
                    self.routing_config[key] = config[key]

        # Build handler list
        if config is not None:
            self.handlers = self._load_handlers_from_config(config)
        else:
            self.handlers = self._initialise_default_handlers()

        # Sort handlers by priority (lower = higher priority)
        self.handlers.sort(key=lambda h: h.priority)
        self._semantic_fallback = self._validate_handler_graph()

        # Preprocessors
        self.preprocessors = self._initialise_preprocessors()

        # Cache (config-driven; entries are (stored_at, result) pairs)
        self.cache_enabled = bool(QUERY_MANAGER_CONFIG.get("enable_caching", False))
        self.cache_ttl_seconds = float(
            QUERY_MANAGER_CONFIG.get("cache_ttl_seconds", 300)
        )
        self.cache_max_entries = 128
        self.cache: dict[str, tuple[float, QueryResult]] = {}

        # Stats
        self.stats = {
            "handlers": {},  # per-handler stats
            "query_types": {},  # per QueryType stats
            "total_queries": 0,
            "overall_total_ms": 0.0,
            "cached_queries": 0,
        }

        if intent_classifier is not None:
            self.intent_clf = intent_classifier
        else:
            self.intent_clf = NLPIntentClassifier()

        # Map QueryType
        self.intent_to_handler = {}
        for h in self.handlers:
            if getattr(h, "query_type", None) is not None:
                self.intent_to_handler[h.query_type] = h

    # =========================================================================
    # Handler initialisation
    # =========================================================================

    def _initialise_default_handlers(self) -> list:
        """Create the default handler chain."""
        return [
            ConversationalHandler(),
            MaintenanceHandler(),
            RankingHandler(),
            PropertyHandler(),
            CountingHandler(),
            SemanticSearchHandler(),
        ]

    def _load_handlers_from_config(self, config: dict) -> list:
        """
        Load handlers from config. Config format:

            {
              "ConversationalHandler": {"enabled": True},
              "RankingHandler": {"enabled": True}
            }

        Missing/disabled handlers are skipped.
        """
        handlers = []

        # Routing thresholds may share this dict (top-level keys or a "routing"
        # sub-dict); skip them so they aren't mistaken for handler class names.
        reserved_keys = {"routing", *self.DEFAULT_CONFIG.keys()}

        for handler_cls_name, settings in config.items():
            if handler_cls_name in reserved_keys:
                continue
            handler_cls = _HANDLER_TYPES.get(handler_cls_name)
            if handler_cls is None:
                self._handler_configuration_errors.append("unknown_handler")
                self.logger.error("Unknown handler in configuration: %s", handler_cls_name)
                continue
            if not isinstance(settings, dict):
                self._handler_configuration_errors.append("invalid_handler_settings")
                self.logger.error(
                    "Invalid settings for handler: %s", handler_cls_name
                )
                continue
            if not settings.get("enabled", True):
                continue

            try:
                handlers.append(handler_cls())
            except Exception as e:
                self._handler_configuration_errors.append(
                    "handler_initialisation_failed"
                )
                self.logger.error("Could not load %s: %s", handler_cls_name, e)

        return handlers

    def _validate_handler_graph(self) -> SemanticSearchHandler | None:
        """Validate that routing has one final semantic fallback.

        Configuration problems are retained as low-cardinality validation
        reasons. ``process_query`` converts the invalid startup state into the
        public typed failure contract instead of attempting to execute a
        missing handler.
        """
        validation_errors = list(self._handler_configuration_errors)
        if not self.handlers:
            validation_errors.append("empty_handler_list")

        semantic_fallbacks = [
            handler
            for handler in self.handlers
            if isinstance(handler, SemanticSearchHandler)
        ]
        if not semantic_fallbacks:
            validation_errors.append("semantic_fallback_missing")
        elif len(semantic_fallbacks) > 1:
            validation_errors.append("semantic_fallback_duplicate")
        elif self.handlers[-1] is not semantic_fallbacks[0]:
            validation_errors.append("semantic_fallback_not_terminal")

        self._routing_graph_validation_errors = tuple(validation_errors)
        if validation_errors:
            self.logger.error(
                "Invalid query handler graph: %s",
                ",".join(validation_errors),
            )
            return None
        return semantic_fallbacks[0]

    @staticmethod
    def is_followup_query(
        q: str, prev_context: dict | None, *, previous_intent, prev_intent_confidence
    ) -> bool:
        if not q:
            return False

        q = q.strip().lower()
        tokens = q.split()

        # 1️⃣ Exact matches ("and", "more", etc.)
        if q in FOLLOWUP_EXACT:
            return True

        # 2️⃣ Prefix matches ("and show", "what about X")
        if any(q.startswith(p + " ") or q == p for p in FOLLOWUP_PREFIXES):
            return True

        # 3️⃣ Suffix matches ("too", "as well")
        if any(q.endswith(" " + s) or q == s for s in FOLLOWUP_SUFFIXES):
            return True

        # 4️⃣ Pronoun-led queries ("those with alarms", "them only")
        if tokens and tokens[0] in FOLLOWUP_PRONOUNS and prev_context:
            return True

        # 5️⃣ Ultra-short continuation ("more", "next", "continue")
        if len(tokens) <= QUERY_FOLLOWUP_MAX_TOKENS and prev_context:
            return True
        # 6️⃣ Previous turn was classified with low confidence + we have a prior
        # intent → treat a scope-less query as a continuation of it. (Follow-up
        # detection runs before this turn's classifier, so we key off the
        # *previous* turn's confidence, not the current one.)
        if (
            previous_intent
            and prev_context
            and not prev_context.get("building")
            and prev_intent_confidence is not None
            and prev_intent_confidence < QUERY_FOLLOWUP_ML_CONF_THRESHOLD
        ):
            return True
        return False

    def _maybe_inherit_followup_context(self, context: QueryContext) -> None:
        """
        If the user asked a follow-up (starts with 'and', 'what about', etc.)
        and preprocessors didn't extract scope (building), inherit
        it from the previous_context to maintain continuity.
        """
        q = context.query.strip().lower()
        prev = context.previous_context or {}

        if not self.is_followup_query(
            q,
            prev,
            previous_intent=context.previous_intent,
            prev_intent_confidence=context.previous_intent_confidence,
        ):
            return

        # inherit building
        prev_building = prev.get("building")
        if not context.building and prev_building:
            context.building = prev_building
            # Log the successful inheritance for debugging!
            self.logger.info(
                "%s CONTEXT INHERITED: Building '%s' from previous turn.",
                EMOJI_TICK,
                context.building,
            )
            context.routing_notes.append("inherited_building_from_previous_turn")
        else:
            # Log why inheritance did not occur
            self.logger.info(
                "%s CONTEXT INHERITANCE SKIPPED: Followup is '%s' but "
                "context.building is '%s' or "
                "previous context missing building (%s).",
                EMOJI_CROSS,
                q,
                context.building,
                "building" in prev,
            )

    # =========================================================================
    # Preprocessor initialisation
    # =========================================================================

    def _initialise_preprocessors(self) -> list:
        """
        Preprocessors run before handlers and enrich QueryContext.
        Order matters.
        """
        return [
            SpellCheckPreprocessor(),  # Optional: disabled by default
            BuildingExtractor(),
            BusinessTermExtractor(),
            QueryComplexityAnalyser(),
        ]

    # =========================================================================
    # Main processing pipeline
    # =========================================================================

    def process_query(self, query: str, **kwargs) -> QueryResult:
        """
        Main entry point.

        Args:
            query (str): The user query.
            kwargs: Passed to QueryContext (e.g., top_k, building_filter).

        Returns:
            QueryResult
        """
        self.logger.debug("Raw query received (%d chars)", len(query or ""))

        start_time = time.time()  # timing for this request

        if (
            hasattr(self, "_semantic_fallback")
            and self._semantic_fallback is None
        ):
            return self._routing_graph_invalid_result(query, start_time)

        # Create context
        context = QueryContext(query=query, **kwargs)

        # Fail closed before retrieval when an authenticated session has no
        # usable access context. Otherwise the deny-all filter below yields
        # zero matches that are indistinguishable from a genuine empty result.
        mandatory_auth = auth_is_mandatory()
        access_failure = validate_access_context(
            authenticated=context.authenticated,
            tenant_id=context.tenant_id,
            user_roles=context.user_roles,
            auth_mandatory=mandatory_auth,
        )
        if access_failure is not None:
            self.logger.warning(
                "access_context_rejected code=%s correlation_id=%s component=%s",
                access_failure.code.value,
                access_failure.correlation_id,
                access_failure.component,
            )
            get_telemetry().record_request_outcome(
                OutcomeStatus.REJECTED, access_failure.code
            )
            elapsed_ms = (time.time() - start_time) * 1000
            return QueryResult(
                query=query,
                answer=None,
                results=[],
                handler_used=ACCESS_CONTROL_COMPONENT,
                query_type=ACCESS_CONTROL_COMPONENT,
                status=OutcomeStatus.REJECTED,
                failure=access_failure,
                processing_time_ms=elapsed_ms,
            )

        if context.access_filter is None:
            context.access_filter = build_access_filter(
                tenant_id=context.tenant_id,
                user_roles=context.user_roles,
                authenticated=context.authenticated,
                auth_mandatory=mandatory_auth,
            )

        # Fail fast before executing the query when a required dependency
        # (OpenAI, Pinecone) was found unconfigured at startup. Retrieval or
        # answer generation would otherwise raise a ConfigError deep in a
        # handler; here it surfaces as a typed ``unavailable`` outcome with the
        # configuration cause kept in logs/operator diagnostics only (START-09).
        missing_dependency = missing_required_query_dependency()
        if missing_dependency is not None:
            return self._required_dependency_unavailable_result(
                query, missing_dependency, start_time
            )

        # ---------------------------------------------------
        # Load previous conversational memory from SessionManager
        # ---------------------------------------------------
        prev_context_dict = SessionManager.get_last_query_context()
        prev_intent, prev_conf = SessionManager.get_last_intent()

        # Defensive Logging
        if prev_context_dict:
            self.logger.info(
                "MEMORY LOADED: Previous building: %r",
                prev_context_dict.get("building"),
            )

        # Attach previous QueryContext data (if any)
        if prev_context_dict:
            context.previous_context = prev_context_dict
        else:
            context.previous_context = None

        # Attach previous intent + confidence
        context.previous_intent = prev_intent
        context.previous_intent_confidence = prev_conf

        # Store this info in routing notes for debugging
        if prev_context_dict:
            context.routing_notes.append("previous_context_available")
        if prev_intent:
            context.routing_notes.append(
                f"previous_intent={prev_intent}, conf={prev_conf}"
            )

        # ---------------------------------------------------
        # Preprocessors
        # ---------------------------------------------------
        preprocessor_degradations = self._run_preprocessors(context)
        # 🔧 Normalise / clean building extracted by preprocessors
        if context.building and context.building.lower() in INVALID_BUILDING_NAMES:
            self.logger.info(
                "%s Discarding invalid building from preprocessors: %r",
                EMOJI_CAUTION,
                context.building,
            )
            context.building = None
            context.building_filter = None
            context.routing_notes.append("invalid_building_cleared")

        self._maybe_inherit_followup_context(context)

        if context.building and not context.building_filter:
            context.building_filter = context.building
            context.routing_notes.append("synchronised_building_filter")

        # Surface building-directory degradation (ROUTE-03). When the building
        # cache is unavailable, recognition falls back to pattern/n-gram matching
        # with reduced recall; a building-scoped query is materially affected.
        building_directory_degraded = self._record_building_directory_readiness(context)

        # Cache check (must happen *after* preprocessors + follow-up inheritance,
        # so the cache key includes the correct building scope)
        cache_key = self._make_cache_key(context)
        cached = self._get_cached_result(cache_key)
        if cached is not None:
            self.stats["cached_queries"] += 1

            query_result = cached

            self._apply_preprocessor_degradation(
                query_result, context, preprocessor_degradations
            )

            # persist context snapshot
            SessionManager.set_last_query_context(context)

            # use cached result’s query_type + no confidence (confidence was for ML path)
            SessionManager.set_last_intent(query_result.query_type, None)

            elapsed_ms = (time.time() - start_time) * 1000

            # Record telemetry for cached responses (coerce None -> "unknown")
            self._update_stats(
                handler_class_name=query_result.handler_used or "unknown",
                query_type=query_result.query_type or "unknown",
                elapsed_ms=elapsed_ms,
                success=is_successful(query_result.status),
            )
            self._record_outcome_telemetry(query_result)

            query_result.processing_time_ms = elapsed_ms
            return query_result

        self.logger.debug("Final query before routing: %r", context.query)

        # ---------------------------------------------------
        # Routing
        # ---------------------------------------------------
        route = self._route_query_hybrid(context)

        if route.handler is None:
            return self._routing_graph_invalid_result(query, start_time)

        # ---------------------------------------------------
        # Execute handler
        # ---------------------------------------------------
        handler_start = time.time()
        try:
            query_result = route.handler.handle(context)
        except Exception as exc:
            # This is the final exception boundary for every query handler.
            # Individual handlers may translate known dependency failures more
            # precisely, while anything unexpected still leaves the manager as
            # a transport-safe, typed outcome for UI, API, and direct callers.
            query_result = handler_failed_result(
                context.query,
                route.handler.__class__.__name__,
                route.handler.query_type.value,
                error_code=FailureCode.HANDLER_EXECUTION_FAILED.value,
            )
            failure = query_result.failure
            self.logger.error(
                "handler_execution_failed code=%s correlation_id=%s "
                "component=%s handler=%s error=%s",
                failure.code.value,
                failure.correlation_id,
                failure.component,
                route.handler.__class__.__name__,
                sanitise_error(exc),
                exc_info=False,
            )
        handler_elapsed_ms = (time.time() - handler_start) * 1000

        # Attach handler metadata
        query_result.handler_used = route.handler.__class__.__name__
        query_result.query_type = route.handler.query_type.value

        if isinstance(route.metadata, dict):
            query_result.metadata.update(route.metadata)

        # Record every failed preprocessor on this request. Only failures that
        # removed context needed by a non-conversational answer downgrade the
        # result and trigger the UI's concise degraded-capability warning.
        self._apply_preprocessor_degradation(
            query_result, context, preprocessor_degradations
        )

        # A building-scoped query answered while the building directory is
        # degraded may have reduced recall; mark it degraded so the UI can warn
        # and so the reduced-recall answer is not cached (ROUTE-03).
        self._apply_building_directory_degradation(
            query_result, building_directory_degraded
        )

        # ---------------------------------------------------
        # Stats update (expanded telemetry)
        # ---------------------------------------------------
        self._update_stats(
            handler_class_name=query_result.handler_used or "unknown",
            query_type=query_result.query_type or "unknown",
            elapsed_ms=handler_elapsed_ms,
            success=is_successful(query_result.status),
        )

        # Cache result. Only cache trustworthy terminal outcomes: a transient
        # failed/unavailable/partial/degraded result must not be replayed from
        # cache until its TTL expires (plan section A / ROUTE-12).
        if (
            self.cache_enabled
            and query_result.status in _CACHEABLE_STATUSES
            and not preprocessor_degradations
        ):
            self._store_cached_result(cache_key, query_result)

        # ---------------------------------------------------
        # 5. CONVERSATIONAL MEMORY PERSISTENCE
        # ---------------------------------------------------
        self.logger.debug(
            "Memory persistence check: context.building is %r", context.building
        )
        try:
            # Save compact QueryContext into session memory
            SessionManager.set_last_query_context(context)

            # Save ML intent (if available) otherwise fallback to handler type
            final_intent = (
                context.predicted_intent
                if context.predicted_intent
                else route.handler.query_type
            )
            SessionManager.set_last_intent(final_intent, context.ml_intent_confidence)
        except Exception as e:
            self.logger.error("Failed to persist session memory: %s", e)

        # Total round-trip time for everything
        total_elapsed_ms = (time.time() - start_time) * 1000
        query_result.processing_time_ms = total_elapsed_ms
        logging.info(
            "%s QueryManager.process_query took %.2f ms",
            EMOJI_TIME,
            query_result.processing_time_ms,
        )

        self._record_outcome_telemetry(query_result)
        return query_result

    # =========================================================================
    # Degraded-mode telemetry (plan section H, ROUTE-03, ROUTE-05)
    # =========================================================================

    def _routing_graph_invalid_result(
        self, query: str, start_time: float
    ) -> QueryResult:
        """Return the stable ROUTE-08 outcome for an invalid handler graph."""
        failure = FailureInfo.from_code(
            FailureCode.ROUTING_GRAPH_INVALID,
            _QUERY_ROUTING_COMPONENT,
            safe_context={"validation": "handler_graph"},
        )
        elapsed_ms = (time.time() - start_time) * 1000
        result = QueryResult(
            query=query,
            answer=None,
            results=[],
            status=OutcomeStatus.FAILED,
            failure=failure,
            processing_time_ms=elapsed_ms,
            metadata={"route": "graph_invalid"},
        )
        self.logger.error(
            "routing_graph_invalid code=%s correlation_id=%s component=%s",
            failure.code.value,
            failure.correlation_id,
            failure.component,
        )
        self._update_stats(
            handler_class_name=_QUERY_ROUTING_COMPONENT,
            query_type=_QUERY_ROUTING_COMPONENT,
            elapsed_ms=elapsed_ms,
            success=False,
        )
        self._record_outcome_telemetry(result)
        return result

    def _required_dependency_unavailable_result(
        self, query: str, component: str, start_time: float
    ) -> QueryResult:
        """Return the START-09 ``unavailable`` outcome for a missing dependency.

        ``component`` is the low-cardinality dependency name (e.g. ``openai``)
        recorded in ``safe_context`` for operator diagnostics; the underlying
        configuration cause is only in logs, never in the user-facing result.
        """
        failure = FailureInfo.from_code(
            FailureCode.DEPENDENCY_UNAVAILABLE,
            _DEPENDENCY_READINESS_COMPONENT,
            safe_context={"dependency": component},
        )
        elapsed_ms = (time.time() - start_time) * 1000
        result = QueryResult(
            query=query,
            answer=None,
            results=[],
            status=OutcomeStatus.UNAVAILABLE,
            failure=failure,
            processing_time_ms=elapsed_ms,
            metadata={"route": "dependency_unavailable"},
        )
        self.logger.error(
            "required_dependency_unavailable code=%s correlation_id=%s "
            "component=%s dependency=%s",
            failure.code.value,
            failure.correlation_id,
            failure.component,
            component,
        )
        self._update_stats(
            handler_class_name=_DEPENDENCY_READINESS_COMPONENT,
            query_type=_DEPENDENCY_READINESS_COMPONENT,
            elapsed_ms=elapsed_ms,
            success=False,
        )
        self._record_outcome_telemetry(result)
        return result

    def _record_building_directory_readiness(self, context: QueryContext) -> bool:
        """Publish building-directory readiness; return True if it degrades this query.

        Returns True only when the directory is unavailable *and* the query is
        building-scoped, so a concise capability warning is warranted.
        """
        try:
            populated = BuildingCacheManager.is_populated()
        except Exception:  # pragma: no cover - defensive
            populated = False

        if populated:
            get_readiness().mark_ready(COMPONENT_BUILDING_DIRECTORY)
            return False

        get_readiness().mark_degraded(
            COMPONENT_BUILDING_DIRECTORY,
            FailureCode.BUILDING_DIRECTORY_UNAVAILABLE,
        )
        get_telemetry().record_fallback(COMPONENT_BUILDING_DIRECTORY)

        building_scoped = bool(context.building_filter or context.building)
        return building_scoped

    def _apply_building_directory_degradation(
        self, query_result: QueryResult, degraded: bool
    ) -> None:
        """Mark a building-scoped result degraded when the directory is unavailable."""
        if not degraded:
            return
        if COMPONENT_BUILDING_DIRECTORY not in query_result.degraded_components:
            query_result.degraded_components.append(COMPONENT_BUILDING_DIRECTORY)
        # Only downgrade trustworthy outcomes; never upgrade a worse outcome
        # (failed/unavailable/partial/rejected) to degraded.
        if query_result.status in _CACHEABLE_STATUSES:
            query_result.status = OutcomeStatus.DEGRADED

    def _record_intent_classifier_degraded(self) -> None:
        """Record an intent-classifier fallback for this query (ROUTE-05)."""
        get_readiness().mark_degraded(COMPONENT_INTENT_CLASSIFIER)
        get_telemetry().record_fallback(COMPONENT_INTENT_CLASSIFIER)

    def _apply_preprocessor_degradation(
        self,
        query_result: QueryResult,
        context: QueryContext,
        degraded_components: list[str] | None,
    ) -> None:
        """Attach ROUTE-01 failures and warn only for materially reduced context."""
        components = list(dict.fromkeys(degraded_components or []))
        if not components:
            return

        for component in components:
            if component not in query_result.degraded_components:
                query_result.degraded_components.append(component)
        query_result.metadata["preprocessor_degradations"] = components

        if query_result.query_type == QueryType.CONVERSATIONAL.value:
            return

        material_components = set(components) & _MATERIAL_CONTEXT_PREPROCESSORS
        # Building extraction is immaterial when explicit or inherited scope is
        # already available despite the failed extractor.
        if context.building_filter:
            material_components.discard("building_extractor")

        if material_components and query_result.status in _CACHEABLE_STATUSES:
            query_result.status = OutcomeStatus.DEGRADED

    def _record_outcome_telemetry(self, query_result: QueryResult) -> None:
        """Record the terminal request outcome by status and failure code."""
        failure_code = (
            query_result.failure.code if query_result.failure is not None else None
        )
        get_telemetry().record_request_outcome(query_result.status, failure_code)

    # =========================================================================
    # Preprocessor execution
    # =========================================================================

    def _run_preprocessors(self, context: QueryContext) -> list[str]:
        """Run preprocessors, retaining stable request-scoped failure details."""
        degraded_components: list[str] = []
        for pre in self.preprocessors:
            try:
                if pre.should_run(context):
                    pre.process(context)
            except Exception as e:
                component = _PREPROCESSOR_COMPONENTS.get(
                    type(pre), "query_preprocessor"
                )
                if component not in degraded_components:
                    degraded_components.append(component)
                    get_telemetry().record_fallback(component)
                self.logger.error(
                    "preprocessor_degraded component=%s error=%s",
                    component,
                    sanitise_error(e),
                    exc_info=False,
                )

        if degraded_components:
            context.add_to_cache(
                "preprocessor_degradations", list(degraded_components)
            )
            context.routing_notes.extend(
                f"preprocessor_degraded:{component}"
                for component in degraded_components
            )
        return degraded_components

    # =========================================================================
    # Query routing
    # =========================================================================

    def _route_query_hybrid(self, context: QueryContext) -> QueryRoute:
        """
        Option D (Hybrid) routing:

        1) Rule layer: try handlers' can_handle() (clear-cut cases)
        2) ML classifier: predict intent + confidence for ambiguous cases
        3) Thresholds: if conf < 0.6 -> SemanticSearch with intent in context
        4) Negotiation: chosen handler can still reject; then fallback to SemanticSearch
        """
        # -----------------------------
        # 1) RULE LAYER (handlers)
        # -----------------------------
        best_handler = None
        best_priority = float("inf")

        for h in self.handlers:
            try:
                if h.can_handle(context):
                    if h.priority < best_priority:
                        best_priority = h.priority
                        best_handler = h
            except Exception as e:
                self.logger.error(
                    "Handler %s failed during can_handle(): %s",
                    h.__class__.__name__,
                    sanitise_error(e),
                    exc_info=False,
                )

        # --------------------------------------------------------
        # RULE OVERRIDE LOGIC
        # "Rule layer wins UNLESS ML predicts semantic_search
        #   with high confidence (≥ 0.75)"
        # --------------------------------------------------------
        RULE_OVERRIDE_THRESHOLD = self.routing_config["RULE_OVERRIDE_THRESHOLD"]

        if best_handler and best_handler.__class__.__name__ != "SemanticSearchHandler":
            # Defer ML override check until ML is computed
            rule_candidate = best_handler
        else:
            rule_candidate = None

        # ----------------------------------
        # 2) ML CLASSIFIER for ambiguous case
        # ----------------------------------
        try:
            ml = self.intent_clf.classify_intent(context.query, context)
            context.predicted_intent = ml.intent
            context.ml_intent_confidence = ml.confidence
            self.logger.info(
                "ML intent: %s (%.2f)",
                ml.intent.value if hasattr(ml.intent, "value") else ml.intent,
                ml.confidence,
            )
        except Exception as e:
            self.logger.error(
                "Intent classifier failed: %s", sanitise_error(e), exc_info=False
            )
            self._record_intent_classifier_degraded()
            ml = None

        # --------------------------------------------------------
        # RULE override check
        # rule wins unless ML strongly believes the query should
        # be semantic search (intent=SEMANTIC_SEARCH + high conf)
        # --------------------------------------------------------
        if rule_candidate:
            if (
                ml
                and ml.intent == QueryType.SEMANTIC_SEARCH
                and ml.confidence >= RULE_OVERRIDE_THRESHOLD
            ):
                context.routing_notes.append("ml_override_rule_layer")
                # fall through to ML-based semantic routing
            else:
                # Rule layer restored
                context.routing_notes.append("rule_layer_selected")
                return QueryRoute(
                    handler=rule_candidate,
                    metadata={
                        "route": "rule",
                        "ml_intent": getattr(ml.intent, "value", None) if ml else None,
                        "ml_confidence": getattr(ml, "confidence", None),
                        "ml_route_reason": "rule_not_overridden",
                    },
                )

        # -------------------------------------------------------------------
        # 3) CONFIDENCE THRESHOLD -> route to general RAG if confidence is low
        # -------------------------------------------------------------------
        CONF_THRESHOLD = self.routing_config["CONF_THRESHOLD"]
        if ml is None or ml.confidence < CONF_THRESHOLD:
            context.routing_notes.append("ml_low_confidence_to_semantic")
            return QueryRoute(
                handler=self._semantic_fallback,
                metadata={
                    "route": "semantic_fallback_low_conf",
                    "ml_intent": ml.intent.value if ml else None,
                    "ml_confidence": ml.confidence if ml else None,
                    "ml_route_reason": "confidence_below_threshold",
                },
            )

        # -------------------------------------------------------------------
        # 4) Choose handler by ML intent, then let it "negotiate" via can_handle
        # -------------------------------------------------------------------
        target_handler = self.intent_to_handler.get(ml.intent)
        if target_handler is None:
            # No dedicated handler? Default to SemanticSearch.
            context.routing_notes.append("ml_handler_missing_to_semantic")
            return QueryRoute(
                handler=self._semantic_fallback,
                metadata={
                    "route": "ml_missing_semantic",
                    "ml_intent": (
                        ml.intent.value
                        if hasattr(ml.intent, "value")
                        else str(ml.intent)
                    ),
                    "ml_confidence": ml.confidence,
                },
            )

        # Give the handler a chance to reject based on enriched context
        try:
            if target_handler.can_handle(context):
                context.routing_notes.append("ml_selected_handler_accepted")
                return QueryRoute(
                    handler=target_handler,
                    metadata={
                        "route": "ml_handler",
                        "ml_intent": (
                            ml.intent.value
                            if hasattr(ml.intent, "value")
                            else str(ml.intent)
                        ),
                        "ml_confidence": ml.confidence,
                    },
                )
            else:
                context.routing_notes.append("ml_selected_handler_rejected")
        except Exception as e:
            self.logger.error(
                "Handler %s failed during negotiation: %s",
                target_handler.__class__.__name__,
                sanitise_error(e),
                exc_info=False,
            )
            context.routing_notes.append("ml_selected_handler_error")

        # Final fallback: SemanticSearch
        return QueryRoute(
            handler=self._semantic_fallback,
            metadata={
                "route": "semantic_fallback_negotiation",
                "ml_intent": ml.intent.value if ml else None,
                "ml_confidence": ml.confidence if ml else None,
                "ml_route_reason": "handler_rejected_or_missing",
            },
        )

    # =========================================================================
    # Cache + stats helpers
    # =========================================================================

    def _get_cached_result(self, cache_key: str) -> Optional[QueryResult]:
        """Return a live cache entry, expiring it if the TTL has lapsed."""
        if not self.cache_enabled:
            return None
        entry = self.cache.get(cache_key)
        if entry is None:
            return None
        stored_at, result = entry
        if time.time() - stored_at > self.cache_ttl_seconds:
            self.cache.pop(cache_key, None)
            return None
        return copy.deepcopy(result)

    def _store_cached_result(self, cache_key: str, result: QueryResult) -> None:
        """Insert into the cache, evicting the oldest entry when full."""
        if len(self.cache) >= self.cache_max_entries:
            self.cache.pop(next(iter(self.cache)))
        self.cache[cache_key] = (time.time(), copy.deepcopy(result))

    def _make_cache_key(self, context: QueryContext) -> str:
        """
        Deterministic cache key. Uses the corrected query if preprocessors
        ran, as this reflects the content actually processed by handlers,
        improving cache validity.
        """
        # Use the corrected query if SpellCheck or another preprocessor ran,
        # otherwise use the original query.
        query_part = context.corrected_query or context.query

        # Ensure we capture building context which influences the search results
        building_part = context.building_filter or ""
        access_part = (
            json.dumps(context.access_filter, sort_keys=True, default=str)
            if context.access_filter
            else ""
        )

        return (
            f"{context.user_id}:{query_part}:{context.top_k}:"
            f"{building_part}:{access_part}"
        )

    def _update_stats(
        self, handler_class_name: str, query_type: str, elapsed_ms: float, success: bool
    ):
        # --- Update global totals ---
        self.stats["total_queries"] += 1
        self.stats["overall_total_ms"] += elapsed_ms

        # --- Per-handler stats ---
        hstats = self.stats["handlers"].setdefault(
            handler_class_name,
            {
                "count": 0,
                "total_ms": 0.0,
                "min_ms": float("inf"),
                "max_ms": 0.0,
                "successes": 0,
                "success_rate": 0.0,
                "avg_ms": 0.0,
            },
        )

        hstats["count"] += 1
        hstats["total_ms"] += elapsed_ms
        hstats["min_ms"] = min(hstats["min_ms"], elapsed_ms)
        hstats["max_ms"] = max(hstats["max_ms"], elapsed_ms)
        if success:
            hstats["successes"] += 1

        hstats["avg_ms"] = hstats["total_ms"] / hstats["count"]
        hstats["success_rate"] = hstats["successes"] / hstats["count"]

        # --- Per query type stats ---
        tstats = self.stats["query_types"].setdefault(
            query_type,
            {
                "count": 0,
                "total_ms": 0.0,
                "min_ms": float("inf"),
                "max_ms": 0.0,
                "successes": 0,
                "success_rate": 0.0,
                "avg_ms": 0.0,
            },
        )

        tstats["count"] += 1
        tstats["total_ms"] += elapsed_ms
        tstats["min_ms"] = min(tstats["min_ms"], elapsed_ms)
        tstats["max_ms"] = max(tstats["max_ms"], elapsed_ms)
        if success:
            tstats["successes"] += 1

        tstats["avg_ms"] = tstats["total_ms"] / tstats["count"]
        tstats["success_rate"] = tstats["successes"] / tstats["count"]

    # =========================================================================
    # Debug helpers
    # =========================================================================

    def print_handler_chain(self):
        """Display the handler chain in execution order."""
        print("\nHandler Chain (priority order):")
        for h in sorted(self.handlers, key=lambda h: h.priority):
            print(f"  {h.priority:2d}  {h.__class__.__name__}")

    def print_stats(self):
        """Print expanded telemetry for debugging."""
        print("\n=== Alfred Telemetry ===")

        total = self.stats["total_queries"]
        overall_avg = self.stats["overall_total_ms"] / total if total > 0 else 0.0

        print(f"Total queries: {total}")
        print(f"Overall avg time: {overall_avg:.2f} ms\n")

        print("Handlers:")
        for h_name, h_stats in self.stats["handlers"].items():
            print(f"  {h_name}:")
            print(f"    Count:          {h_stats['count']}")
            print(f"    Avg time:       {h_stats['avg_ms']:.2f} ms")
            print(
                f"    Min/Max:        {h_stats['min_ms']:.2f} / {h_stats['max_ms']:.2f} ms"
            )
            print(f"    Success rate:   {h_stats['success_rate']:.1%}")

        print("\nQuery Types:")
        for q_type, q_stats in self.stats["query_types"].items():
            print(f"  {q_type}:")
            print(f"    Count:          {q_stats['count']}")
            print(f"    Avg time:       {q_stats['avg_ms']:.2f} ms")
            print(
                f"    Min/Max:        {q_stats['min_ms']:.2f} / {q_stats['max_ms']:.2f} ms"
            )
            print(f"    Success rate:   {q_stats['success_rate']:.1%}")

        print("\n=========================\n")

    def get_statistics(self):
        total = self.stats["total_queries"]
        avg_time = self.stats["overall_total_ms"] / total if total > 0 else 0.0

        return {
            "total_queries": total,
            "avg_time_ms": avg_time,
            "cached_queries": self.stats["cached_queries"],
            "handlers": self.stats["handlers"],
            "query_types": self.stats["query_types"],
        }


# ============================================================================
# CONVENIENCE FUNCTION FOR EXISTING CODE
# ============================================================================

# Module-level QueryManager reused across process_query_unified() calls so the
# CT2 encoder and intent embeddings are loaded once, not per call.
_DEFAULT_MANAGER: Optional["QueryManager"] = None


def _get_default_manager() -> "QueryManager":
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = QueryManager()
    return _DEFAULT_MANAGER


def process_query_unified(
    user_query: str, top_k: int = 10, **kwargs
) -> tuple[
    list[Any],  # results from semantic search
    Optional[str],  # answer
    Any,  # publication_date_info
    Optional[bool],  # score_too_low
]:
    """
    Convenience wrapper for backward compatibility with existing code.

    Returns same format as perform_federated_search() for easy migration.

    Args:
        user_query: User query
        top_k: Number of results
        **kwargs: Additional context

    Returns:
        tuple of (results, answer, publication_date_info, score_too_low)
    """
    query_mgr = _get_default_manager()
    query_result = query_mgr.process_query(user_query, top_k=top_k, **kwargs)

    return (
        query_result.results,
        query_result.answer or "",
        query_result.publication_date_info or "",
        bool(query_result.score_too_low),
    )


# ============================================================================
# EXAMPLE USAGE
# ============================================================================


if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    # Create manager
    manager = QueryManager()

    # Example queries
    test_queries = [
        "Hello Alfred",
        "How many buildings have FRAs?",
        "Show maintenance requests for Senate House",
        "Rank buildings by area",
        "What buildings are Condition A?",
        "What is the BMS configuration for HVAC?",
    ]

    print("=" * 80)
    print("Query Manager Test Run")
    print("=" * 80)

    for test_query in test_queries:
        print(f"\n📝 Query: {test_query}")
        result = manager.process_query(test_query)
        print(f"✅ Type: {result.query_type}")
        print(f"⏱️  Time: {result.processing_time_ms:.2f}ms")
        print(f"📊 Handler: {result.handler_used}")
        print(
            f"💬 Answer preview: {result.answer[:100] if result.answer else 'No answer available'}..."
        )

    # Show statistics
    print("\n" + "=" * 80)
    print("Statistics")
    print("=" * 80)
    stats = manager.get_statistics()

    print(f"Total queries: {stats['total_queries']}")
    print(f"Average time: {stats['avg_time_ms']:.2f}ms")
    print(f"Cached queries: {stats['cached_queries']}")

    print("\nHandlers:")
    for handler_name, s in stats["handlers"].items():
        print(f"  {handler_name}: {s['count']} uses")

    print("\nQuery Types:")
    for qtype, s in stats["query_types"].items():
        print(f"  {qtype}: {s['count']} uses")
