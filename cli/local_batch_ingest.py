#!/usr/bin/env python3
"""
CLI entrypoint for AskAlfred local batch ingestion.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from config import NAMESPACE_MAPPINGS, BatchIngestConfig
from core.alfred_exceptions import (
    ConfigError,
    CriticalInconsistentError,
    ExternalServiceError,
    IngestError,
    UnexpectedError,
)
from ingest import (
    AclRemediationAction,
    IngestContext,
    ingest_local_directory_with_progress,
    reconcile_acl_vectors,
    validate_namespace_routing,
)
from ingest.fra_reconciliation import reconcile_fra_transactions
from ingest.registry_reconciliation import reconcile_registry_divergence
from security.file_operations_validator import (
    FileOperationSecurityError,
    validate_directory_safety,
)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Ingest local documents into Pinecone via OpenAI embeddings"
    )
    parser.add_argument("--path", help="Local directory path")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip documents that already exist in the index",
    )
    parser.add_argument(
        "--force-reindex",
        action="store_true",
        help="Force re-indexing of all documents (overrides skip-existing)",
    )
    parser.add_argument(
        "--io-workers",
        type=int,
        help="Number of IO workers for processing",
    )
    parser.add_argument(
        "--parse-workers",
        type=int,
        help="Number of parse workers for FRA extraction",
    )
    parser.add_argument(
        "--validate-routing",
        action="store_true",
        help="Run namespace routing validation tests",
    )
    parser.add_argument(
        "--export-events",
        action="store_true",
        help="Write building assignment events to JSONL file",
    )
    parser.add_argument(
        "--events-file",
        help="Path to JSONL export file for building assignment events",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress bar display",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + chunk only. Do NOT call OpenAI or Pinecone.",
    )
    parser.add_argument(
        "--upsert-strategy",
        choices=["worker", "inline"],
        help="Upsert strategy: background worker thread or inline.",
    )
    parser.add_argument(
        "--upsert-workers",
        type=int,
        help="Number of upsert worker threads (worker strategy only).",
    )
    parser.add_argument(
        "--reconcile-fra",
        nargs="?",
        const="*",
        metavar="TRANSACTION_ID",
        help="Reconcile one FRA transaction, or all open transactions if no ID is supplied.",
    )
    parser.add_argument(
        "--reconcile-registry",
        action="store_true",
        help="Replay vector-success/file-registry divergence records.",
    )
    parser.add_argument(
        "--reconcile-acl",
        choices=[action.value for action in AclRemediationAction],
        help="Audit ACL metadata, or explicitly quarantine non-conformant vectors.",
    )
    parser.add_argument(
        "--acl-namespace",
        action="append",
        help="Namespace to scan (repeatable); defaults to ingestion namespaces.",
    )
    parser.add_argument(
        "--acl-threshold",
        type=float,
        help="Required ACL conformance ratio (defaults to ACL_CONFORMANCE_THRESHOLD).",
    )
    parser.add_argument(
        "--acl-report",
        default="logs/acl_reconciliation.json",
        help="Privacy-safe ACL reconciliation report path.",
    )

    return parser.parse_args()


def configure_logging() -> Path:
    """Configure console and timestamped file logging for an ingest run."""
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"ingest_{datetime.now():%Y%m%d_%H%M%S}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )
    return log_path


def main() -> int:
    log_path = configure_logging()
    load_dotenv()
    args = parse_args()
    logging.info("Writing ingest log to %s", log_path)

    try:
        config = BatchIngestConfig.from_env()

        if args.path:
            # Validate path argument for security
            try:
                validated_path = validate_directory_safety(args.path)
                config.local_path = str(validated_path)
                logging.info("Validated ingest directory: %s", validated_path)
            except FileOperationSecurityError as e:
                logging.error("Invalid path argument: %s", e)
                return 2
        if args.io_workers:
            config.max_io_workers = args.io_workers
        if args.parse_workers:
            config.max_parse_workers = args.parse_workers
        if args.force_reindex:
            config.skip_existing = False
        elif args.skip_existing:
            config.skip_existing = True
        if args.export_events:
            config.export_events = True
        if args.events_file:
            config.export_events_file = args.events_file
        if args.dry_run:
            config.dry_run = True
            config.skip_existing = False
        if args.upsert_strategy:
            config.upsert_strategy = args.upsert_strategy
        if args.upsert_workers is not None:
            config.upsert_workers = args.upsert_workers

        config.validate()
        logging.info(
            "Ingest config: upsert_workers=%d, fra_supersession_single_threaded=%s",
            config.upsert_workers,
            config.fra_supersession_single_threaded,
        )

    except ConfigError as error:
        logging.error("Configuration error: %s", error)
        return 5
    except UnexpectedError as error:
        logging.error("Configuration error: %s", error)
        return 5

    if args.validate_routing:
        for doc_type, expected_namespace in NAMESPACE_MAPPINGS.items():
            valid, reason = validate_namespace_routing(doc_type, expected_namespace)
            if not valid:
                logging.error(
                    "Routing validation failed for document type %s: %s",
                    doc_type,
                    reason,
                )
                return 2
        logging.info("Namespace routing validation passed.")
        return 0

    ctx = IngestContext(config)

    try:
        if args.reconcile_fra is not None:
            report = reconcile_fra_transactions(
                ctx,
                transaction_id=(
                    None if args.reconcile_fra == "*" else args.reconcile_fra
                ),
            )
            logging.info(
                "FRA reconciliation status=%s examined=%d reconciled=%d remaining=%d",
                report.status.value,
                report.examined,
                report.reconciled,
                report.remaining,
            )
            return report.exit_code

        if args.reconcile_registry:
            report = reconcile_registry_divergence(ctx)
            logging.info(
                "Registry reconciliation status=%s examined=%d reconciled=%d remaining=%d",
                report.status.value,
                report.examined,
                report.reconciled,
                report.remaining,
            )
            return report.exit_code

        if args.reconcile_acl:
            acl_kwargs = {
                "action": args.reconcile_acl,
                "namespaces": args.acl_namespace,
                "report_path": args.acl_report,
            }
            if args.acl_threshold is not None:
                acl_kwargs["threshold"] = args.acl_threshold
            report = reconcile_acl_vectors(ctx, **acl_kwargs)
            logging.info(
                "ACL reconciliation status=%s action=%s scanned=%d "
                "nonconformant=%d remediated=%d failed=%d ratio=%.4f "
                "threshold=%.4f",
                report.status.value,
                report.action.value,
                report.scanned,
                report.nonconformant,
                report.remediated,
                report.failed,
                report.conformance_ratio,
                report.threshold,
            )
            return report.exit_code

        report = ingest_local_directory_with_progress(
            ctx, use_progress_bar=not args.no_progress
        )
        logging.info(
            "Ingestion terminal status=%s exit_code=%d",
            report.status.value,
            report.exit_code,
        )
        return report.exit_code
    except KeyboardInterrupt:
        ctx.logger.warning("Ingestion interrupted by user. Cleaning up...")
        ctx.logger.info("No cache to persist on shutdown.")
        return 5
    except CriticalInconsistentError as error:
        ctx.logger.critical("Ingestion requires reconciliation: %s", error)
        return 10
    except ExternalServiceError as error:
        ctx.logger.error("Ingestion dependency unavailable: %s", error)
        return 4
    except UnexpectedError as error:
        ctx.logger.error("Ingestion failed: %s", error, exc_info=True)
        return 5
    except IngestError as error:
        ctx.logger.error("Ingestion failed: %s", error, exc_info=True)
        return 5
    except Exception as error:  # pylint: disable=broad-except
        ctx.logger.error("Ingestion failed unexpectedly: %s", error, exc_info=True)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
