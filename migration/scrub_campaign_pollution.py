#!/usr/bin/env python3
"""
scrub_campaign_pollution.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Firestore maintenance script that scrubs contaminated data from active campaign
documents in the sideio-leads-v16 project.

Contamination patterns addressed:
  - bio:      Clears fallback-intent boilerplate strings.
  - keywords: Removes junk tokens (error traces, placeholders, test data).
  - location: Clears values that contain audience/persona language, error
              tokens, or exceed 100 characters.

Safety:
  * --dry-run (default) previews all changes without writing.
  * Pass --no-dry-run to commit changes to Firestore.
  * All writes use merge=True so untouched fields are never overwritten.
  * The script is idempotent — re-running it on already-clean data is a no-op.

Usage:
    # Preview only (safe default)
    python scrub_campaign_pollution.py

    # Actually write cleaned data
    python scrub_campaign_pollution.py --no-dry-run

    # Increase logging verbosity
    python scrub_campaign_pollution.py --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from google.cloud import firestore  # google-cloud-firestore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ID = "sideio-leads-v16"
COLLECTION = "campaigns"

# bio: exact phrases that indicate fallback-intent pollution
BIO_CONTAMINATION_PHRASES: list[str] = [
    "fallback intent processing required",
    # The check is case-insensitive, so this single entry covers mixed case.
]

# keywords: any keyword containing one of these substrings is junk
KEYWORD_JUNK_PATTERNS: list[str] = [
    "fallback intent",
    "error",
    "exception",
    "traceback",
    "internal server error",
    "timeout",
    "failed to",
    "null",
    "undefined",
    "none",
    "n/a",
    "child_campaign_override",
    "shadow_learner",
    "placeholder",
    "test_keyword",
    "sample_data",
]

# location: tokens whose presence signals contamination
LOCATION_JUNK_TOKENS: list[str] = [
    "interested",
    "customers",
    "vehicle",
    "users",
    "audience",
    "persona",
    "error",
    "exception",
    "fallback",
    "null",
]

# Maximum acceptable length for a location value
LOCATION_MAX_LENGTH = 100

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("scrub_campaign_pollution")


def configure_logging(verbose: bool = False) -> None:
    """Set up console logging with a clear format."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[%(levelname)s] %(message)s")
    )
    logger.setLevel(level)
    logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Field-level scrubbing helpers
# ---------------------------------------------------------------------------

def scrub_bio(value: Any) -> tuple[bool, str]:
    """Return (was_contaminated, cleaned_value) for the bio field.

    Contaminated when the value contains the fallback-intent boilerplate
    (case-insensitive check).
    """
    if not isinstance(value, str) or not value:
        return False, value

    lower = value.lower()
    for phrase in BIO_CONTAMINATION_PHRASES:
        if phrase in lower:
            return True, ""

    return False, value


def scrub_keywords(value: Any) -> tuple[bool, str]:
    """Return (was_contaminated, cleaned_value) for the keywords field.

    If the value is a string it is split on commas, each token is checked
    against the junk-pattern list (case-insensitive substring match), and
    surviving tokens are re-joined.
    """
    if not isinstance(value, str) or not value:
        return False, value

    original_tokens = [kw.strip() for kw in value.split(",")]
    clean_tokens: list[str] = []

    for token in original_tokens:
        token_lower = token.lower()
        # Keep the token only if it matches *none* of the junk patterns
        if not any(pattern in token_lower for pattern in KEYWORD_JUNK_PATTERNS):
            clean_tokens.append(token)

    cleaned = ", ".join(clean_tokens)
    was_contaminated = cleaned != value
    return was_contaminated, cleaned


def scrub_location(value: Any) -> tuple[bool, str]:
    """Return (was_contaminated, cleaned_value) for the location field.

    Contaminated when the value contains any junk token (case-insensitive)
    or exceeds LOCATION_MAX_LENGTH characters.
    """
    if not isinstance(value, str) or not value:
        return False, value

    lower = value.lower()

    # Check for junk tokens
    if any(token in lower for token in LOCATION_JUNK_TOKENS):
        return True, ""

    # Check for excessive length
    if len(value) > LOCATION_MAX_LENGTH:
        return True, ""

    return False, value


