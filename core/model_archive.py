"""Controlled startup handling for the optional local intent model archive."""

from __future__ import annotations

import logging
import tempfile
import zipfile
from pathlib import Path

from core.failure_codes import FailureCode
from core.telemetry import (
    COMPONENT_INTENT_CLASSIFIER,
    ReadinessRegistry,
    Telemetry,
    get_readiness,
    get_telemetry,
)
from security.log_sanitiser import sanitise_error

LOGGER = logging.getLogger(__name__)


class UnsafeArchivePathError(ValueError):
    """Raised when an archive member would be written outside its destination."""


def _validated_extract(zip_path: Path, destination: Path) -> None:
    """Validate every ZIP member and extract into an empty destination."""
    destination_root = destination.resolve()
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise UnsafeArchivePathError(
                    f"Unsafe path in archive {zip_path}: {member.filename!r}"
                )

        corrupt_member = archive.testzip()
        if corrupt_member is not None:
            raise zipfile.BadZipFile(
                f"CRC validation failed for archive member {corrupt_member!r}"
            )

        archive.extractall(destination)


def initialise_local_model_archive(
    zip_path: Path,
    model_dir: Path,
    *,
    readiness: ReadinessRegistry | None = None,
    telemetry: Telemetry | None = None,
) -> bool:
    """Prepare a local intent model without allowing archive failure to escape.

    ``True`` means normal classifier initialisation may continue. A missing
    archive is not an error because the classifier retains its existing model
    lookup behaviour. ``False`` means startup must construct the classifier in
    pattern-only mode.
    """
    if model_dir.exists() or not zip_path.exists():
        return True

    readiness = readiness or get_readiness()
    telemetry = telemetry or get_telemetry()

    try:
        model_dir.parent.mkdir(parents=True, exist_ok=True)
        # Extract to a sibling staging directory. A corrupt archive therefore
        # cannot leave a partial model directory that looks valid next startup.
        with tempfile.TemporaryDirectory(
            prefix=f".{model_dir.name}-", dir=model_dir.parent
        ) as staging_name:
            staging_dir = Path(staging_name)
            _validated_extract(zip_path, staging_dir)
            staging_dir.replace(model_dir)
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.error(
            "Local intent model archive rejected; using pattern-only mode: %s",
            sanitise_error(exc),
            exc_info=True,
        )
        readiness.mark_degraded(
            COMPONENT_INTENT_CLASSIFIER, FailureCode.STARTUP_ARCHIVE_INVALID
        )
        telemetry.record_fallback(COMPONENT_INTENT_CLASSIFIER)
        telemetry.record_service_degraded(
            COMPONENT_INTENT_CLASSIFIER, FailureCode.STARTUP_ARCHIVE_INVALID
        )
        return False

    LOGGER.info("Local intent model archive extracted successfully")
    return True


__all__ = [
    "UnsafeArchivePathError",
    "initialise_local_model_archive",
]
