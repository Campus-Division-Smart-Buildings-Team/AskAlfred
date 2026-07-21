from types import SimpleNamespace
from unittest.mock import Mock

from config import BatchIngestConfig
from ingest.document_content import embed_texts_batch
from ingest.document_processor import Vectoriser
from ingest.transaction import FileCompletionTracker, _record_ingested_files
from ingest.utils import validate_with_truncation
from interfaces.embedder import EmbeddingsResult, OpenAIEmbedder
from interfaces.ingest_file_registry import RedisIngestFileRegistry


def _vector(vector_id: str, namespace: str = "docs") -> dict:
    return {
        "id": vector_id,
        "values": [0.0],
        "metadata": {
            "source_path": "/data",
            "source": "source.pdf",
            "key": "derived document",
            "content_hash": "abc",
        },
        "namespace": namespace,
        "_processing_token": "token",
    }


def test_file_completion_waits_for_every_registered_vector():
    tracker = FileCompletionTracker()
    vectors = [_vector("file:a:0"), _vector("file:a:1"), _vector("file:a:2")]
    tracker.register(vectors)

    assert tracker.record_success(vectors[:2]) == []
    completed = tracker.record_success(vectors[2:])

    assert len(completed) == 1
    assert completed[0][0]["metadata"]["source"] == "source.pdf"
    assert tracker.record_success(vectors) == []


def test_file_completion_failure_cannot_be_overwritten_by_success():
    tracker = FileCompletionTracker()
    vectors = [_vector("file:a:0"), _vector("file:a:1")]
    tracker.register(vectors)

    tracker.record_failure(vectors[:1])

    assert tracker.record_success(vectors) == []


def test_registry_success_is_written_once_after_whole_file_completes():
    tracker = FileCompletionTracker()
    registry = Mock()
    ctx = SimpleNamespace(
        config=SimpleNamespace(dry_run=False),
        completion_tracker=tracker,
        file_registry=registry,
    )
    vectors = [_vector("file:a:0"), _vector("file:a:1")]
    tracker.register(vectors)

    _record_ingested_files(ctx, vectors[:1], status="success")
    registry.upsert_with_token.assert_not_called()

    _record_ingested_files(ctx, vectors[1:], status="success")

    registry.upsert_with_token.assert_called_once()
    record = registry.upsert_with_token.call_args.args[0]
    assert record.source_key == "source.pdf"
    assert record.status == "success"


def test_vectoriser_produces_both_fra_and_whole_document_artefacts():
    """A successful FRA risk-item extraction must not suppress whole-document
    indexing: the file yields both fra_risk_items (risk-item summaries) and
    fire_risk_assessments (whole-document) vectors."""
    processor = Mock()
    processor.handle_dry_run.return_value = False

    def add_fra_vector(**kwargs):
        kwargs["vectors_to_upsert"].append(_vector("file:risk:0", "fra_risk_items"))
        return True

    processor.maybe_extract_fra_vectors.side_effect = add_fra_vector
    processor.build_vectors_from_docs.return_value = [
        _vector("file:doc:0", "fire_risk_assessments")
    ]
    vectoriser = Vectoriser(processor)

    vectors = vectoriser.vectorise(
        key="fra.pdf",
        extension="pdf",
        file_id="file",
        content_hash="abc",
        processing_token="token",
        start_time=0.0,
        docs=[],
        text_sample="risk",
        building="Building",
        is_fra_candidate=True,
        precomputed_chunks=None,
    )

    # Both artefacts are produced; the whole-document build is no longer skipped.
    processor.build_vectors_from_docs.assert_called_once()
    assert [vector["id"] for vector in vectors] == ["file:risk:0", "file:doc:0"]
    assert {vector["namespace"] for vector in vectors} == {
        "fra_risk_items",
        "fire_risk_assessments",
    }


