"""Regression coverage for the "sticky building" maintenance bug.

When a maintenance query explicitly names a building that cannot be recognised
(e.g. a garbled speech-to-text name such as "old dark will" for "Old Park
Hill"), the handler must NOT silently inherit the previous turn's building and
confidently answer about the wrong one. It must ask for clarification instead.
A genuinely scope-less follow-up ("what about the complete ones?") still
inherits conversational context.
"""

import pytest

import search_core.generate_maintenance_answers as gma
from domain.maintenance_utils import extract_unresolved_building_phrase


class TestExtractUnresolvedBuildingPhrase:
    @pytest.mark.parametrize(
        "query,expected",
        [
            (
                "tell me about the maintenance requests at old dark will",
                "old dark will",
            ),
            ("maintenance requests for old park hill", "old park hill"),
            ("requests at 1-9 old park hill", "1-9 old park hill"),
        ],
    )
    def test_detects_an_explicitly_named_building(self, query, expected):
        assert extract_unresolved_building_phrase(query) == expected

    @pytest.mark.parametrize(
        "query",
        [
            "show me requests in progress",              # status, not a building
            "how many jobs for heating",                 # category, not a building
            "requests for completed jobs",               # status + query type
            "maintenance requests for the past 3 months",  # date scope
            "what about the complete ones",              # scope-less follow-up
            "how many maintenance jobs",                 # no building preposition
            "",
        ],
    )
    def test_ignores_scopes_that_do_not_name_a_building(self, query):
        assert extract_unresolved_building_phrase(query) is None


def _stub_cache(monkeypatch, known=("Senate House",)):
    monkeypatch.setattr(
        gma.BuildingCacheManager, "ensure_initialised", staticmethod(lambda: None)
    )
    monkeypatch.setattr(
        gma.BuildingCacheManager, "is_populated", staticmethod(lambda: True)
    )
    monkeypatch.setattr(
        gma.BuildingCacheManager,
        "get_known_buildings",
        staticmethod(lambda: list(known)),
    )


def _stub_parse_no_building(monkeypatch):
    monkeypatch.setattr(
        gma,
        "parse_maintenance_query",
        lambda *_a, **_k: {
            "building_name": None,
            "category": None,
            "priority": None,
            "status": None,
            "query_type": "requests",
        },
    )


def test_unrecognised_named_building_asks_for_clarification(monkeypatch):
    """The user named a building we couldn't resolve -> clarify, never inherit."""
    _stub_cache(monkeypatch)
    _stub_parse_no_building(monkeypatch)

    def _no_pinecone(*_a, **_k):
        raise AssertionError(
            "Pinecone must not be queried once we decide to ask for clarification"
        )

    monkeypatch.setattr(gma, "open_index", _no_pinecone)

    answer = gma.generate_maintenance_answer(
        "tell me about the maintenance requests at old dark will",
        building_override="Senate House",
    )

    assert answer is not None
    assert "couldn't identify" in answer.lower()
    # It echoes the unrecognised name, not the previous building's data.
    assert "Old Dark Will" in answer


def test_scopeless_followup_still_inherits_previous_building(monkeypatch):
    """No building named -> the previous building is inherited (not clarified)."""
    _stub_cache(monkeypatch)
    _stub_parse_no_building(monkeypatch)
    monkeypatch.setattr(gma, "TARGET_INDEXES", ["testidx"])

    calls: list[str] = []

    def _record_open_index(name):
        calls.append(name)
        raise RuntimeError("stop after the inheritance decision")

    monkeypatch.setattr(gma, "open_index", _record_open_index)

    # A scope-less follow-up: no "at/for <building>" phrase to resolve. Reaching
    # retrieval (open_index) proves we did NOT short-circuit with a clarification
    # message; the per-index failure is later surfaced as an unavailable outcome.
    with pytest.raises(Exception):
        gma.generate_maintenance_answer(
            "what about the complete ones",
            building_override="Senate House",
        )

    assert calls, "expected retrieval to proceed for a scope-less follow-up"
