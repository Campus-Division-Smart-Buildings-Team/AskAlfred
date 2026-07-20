"""Regression tests for user-facing copy and source labels."""

from pathlib import Path

from search_core.generate_semantic_answer import format_date_information
from ui.ui_components import _public_dependency_status, get_source_label


def test_source_label_hides_internal_paths():
    result = {"key": r"internal\archive\Senate House FRA.pdf"}

    assert get_source_label(result, 1) == "Senate House FRA.pdf"


def test_source_label_uses_neutral_fallback():
    assert get_source_label({"key": "__default__"}, 3) == "Source 3"


def test_publication_info_hides_internal_path():
    _, publication_info = format_date_information(
        planon_date=None,
        operational_date="2026-07-20",
        operational_doc_key=r"private\storage\Senate House BMS.pdf",
    )

    assert "private" not in publication_info
    assert "storage" not in publication_info
    assert "Senate House BMS.pdf" in publication_info


def test_dependency_status_copy_is_impact_focused():
    assert _public_dependency_status("none") == ("Available", "ok")
    assert _public_dependency_status("major") == (
        "Some features may be affected",
        "warning",
    )
    assert _public_dependency_status("unknown") == (
        "Status could not be checked",
        "info",
    )


def test_known_developer_copy_does_not_reach_user_surfaces():
    repo_root = Path(__file__).resolve().parents[1]
    surfaced_files = [
        repo_root / "main.py",
        repo_root / "auth" / "auth_manager.py",
        repo_root / "security" / "sanitise_context.py",
        repo_root / "ui" / "ui_components.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in surfaced_files)

    forbidden_fragments = (
        "st.caption(str(error))",
        "st.error(str(error))",
        "Cache status unavailable:",
        "Minimum Score Threshold:",
        "_Index:_",
        "_Namespace:_",
        "Error during search:",
        "Results below relevance threshold",
        "Regan has told me to say I don't know",
    )

    for fragment in forbidden_fragments:
        assert fragment not in source
