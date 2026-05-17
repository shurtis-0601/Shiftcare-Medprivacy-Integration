#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Deploy the ShiftCare → MedPrivacy → Google Drive pipeline
#
# Prerequisites:
#   gcloud CLI installed and authenticated
#   gcloud config set project <GCP_PROJECT_ID>
#   All environment variables below filled in
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
REGION="australia-southeast1"           # Sydney — closest GCP region to Melbourne
FUNCTION_NAME="shiftcare-medprivacy-pipeline"
SERVICE_ACCOUNT_NAME="shiftcare-pipeline-sa"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_JOB="shiftcare-pipeline-daily"
SCHEDULER_SCHEDULE="0 8 * * *"          # 8:00 AM daily
SCHEDULER_TIMEZONE="Australia/Melbourne"
PUBSUB_TOPIC="shiftcare-pipeline-trigger"

# Environment variables passed to the Cloud Function at runtime
# Replace placeholder values before running
DRIVE_PENDING_FOLDER_ID="${DRIVE_PENDING_FOLDER_ID:?Set DRIVE_PENDING_FOLDER_ID}"
DRIVE_QUARANTINE_FOLDER_ID="${DRIVE_QUARANTINE_FOLDER_ID:?Set DRIVE_QUARANTINE_FOLDER_ID}"
REFERENCE_MAP_SHEET_ID="${REFERENCE_MAP_SHEET_ID:?Set REFERENCE_MAP_SHEET_ID}"
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:?Set NOTIFICATION_EMAIL}"
GMAIL_SENDER_EMAIL="${GMAIL_SENDER_EMAIL:-}"

echo "=== ShiftCare MedPrivacy Pipeline Deployment ==="
echo "Project:  $PROJECT_ID"
echo "Region:   $REGION"
echo ""

# ── 1. Enable required APIs ───────────────────────────────────────────────────
echo "[1/7] Enabling GCP APIs..."
gcloud services enable \
    cloudfunctions.googleapis.com \
    cloudscheduler.googleapis.com \
    pubsub.googleapis.com \
    secretmanager.googleapis.com \
    sheets.googleapis.com \
    drive.googleapis.com \
    gmail.googleapis.com \
    logging.googleapis.com \
    cloudbuild.googleapis.com \
    --project="$PROJECT_ID"

# ── 2. Create service account ─────────────────────────────────────────────────
echo "[2/7] Creating service account..."
gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
    --display-name="ShiftCare MedPrivacy Pipeline" \
    --project="$PROJECT_ID" 2>/dev/null || echo "  (service account already exists)"

# Grant required IAM roles
ROLES=(
    "roles/secretmanager.secretAccessor"   # Read secrets
    "roles/logging.logWriter"              # Write Cloud Logs
    "roles/cloudfunctions.invoker"         # Allow self-invocation if needed
)
for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SERVICE_ACCOUNT}" \
        --role="$ROLE" \
        --quiet
done
echo "  IAM roles granted."

# NOTE: Google Drive and Sheets access is granted at the resource level (share
# the folder / spreadsheet with $SERVICE_ACCOUNT), not via IAM roles.
#
# Drive Pending folder:     Share with $SERVICE_ACCOUNT as Editor
# Drive Quarantine folder:  Share with $SERVICE_ACCOUNT as Editor
# Reference Map Sheet:      Share with $SERVICE_ACCOUNT as Editor
#
# Run these steps manually in Google Drive / Google Sheets before first run.

# ── 3. Store ShiftCare API key in Secret Manager ──────────────────────────────
echo "[3/7] Storing ShiftCare API key in Secret Manager..."
if ! gcloud secrets describe shiftcare-api-key --project="$PROJECT_ID" &>/dev/null; then
    echo -n "Enter ShiftCare API key: "
    read -rs SHIFTCARE_KEY
    echo ""
    echo -n "$SHIFTCARE_KEY" | gcloud secrets create shiftcare-api-key \
        --data-file=- \
        --replication-policy=automatic \
        --project="$PROJECT_ID"
    echo "  Secret created."
else
    echo "  Secret 'shiftcare-api-key' already exists — skipping."
fi

# Grant service account access to the secret
gcloud secrets add-iam-policy-binding shiftcare-api-key \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="$PROJECT_ID" \
    --quiet

# ── 4. Create Pub/Sub topic ───────────────────────────────────────────────────
echo "[4/7] Creating Pub/Sub topic..."
gcloud pubsub topics create "$PUBSUB_TOPIC" \
    --project="$PROJECT_ID" 2>/dev/null || echo "  (topic already exists)"