def test_oversized_metadata_is_truncated_before_size_validation():
    class Encoder:
        @staticmethod
        def encode(text):
            return list(text)

        @staticmethod
        def decode(tokens):
            return "".join(tokens)

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            max_metadata_size=1_000,
            max_metadata_text_tokens=50,
        ),
        encoder=Encoder(),
    )
    metadata = {
        "source_path": "/data",
        "key": "doc",
        "source": "doc.txt",
        "document_type": "unknown",
        "text": "x" * 5_000,
        "tenant_id": "tenant-123",
        "access_level": "pilot_internal",
        "allowed_roles": ["base_view"],
    }

    valid, reason = validate_with_truncation(ctx, metadata)

    assert valid, reason
    assert len(metadata["text"]) == 50


def test_embed_batch_uses_configured_batch_size():
    embedder = Mock()
    embedder.embed_texts.return_value = EmbeddingsResult({}, {})
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            embed_model="text-embedding-3-small",
            openai_timeout=60.0,
            embed_batch=96,
            dry_run=False,
        ),
        embedder=embedder,
        stats=Mock(),
    )

    embed_texts_batch(ctx, ["one", "two"])

    assert embedder.embed_texts.call_args.kwargs["max_batch"] == 96


def test_fra_serialisation_keeps_non_fra_upsert_workers_available(tmp_path):
    config = BatchIngestConfig(
        pinecone_api_key="",
        openai_api_key="",
        redis_host="",
        redis_port=0,
        redis_username="",
        redis_password="",
        local_path=str(tmp_path),
        cache_dir=str(tmp_path / "cache"),
        dry_run=True,
        upsert_workers=4,
        fra_supersession_single_threaded=True,
    )

    config.validate()

    assert config.upsert_workers == 4


def test_registry_state_update_uses_one_atomic_script_call():
    scripts = []

    class Script:
        def __init__(self):
            self.calls = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            return 1

    class Client:
        def register_script(self, _source):
            script = Script()
            scripts.append(script)
            return script

    registry = RedisIngestFileRegistry(Client())

    registry.mark_state(
        file_id="file",
        processing_token="token",
        status="success",
        source_path="/data",
        source_key="source.pdf",
        content_hash="abc",
        namespaces=("docs",),
    )

    assert len(scripts) == 3
    assert len(scripts[-1].calls) == 1


def test_embed_batch_size_recovers_after_single_item_failure():
    """A single un-embeddable item must not collapse the rest of the run to
    batch size 1; the batch size resets once the bad item is skipped."""
    calls: list[int] = []

    class _Embeddings:
        @staticmethod
        def create(*, model, input, timeout=None):  # noqa: A002 - OpenAI kwarg
            calls.append(len(input))
            if "BAD" in input:
                # Non-OpenAI error -> embedder treats it as a non-fatal,
                # no-retry failure (adaptive batch reduction), no sleeps.
                raise ValueError("poison item")
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[float(len(text))]) for text in input]
            )

    class _Client:
        embeddings = _Embeddings()

    embedder = OpenAIEmbedder(client=_Client())
    texts = ["ok0", "ok1", "ok2", "BAD", "ok4", "ok5", "ok6", "ok7"]

    result = embedder.embed_texts(texts, model="m", max_batch=4)

    # Only the poison item fails; everything else is embedded.
    assert set(result.errors_by_index) == {3}
    assert set(result.embeddings_by_index) == {0, 1, 2, 4, 5, 6, 7}
    # After index 3 is skipped at size 1, the remaining four items are embedded
    # in one full-size batch. Without the reset they would trickle out at size 1.
    assert calls[-1] == 4


def test_embed_response_size_mismatch_retries_once_and_recovers():
    """VECTOR-04: a short provider response is retried once; a healthy retry
    recovers every embedding without recording a contract failure."""
    attempts = {"count": 0}

    class _Embeddings:
        @staticmethod
        def create(*, model, input, timeout=None):  # noqa: A002 - OpenAI kwarg
            attempts["count"] += 1
            if attempts["count"] == 1:
                # First response drops the final embedding (count mismatch).
                data = [SimpleNamespace(embedding=[0.0]) for _ in input[:-1]]
            else:
                data = [SimpleNamespace(embedding=[0.0]) for _ in input]
            return SimpleNamespace(data=data)

    class _Client:
        embeddings = _Embeddings()

    embedder = OpenAIEmbedder(client=_Client())

    result = embedder.embed_texts(["a", "b", "c"], model="m", max_batch=3)

    assert attempts["count"] == 2  # original call + exactly one safe retry
    assert set(result.embeddings_by_index) == {0, 1, 2}
    assert result.errors_by_index == {}
    assert result.response_mismatch_retries == 1
    assert result.response_mismatch_batches == 0