# ---------------------------------------------------------------------------
# Core scrubbing logic
# ---------------------------------------------------------------------------

def scrub_campaigns(dry_run: bool = True) -> None:
    """Query all active campaigns and scrub contaminated fields.

    Parameters
    ----------
    dry_run : bool
        When True (default) changes are logged but NOT written to Firestore.
    """
    # --- Connect to Firestore ---
    db = firestore.Client(project=PROJECT_ID)
    logger.info("Connected to Firestore project: %s", PROJECT_ID)
    logger.info("Mode: %s", "DRY-RUN (no writes)" if dry_run else "LIVE (writes enabled)")
    logger.info("-" * 70)

    # --- Query active campaigns ---
    query = db.collection(COLLECTION).where("status", "==", "active")
    docs = list(query.stream())  # materialise so we can count easily

    total_scanned = len(docs)
    total_contaminated = 0
    total_fields_cleaned = 0

    logger.info("Found %d active campaign(s) to scan.", total_scanned)

    for doc in docs:
        data = doc.to_dict() or {}
        campaign_id = doc.id
        tenant_id = data.get("tenant_id", "<unknown>")

        # Track per-document changes: field_name -> (before, after)
        changes: dict[str, tuple[Any, Any]] = {}

        # --- bio ---
        bio_val = data.get("bio", "")
        contaminated, cleaned = scrub_bio(bio_val)
        if contaminated:
            changes["bio"] = (bio_val, cleaned)

        # --- keywords ---
        kw_val = data.get("keywords", "")
        contaminated, cleaned = scrub_keywords(kw_val)
        if contaminated:
            changes["keywords"] = (kw_val, cleaned)

        # --- location ---
        loc_val = data.get("location", "")
        contaminated, cleaned = scrub_location(loc_val)
        if contaminated:
            changes["location"] = (loc_val, cleaned)

        # --- Apply / log changes ---
        if changes:
            total_contaminated += 1
            total_fields_cleaned += len(changes)

            logger.info(
                "Campaign %s (tenant=%s) — %d contaminated field(s):",
                campaign_id,
                tenant_id,
                len(changes),
            )
            update_payload: dict[str, Any] = {}
            for field_name, (before, after) in changes.items():
                # Truncate long before-values for readability
                display_before = (before[:120] + "…") if len(str(before)) > 120 else before
                logger.info("  • %s", field_name)
                logger.info("      BEFORE: %r", display_before)
                logger.info("      AFTER:  %r", after)
                update_payload[field_name] = after

            if not dry_run:
                # merge=True ensures we only touch the fields we're cleaning
                doc.reference.set(update_payload, merge=True)
                logger.info("    ✓ Written to Firestore.")
            else:
                logger.debug("    (dry-run — no write performed)")
        else:
            logger.debug(
                "Campaign %s (tenant=%s) — clean, no changes needed.",
                campaign_id,
                tenant_id,
            )

    # --- Summary ---
    logger.info("-" * 70)
    logger.info("SUMMARY")
    logger.info("  Total campaigns scanned:      %d", total_scanned)
    logger.info("  Total contaminated campaigns:  %d", total_contaminated)
    logger.info("  Total fields cleaned:          %d", total_fields_cleaned)
    if dry_run and total_contaminated > 0:
        logger.info(
            "  ⚠  Dry-run mode was ON — no data was written. "
            "Re-run with --no-dry-run to apply changes."
        )
    logger.info("-" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Scrub contaminated data (bio, keywords, location) from active "
            "campaign documents in Firestore."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scrub_campaign_pollution.py              # dry-run (safe preview)\n"
            "  python scrub_campaign_pollution.py --no-dry-run # commit changes\n"
            "  python scrub_campaign_pollution.py --verbose    # debug-level logs\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,  # gives --dry-run / --no-dry-run
        default=True,
        help="When set (default), only log changes without writing to Firestore. "
             "Use --no-dry-run to actually write.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable debug-level logging for more detail.",
    )
    return parser


def main() -> None:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)
    scrub_campaigns(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