# ── 5. Deploy Cloud Function (2nd gen) ───────────────────────────────────────
echo "[5/7] Deploying Cloud Function..."
ENV_VARS="GCP_PROJECT_ID=${PROJECT_ID}"
ENV_VARS+=",DRIVE_PENDING_FOLDER_ID=${DRIVE_PENDING_FOLDER_ID}"
ENV_VARS+=",DRIVE_QUARANTINE_FOLDER_ID=${DRIVE_QUARANTINE_FOLDER_ID}"
ENV_VARS+=",REFERENCE_MAP_SHEET_ID=${REFERENCE_MAP_SHEET_ID}"
ENV_VARS+=",NOTIFICATION_EMAIL=${NOTIFICATION_EMAIL}"
ENV_VARS+=",TIMEZONE=Australia/Melbourne"
if [[ -n "$GMAIL_SENDER_EMAIL" ]]; then
    ENV_VARS+=",GMAIL_SENDER_EMAIL=${GMAIL_SENDER_EMAIL}"
fi

gcloud functions deploy "$FUNCTION_NAME" \
    --gen2 \
    --region="$REGION" \
    --runtime=python312 \
    --source=. \
    --entry-point=run_pipeline_scheduled \
    --trigger-topic="$PUBSUB_TOPIC" \
    --service-account="$SERVICE_ACCOUNT" \
    --memory=512Mi \
    --timeout=540s \
    --min-instances=0 \
    --max-instances=1 \
    --set-env-vars="$ENV_VARS" \
    --project="$PROJECT_ID"

echo "  Cloud Function deployed."

# Also deploy the HTTP trigger variant for manual runs
gcloud functions deploy "${FUNCTION_NAME}-http" \
    --gen2 \
    --region="$REGION" \
    --runtime=python312 \
    --source=. \
    --entry-point=run_pipeline_http \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account="$SERVICE_ACCOUNT" \
    --memory=512Mi \
    --timeout=540s \
    --set-env-vars="$ENV_VARS" \
    --project="$PROJECT_ID"

echo "  HTTP trigger deployed."

# ── 6. Create Cloud Scheduler job ────────────────────────────────────────────
echo "[6/7] Creating Cloud Scheduler job..."
gcloud scheduler jobs create pubsub "$SCHEDULER_JOB" \
    --location="$REGION" \
    --schedule="$SCHEDULER_SCHEDULE" \
    --time-zone="$SCHEDULER_TIMEZONE" \
    --topic="$PUBSUB_TOPIC" \
    --message-body='{"trigger":"scheduled"}' \
    --project="$PROJECT_ID" 2>/dev/null \
    || gcloud scheduler jobs update pubsub "$SCHEDULER_JOB" \
        --location="$REGION" \
        --schedule="$SCHEDULER_SCHEDULE" \
        --time-zone="$SCHEDULER_TIMEZONE" \
        --project="$PROJECT_ID"

echo "  Scheduler job: '$SCHEDULER_SCHEDULE' ($SCHEDULER_TIMEZONE)"

# ── 7. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "=== Deployment complete ==="
echo ""
echo "Service account: $SERVICE_ACCOUNT"
echo ""
echo "MANUAL STEPS STILL REQUIRED:"
echo "  1. Share the Google Drive 'Pending' folder with $SERVICE_ACCOUNT (Editor)"
echo "  2. Share the Google Drive 'Quarantine' folder with $SERVICE_ACCOUNT (Editor)"
echo "  3. Share the Reference Map Google Sheet with $SERVICE_ACCOUNT (Editor)"
echo "  4. If using Gmail notifications:"
echo "     a. Enable domain-wide delegation on the service account in Workspace Admin"
echo "     b. Add OAuth scope: https://www.googleapis.com/auth/gmail.send"
echo "     c. Set GMAIL_SENDER_EMAIL to the delegated sender address"
echo ""
echo "Test the pipeline manually:"
HTTP_URL=$(gcloud functions describe "${FUNCTION_NAME}-http" \
    --gen2 --region="$REGION" --project="$PROJECT_ID" \
    --format='value(serviceConfig.uri)' 2>/dev/null || echo "<get from console>")
echo "  curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" \\"
echo "       \"${HTTP_URL}?date=$(date -d yesterday +%Y-%m-%d 2>/dev/null || date -v-1d +%Y-%m-%d)\""
echo ""
echo "Trigger manually right now:"
echo "  gcloud scheduler jobs run $SCHEDULER_JOB --location=$REGION --project=$PROJECT_ID"
