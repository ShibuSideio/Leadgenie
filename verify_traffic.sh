#!/usr/bin/env bash
# =============================================================================
# LeadGenie — Production Traffic Verification Script
# Version: V25.2.3 | 2026-07-03
#
# PURPOSE:
#   Ensures 100% traffic is routed to the LATEST revision of every Cloud Run
#   service. Detects stale revisions still serving traffic.
#
# USAGE:
#   chmod +x verify_traffic.sh && ./verify_traffic.sh
# =============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-lead-sniper-prod}"
REGION="asia-south1"
COMMIT_SHA="${1:-}"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   LeadGenie — Production Traffic Verification V25.2.3       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Project : $PROJECT_ID"
echo "  Region  : $REGION"
echo ""

SERVICES=(
  "orchestrator"
  "lead-pipeline-main"
  "scraper-heavy"
  "digital-twin-engine"
  "whatsapp-webhook"
  "email-summary"
)

JOBS=(
  "shadow-learner-aggregator"
  "autonomous-engine"
)

FAIL_COUNT=0

# ─── Cloud Run Services ──────────────────────────────────────────────────────
echo "═══ CLOUD RUN SERVICES — Traffic Routing ═══"
echo ""

for SVC in "${SERVICES[@]}"; do
  echo "  ── $SVC ──"

  # Get the latest revision and its traffic percentage
  TRAFFIC_JSON=$(gcloud run services describe "$SVC" \
    --region="$REGION" --project="$PROJECT_ID" \
    --format="json(status.traffic)" 2>/dev/null || echo "{}")

  LATEST_REV=$(gcloud run services describe "$SVC" \
    --region="$REGION" --project="$PROJECT_ID" \
    --format="value(status.latestReadyRevisionName)" 2>/dev/null || echo "UNKNOWN")

  SERVING_REV=$(echo "$TRAFFIC_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    traffic = data.get('status', {}).get('traffic', [])
    for t in traffic:
        if t.get('percent', 0) > 0:
            print(f\"{t.get('revisionName','?')} → {t.get('percent',0)}%\")
except: print('PARSE_ERROR')
" 2>/dev/null || echo "PARSE_ERROR")

  IMAGE=$(gcloud run services describe "$SVC" \
    --region="$REGION" --project="$PROJECT_ID" \
    --format="value(spec.template.spec.containers[0].image)" 2>/dev/null || echo "UNKNOWN")

  IMAGE_TAG="${IMAGE##*:}"

  echo "     Latest revision : $LATEST_REV"
  echo "     Serving traffic : $SERVING_REV"
  echo "     Image tag       : $IMAGE_TAG"

  # Verify 100% on latest
  LATEST_PERCENT=$(echo "$TRAFFIC_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    traffic = data.get('status', {}).get('traffic', [])
    for t in traffic:
        if t.get('latestRevision', False):
            print(t.get('percent', 0)); sys.exit(0)
    # Fallback: check by revision name
    latest = '$LATEST_REV'
    for t in traffic:
        if t.get('revisionName') == latest:
            print(t.get('percent', 0)); sys.exit(0)
    print(0)
except: print(0)
" 2>/dev/null || echo "0")

  if [ "$LATEST_PERCENT" -eq 100 ] 2>/dev/null; then
    echo "     ✅ 100% traffic on latest revision"
  else
    echo "     ⚠️  WARNING: Latest revision has ${LATEST_PERCENT}% traffic (expected 100%)"
    echo ""
    echo "     FIX: Run this to migrate all traffic to latest:"
    echo "       gcloud run services update-traffic $SVC \\"
    echo "         --to-latest --region=$REGION --project=$PROJECT_ID"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi

  # If commit SHA provided, verify image matches
  if [ -n "$COMMIT_SHA" ]; then
    if [ "$IMAGE_TAG" = "$COMMIT_SHA" ]; then
      echo "     ✅ Image SHA matches deploy commit"
    else
      echo "     ⚠️  Image SHA mismatch: expected $COMMIT_SHA, got $IMAGE_TAG"
      FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
  fi

  echo ""
done

# ─── Cloud Run Jobs ──────────────────────────────────────────────────────────
echo "═══ CLOUD RUN JOBS — Image Verification ═══"
echo ""

for JOB in "${JOBS[@]}"; do
  echo "  ── $JOB ──"

  JOB_IMAGE=$(gcloud run jobs describe "$JOB" \
    --region="$REGION" --project="$PROJECT_ID" \
    --format="value(spec.template.spec.template.spec.containers[0].image)" 2>/dev/null || echo "UNKNOWN")

  JOB_TAG="${JOB_IMAGE##*:}"
  echo "     Image tag : $JOB_TAG"

  if [ -n "$COMMIT_SHA" ] && [ "$JOB_TAG" = "$COMMIT_SHA" ]; then
    echo "     ✅ Image SHA matches deploy commit"
  elif [ -n "$COMMIT_SHA" ]; then
    echo "     ⚠️  Image SHA mismatch: expected $COMMIT_SHA, got $JOB_TAG"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  else
    echo "     ℹ️  Pass commit SHA as arg to verify: ./verify_traffic.sh <commit_sha>"
  fi
  echo ""
done

# ─── Summary ─────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
if [ "$FAIL_COUNT" -eq 0 ]; then
  echo "  ✅ ALL SERVICES — 100% traffic on latest revisions."
else
  echo "  ⚠️  $FAIL_COUNT issue(s) detected. See warnings above."
fi
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Verified at: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""
