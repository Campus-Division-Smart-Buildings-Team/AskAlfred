from __future__ import annotations

import logging
import zipfile

import query_core.intent_classifier as intent_classifier_module
from core.failure_codes import FailureCode
from core.model_archive import initialise_local_model_archive
from core.telemetry import (
    COMPONENT_INTENT_CLASSIFIER,
    METRIC_FALLBACK_ACTIVATED,
    METRIC_SERVICE_DEGRADED,
    Readiness,
    ReadinessRegistry,
    Telemetry,
)


def _assert_pattern_only_degradation(
    readiness: ReadinessRegistry, telemetry: Telemetry
) -> None:
    assert readiness.get(COMPONENT_INTENT_CLASSIFIER) is Readiness.DEGRADED
    assert readiness.snapshot()[COMPONENT_INTENT_CLASSIFIER]["code"] == (
        FailureCode.STARTUP_ARCHIVE_INVALID.value
    )
    assert (
        telemetry.get(METRIC_FALLBACK_ACTIVATED, component=COMPONENT_INTENT_CLASSIFIER)
        == 1
    )
    assert (
        telemetry.get(
            METRIC_SERVICE_DEGRADED,
            component=COMPONENT_INTENT_CLASSIFIER,
            code=FailureCode.STARTUP_ARCHIVE_INVALID,
        )
        == 1
    )


def test_corrupt_model_archive_enters_pattern_only_mode(tmp_path, caplog):
    archive = tmp_path / "model.zip"
    archive.write_bytes(b"this is not a zip archive")
    model_dir = tmp_path / "model"
    readiness = ReadinessRegistry()
    telemetry = Telemetry()

    with caplog.at_level(logging.ERROR):
        enabled = initialise_local_model_archive(
            archive,
            model_dir,
            readiness=readiness,
            telemetry=telemetry,
        )

    assert enabled is False
    assert not model_dir.exists()
    assert "using pattern-only mode" in caplog.text
    assert "File is not a zip file" in caplog.text
    _assert_pattern_only_degradation(readiness, telemetry)


def test_path_traversing_model_archive_is_rejected(tmp_path, caplog):
    archive = tmp_path / "model.zip"
    with zipfile.ZipFile(archive, "w") as model_zip:
        model_zip.writestr("../escaped.txt", "must not be written")
        model_zip.writestr("config.json", "{}")

    model_dir = tmp_path / "model"
    readiness = ReadinessRegistry()
    telemetry = Telemetry()

    with caplog.at_level(logging.ERROR):
        enabled = initialise_local_model_archive(
            archive,
            model_dir,
            readiness=readiness,
            telemetry=telemetry,
        )

    assert enabled is False
    assert not model_dir.exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert "Unsafe path in archive" in caplog.text
    _assert_pattern_only_degradation(readiness, telemetry)


def test_valid_model_archive_is_atomically_extracted(tmp_path):
    archive = tmp_path / "model.zip"
    with zipfile.ZipFile(archive, "w") as model_zip:
        model_zip.writestr("config.json", "{}")

    model_dir = tmp_path / "model"

    assert initialise_local_model_archive(archive, model_dir) is True
    assert (model_dir / "config.json").read_text(encoding="utf-8") == "{}"


def test_pattern_only_classifier_does_not_initialise_model_runtime(monkeypatch):
    def fail_if_model_runtime_starts():
        raise AssertionError("model runtime must not start after archive rejection")

    monkeypatch.setattr(
        intent_classifier_module,
        "_get_encoder_ct2_runtime",
        fail_if_model_runtime_starts,
    )

    classifier = intent_classifier_module.NLPIntentClassifier(enable_model=False)

    assert classifier.enabled is False
    assert classifier.classify_intent("show maintenance requests").intent.value == (
        "maintenance"
    )