def test_embed_response_size_mismatch_persists_after_single_retry():
    """A mismatch that survives the one safe retry is recorded per item and
    flagged so the ingestion boundary can alert; it is not retried endlessly."""
    attempts = {"count": 0}

    class _Embeddings:
        @staticmethod
        def create(*, model, input, timeout=None):  # noqa: A002 - OpenAI kwarg
            attempts["count"] += 1
            # Always return one fewer embedding than requested.
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.0]) for _ in input[:-1]]
            )

    class _Client:
        embeddings = _Embeddings()

    embedder = OpenAIEmbedder(client=_Client())

    result = embedder.embed_texts(["a", "b", "c"], model="m", max_batch=3)

    assert attempts["count"] == 2  # one retry only, not an unbounded loop
    assert result.embeddings_by_index == {}
    assert set(result.errors_by_index.values()) == {"response_size_mismatch"}
    assert set(result.errors_by_index) == {0, 1, 2}
    assert result.response_mismatch_retries == 1
    assert result.response_mismatch_batches == 1


def test_embed_texts_batch_alerts_on_persistent_response_mismatch():
    """A persistent response-size mismatch surfaces a run stat, integrity
    telemetry, and an operator event (VECTOR-04)."""
    from core.telemetry import get_telemetry

    get_telemetry().reset()
    events: list[dict] = []

    embedder = Mock()
    embedder.embed_texts.return_value = EmbeddingsResult(
        embeddings_by_index={},
        errors_by_index={0: "response_size_mismatch", 1: "response_size_mismatch"},
        response_mismatch_retries=1,
        response_mismatch_batches=1,
    )
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            embed_model="text-embedding-3-small",
            openai_timeout=60.0,
            embed_batch=96,
            dry_run=False,
        ),
        embedder=embedder,
        stats=Mock(),
        event_sink=SimpleNamespace(emit_event=events.append),
        logger=Mock(),
    )

    embed_texts_batch(ctx, ["one", "two"])

    ctx.stats.increment.assert_any_call("embed_response_mismatch_total", 1)
    ctx.stats.increment.assert_any_call("embed_response_mismatch_retries_total", 1)
    assert (
        get_telemetry().get(
            "ingest_integrity_total", event="embedding", state="response_mismatch"
        )
        == 1
    )
    assert len(events) == 1
    assert events[0]["event_type"] == "embed_response_size_mismatch"
    assert events[0]["affected_batches"] == 1
    ctx.logger.error.assert_called()


def test_fra_upsert_verification_retries_before_failing(monkeypatch):
    """Post-upsert verification must be given several fetch attempts to absorb
    read-after-write lag, not fail the whole batch (and force a re-upsert) on
    the first miss."""
    from ingest import transaction

    captured: dict = {}

    def _fake_verify(ctx, vectors, attempts=1):
        captured["attempts"] = attempts
        return []  # nothing missing

    monkeypatch.setattr(transaction, "upsert_vectors", lambda ctx, vectors: None)
    monkeypatch.setattr(transaction, "_verify_fra_vectors_present", _fake_verify)
    monkeypatch.setattr(transaction, "_record_ingested_files", lambda *a, **k: None)

    ctx = SimpleNamespace(
        config=SimpleNamespace(dry_run=False),
        stats=Mock(),
        logger=Mock(),
        upsert_stop_event=None,
    )
    # FRA vector with no assessment date -> simple (non-supersession) path.
    vectors = [
        {
            "id": "file:risk:0",
            "values": [0.0],
            "metadata": {"canonical_building_name": "Some Building"},
            "namespace": "fra_risk_items",
        }
    ]

    transaction.upsert_vectors_atomic(ctx, vectors)

    assert captured["attempts"] > 1
