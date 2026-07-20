#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI components and styling for the AskAlfred Streamlit app.
Enhanced with building cache status display.
"""

import logging
import re
from datetime import datetime, timezone
from html import escape

import requests
import streamlit as st

from auth.auth_manager import current_user_is_operator
from building import get_cache_status
from config import (
    DEFAULT_NAMESPACE,
    ENABLE_SERVICE_STATUS,
    MIN_SCORE_THRESHOLD,
    TARGET_INDEXES,
    UI_SNIPPET_MAX_CHARS,
    UI_TOP_K_DEFAULT,
    UI_TOP_K_MAX,
    UI_TOP_K_MIN,
)
from core.clients import get_redis
from security.sanitise_context import (
    display_safe_low_score_warning,
    display_safe_publication_date_info,
)

BUILDING_DIRECTORY_LIMITED_MESSAGE = (
    "Building-name recognition is temporarily limited. "
    "For the best results, use the full building name in your question."
)


def get_source_label(result: dict, number: int) -> str:
    """Return a user-facing source name without exposing storage details."""
    metadata = result.get("metadata", {}) or {}
    candidates = (
        result.get("title"),
        metadata.get("title"),
        metadata.get("document_title"),
        metadata.get("file_name"),
        result.get("key"),
        metadata.get("key"),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        label = str(candidate).strip()
        if not label:
            continue
        # Storage keys may contain a full Windows or POSIX path. A filename is
        # useful to users; the internal path is not.
        label = re.split(r"[\\/]", label)[-1].strip()
        if label and label.lower() not in {"unknown", "__default__", "?"}:
            return label
    return f"Source {number}"


def setup_page_config():
    """Set up Streamlit page configuration."""
    st.set_page_config(
        page_title="University of Bristol | AskAlfred",
        page_icon="https://www.bristol.ac.uk/assets/responsive-web-project/2.6.9/images/logos/uob-logo.svg",
        layout="wide",
    )


def render_custom_css():
    """Render custom CSS styles."""
    st.markdown(
        """
        <style>
          .uob-header {
            position: relative;
            background: rgba(227, 230, 229, 0.7);
            padding: 1.25rem 1.5rem;
            border-radius: 12px;
            display: flex;
            align-items: center;
            gap: 14px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            margin-bottom: 20px;
          }
          @media (prefers-color-scheme: light) {
            .uob-header { background: rgba(171, 31, 45, 0.4); border: 1px solid rgba(0, 0, 0, 0.1); }
          }
          @media (prefers-color-scheme: dark) {
            .uob-header h1 { color: #000 !important; }
          }
          .uob-header img { height: 70px; z-index: 2;}
          .uob-header h1 {
            position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
            margin: 0; font-size: 2rem;
          }
          .publication-date {
            background-color: rgba(255, 193, 7, 0.1);
            border-left: 4px solid #ffc107;
            padding: 8px 12px;
            margin: 8px 0;
            border-radius: 4px;
            font-size: 0.9em;
          }
          .top-result-highlight {
            background-color: rgba(40, 167, 69, 0.1);
            border-left: 4px solid #28a745;
            padding: 8px 12px;
            margin: 8px 0;
            border-radius: 4px;
            font-size: 0.9em;
          }
          .low-score-warning {
            background-color: rgba(220, 53, 69, 0.1);
            border-left: 4px solid #dc3545;
            padding: 8px 12px;
            margin: 8px 0;
            border-radius: 4px;
            font-size: 0.9em;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header():
    """Render the application header."""
    st.markdown(
        """
        <div class="uob-header">
          <picture>
            <source srcset="https://www.bristol.ac.uk/assets/responsive-web-project/2.6.9/images/logos/uob-logo.svg" media="(prefers-color-scheme: light)"/>
            <source srcset="https://www.bristol.ac.uk/assets/responsive-web-project/2.6.9/images/logos/uob-logo.svg"/>
            <img src="https://www.bristol.ac.uk/assets/responsive-web-project/2.6.9/images/logos/uob-logo.svg" alt="University of Bristol"/>
          </picture>
          <h1>🦍 AskAlfred</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_tabs():
    """Render main content tabs."""
    tab1, tab2, tab3 = st.tabs(["Welcome", "Info", "Resources"])

    with tab1:
        st.write("""
            #### Hi, I'm Alfred! 👋
            You can ask me questions about the following topics: 
            - 🏢 Building Management Systems (BMS)  
            - 🔥 Fire Risk Assessments (FRAs) and
            - 🛠️ Maintenance requests and jobs across our estate.

            Type your question in the chat below, and I'll look through the available information for an answer.
            
            **💡 Tip:** You can use building names or their abbreviations (e.g., "BDFI" for "65 Avon Street")
            """)

    with tab2:
        st.write("""
            #### ⚠️ Disclaimer
            This app is experimental and should not be used as the sole basis for decisions.
            Alfred may be unable to answer when the available information does not match your question closely enough. In that case, try adding a building name, document type, or date.
            
            #### 🏢 Building Name Recognition
            Alfred can recognise building names and common abbreviations, including:
            - Official building names (e.g., "Senate House", "1-9 Old Park Hill")
            - Alternative names and abbreviations (e.g., "BDFI" for "65 Avon Street", "SHB" for "Senate House Building")
            - Common variations and aliases
            
            When you mention a building in your query, Alfred will automatically:
            - Detect the building name or abbreviation
            - Search specifically for documents related to that building
            - Prioritise results from the correct building
            - Show you the building it detected in the results
            """)

    with tab3:
        st.markdown("#### 💡 Example queries")
        col1, col2 = st.columns([2, 3])
        with col1:
            st.markdown("""
                **FRA topics:**
                - How many staff or visitors can Senate House accommodate?
                - How many floors does Augustines Courtyard have?
                - List the fire risks at Old Park Hill
                
                **Building abbreviations:**
                - Where is DHB?
                - Tell me about BDFI
                - What are the maintenance jobs at DEFRA?

                """)
        with col2:
            st.markdown("""
                **BMS topics:**
                - How does the frost protection sequence operate in the Senate House BMS?
                - How do the AHUs in Indoor Sports Hall behave?
                - How does the Mitsubishi AC controller integrate with the Trend IQ4 BMS?

                **Other queries:**
                - Which buildings are derelict?
                - Which buildings have fras?
                - How many maintenance requests have been raised at senate house?
                
                """)


def render_sidebar():
    """Render user settings and material availability notices."""
    with st.sidebar:
        st.header("Settings")
        try:
            cache_status = get_cache_status()
            if not cache_status["populated"]:
                st.warning(BUILDING_DIRECTORY_LIMITED_MESSAGE)
        except Exception:  # pylint: disable=broad-except
            logging.warning("Building directory status check failed", exc_info=True)
            st.warning(BUILDING_DIRECTORY_LIMITED_MESSAGE)

        st.markdown("---")

        top_k = st.slider(
            "Results per query",
            min_value=UI_TOP_K_MIN,
            max_value=UI_TOP_K_MAX,
            value=UI_TOP_K_DEFAULT,
            step=1,
            help="Number of results to return per query",
        )

        if ENABLE_SERVICE_STATUS:
            st.markdown("---")
            render_service_status()
            st.markdown("---")

        render_operator_diagnostics()

        if st.button("Clear Chat History"):
            st.session_state.messages = []
            st.session_state.last_results = []
            st.rerun()

        # Footer with accessibility statement
        st.markdown(
            """
        ---
        <footer role="contentinfo" style="margin-top: 2rem; padding: 1rem; background-color: rgba(0,0,0,0.05); border-radius: 8px;">
            <small>
            <strong>Accessibility:</strong> This application follows WCAG 2.2 AA guidelines. 
            If you encounter any accessibility issues, please contact the <strong>Smart Buildings Data Team</strong>.<br>
            <strong>University of Bristol</strong> | Experimental Research Application
            </small>
        </footer>
        """,
            unsafe_allow_html=True,
        )

    return top_k


def render_operator_diagnostics() -> None:
    """Render internal diagnostics, gated on an Entra operator app role.

    Fails closed: non-operator (including anonymous) sessions see nothing. This
    is where retrieval configuration, cache internals, and stats live now that
    they are no longer shown on normal user surfaces (plan item 6).
    """
    if not current_user_is_operator():
        return

    with st.expander("Operator diagnostics", expanded=False):
        st.caption("Visible to operator roles only. Not shown to standard users.")

        st.markdown("**Retrieval configuration**")
        st.json(
            {
                "indexes": list(TARGET_INDEXES),
                "namespace": DEFAULT_NAMESPACE or "default",
                "min_score_threshold": MIN_SCORE_THRESHOLD,
            }
        )

        st.markdown("**Building directory cache**")
        try:
            cache_status = get_cache_status()
        except Exception:  # pylint: disable=broad-except
            logging.warning("Operator cache status lookup failed", exc_info=True)
            cache_status = {}
        st.json(
            {
                "populated": cache_status.get("populated"),
                "canonical_names": cache_status.get("canonical_names"),
                "aliases": cache_status.get("aliases"),
                "indexes_with_buildings": cache_status.get("indexes_with_buildings"),
            }
        )

        manager = st.session_state.get("manager")
        if manager is not None and hasattr(manager, "get_statistics"):
            st.markdown("**Query manager stats**")
            try:
                st.json(manager.get_statistics())
            except Exception:  # pylint: disable=broad-except
                logging.warning("Operator stats lookup failed", exc_info=True)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_statuspage_status(url: str) -> dict[str, str]:
    """Fetch status data from a Statuspage status.json endpoint."""
    response = requests.get(url, timeout=8)
    response.raise_for_status()
    data = response.json()
    status = data.get("status", {})
    return {"indicator": str(status.get("indicator", "unknown"))}


@st.cache_data(ttl=60, show_spinner=False)
def get_redis_status() -> tuple[str, str]:
    """Return a user-facing safeguards status without deployment details."""
    try:
        redis_client = get_redis()
        if redis_client.ping():
            return "Available", "ok"
        return "Running in local mode", "warning"
    except Exception:  # pylint: disable=broad-except
        logging.warning("Redis status check failed", exc_info=True)
        return "Running in local mode", "warning"


def _public_dependency_status(indicator: str) -> tuple[str, str]:
    """Translate provider status into impact-focused, provider-neutral copy."""
    if indicator == "none":
        return "Available", "ok"
    if indicator in {"minor", "major"}:
        return "Some features may be affected", "warning"
    if indicator == "critical":
        return "Temporarily unavailable", "error"
    return "Status could not be checked", "info"


def render_status_line(label: str, status_text: str, severity: str):
    """Render a single status line with consistent styling."""
    if severity == "ok":
        st.success(f"{label}: {status_text}")
    elif severity == "warning":
        st.warning(f"{label}: {status_text}")
    elif severity == "error":
        st.error(f"{label}: {status_text}")
    else:
        st.info(f"{label}: {status_text}")


def render_service_status():
    """Render external service availability widget in the sidebar."""
    st.subheader("Service Status")

    if st.button("Refresh Service Status"):
        fetch_statuspage_status.clear()
        get_redis_status.clear()
        st.session_state.pop("service_status_snapshot", None)

    status_endpoints = (
        ("Answer service", "https://status.openai.com/api/v2/status.json"),
        ("Search service", "https://status.pinecone.io/api/v2/status.json"),
    )

    now_dt = datetime.now(timezone.utc)
    now_label = now_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    snapshot: dict[str, tuple[str, str]] = {}

    for label, url in status_endpoints:
        try:
            status = fetch_statuspage_status(url)
            indicator = status.get("indicator", "unknown")
            status_text, severity = _public_dependency_status(indicator)
        except Exception:  # pylint: disable=broad-except
            logging.warning("%s status check failed", label, exc_info=True)
            status_text, severity = "Status could not be checked", "info"
        render_status_line(label, status_text, severity)
        snapshot[label] = (status_text, severity)

    redis_status, redis_severity = get_redis_status()
    render_status_line("Request safeguards", redis_status, redis_severity)
    snapshot["Request safeguards"] = (redis_status, redis_severity)

    st.caption(f"Last updated: {now_label}")

    if "service_status_history" not in st.session_state:
        st.session_state.service_status_history = []

    current_snapshot = {"time_label": now_label, "statuses": snapshot}
    last_snapshot = st.session_state.get("service_status_snapshot")
    if last_snapshot != current_snapshot:
        st.session_state.service_status_history.insert(0, current_snapshot)
        st.session_state.service_status_history = (
            st.session_state.service_status_history[:3]
        )
        st.session_state.service_status_snapshot = current_snapshot

    with st.expander("Status History"):
        history = st.session_state.service_status_history
        if not history:
            st.caption("No history yet.")
        service_order = ["Answer service", "Search service", "Request safeguards"]
        for item in history:
            item_label = item.get("time_label", "Unknown time")
            st.markdown(f"**{item_label}**")
            cols = st.columns(len(service_order))
            for idx, svc in enumerate(service_order):
                _, sev = item["statuses"].get(svc, ("", "info"))
                if sev == "ok":
                    color = "#28a745"
                    bg = "rgba(40, 167, 69, 0.15)"
                elif sev == "warning":
                    color = "#ffc107"
                    bg = "rgba(255, 193, 7, 0.15)"
                elif sev == "error":
                    color = "#dc3545"
                    bg = "rgba(220, 53, 69, 0.15)"
                else:
                    color = "#6c757d"
                    bg = "rgba(108, 117, 125, 0.15)"

                with cols[idx]:
                    st.markdown(
                        f"""
                        <div style="border:1px solid {color}; color:{color};
                                    background:{bg}; padding:6px 8px;
                                    border-radius:8px; text-align:center;
                                    font-weight:600; font-size:0.85rem;">
                            {svc}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )


def display_search_results(results):
    """Display search results in an expandable section."""
    if not results:
        return

    with st.expander(f"📚 Search Results ({len(results)} found)", expanded=False):
        for i, result in enumerate(results, 1):
            # Highlight the top result
            if i == 1:
                st.markdown(
                    '<div class="top-result-highlight">🥇 <strong>TOP RESULT</strong></div>',
                    unsafe_allow_html=True,
                )

            source_label = get_source_label(result, i)
            st.markdown(f"**{i}. {escape(source_label)}**")

            # Show building name if available
            building_name = result.get("building_name", "")
            if building_name:
                st.caption(f"🏢 Building: {building_name}")

            snippet = result.get("text") or "_Preview unavailable._"
            st.write(
                snippet[:UI_SNIPPET_MAX_CHARS] + "..."
                if len(snippet) > UI_SNIPPET_MAX_CHARS
                else snippet
            )
            if i < len(results):
                st.markdown("---")


def initialise_chat_history():
    """Initialise chat history if not present."""
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Hello! I'm Alfred 🦍, your helpful assistant at the University of Bristol. I can help you find information about BMS description of operations documents, FRAs and maintenance requests and jobs across the UoB estate. What would you like to know?",
            }
        ]
    # # Add processing flag
    # if "processing_query" not in st.session_state:
    #     st.session_state.processing_query = False


def display_chat_history():
    """Display all chat messages from history safely."""
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            # Safely render message content with HTML escaping
            safe_content = escape(message["content"])
            st.markdown(safe_content, unsafe_allow_html=False)

            # Display publication date info if it exists
            if "publication_date_info" in message and message["publication_date_info"]:
                display_safe_publication_date_info(message["publication_date_info"])

            # Display low score warning if applicable
            if message.get("score_too_low", False):
                display_safe_low_score_warning()

            render_citation_legend(
                message.get("content", ""),
                message.get("results", []),
            )

            # Display search results if they exist
            if "results" in message:
                display_search_results(message["results"])


def render_citation_legend(answer: str, results: list[dict]) -> None:
    """Render a simple legend for inline [S1]-style answer citations."""
    if not answer or not results:
        return

    citation_numbers = []
    seen = set()
    for match in re.findall(r"\[S(\d+)\]", answer):
        number = int(match)
        if number not in seen:
            citation_numbers.append(number)
            seen.add(number)

    if not citation_numbers:
        return

    st.markdown("**Sources cited**")
    for number in citation_numbers:
        if number < 1 or number > len(results):
            continue
        result = results[number - 1]
        source_label = get_source_label(result, number)
        st.caption(f"[S{number}] {escape(source_label)}")
