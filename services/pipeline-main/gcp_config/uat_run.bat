@echo off
REM =============================================================================
REM V23 UAT — Full Provisioning + Dispatch Script (Windows cmd.exe)
REM =============================================================================
REM Run from the repo root:  gcp_config\uat_run.bat
REM Requires: gcloud auth login already completed
REM =============================================================================

SET PROJECT=trendpulse-app-2025
SET REGION=asia-south1
SET SERVICE=lead-pipeline-main
SET SA=lead-pipeline-sa@trendpulse-app-2025.iam.gserviceaccount.com
SET TENANT_ID=tenant_sideio_internal_test
SET CAMPAIGN_ID=uat_campaign_v23_preview_001
SET QUEUE=lead-pipeline-queue

echo ===========================================================
echo  SIDEIO V23 UAT — Environment Provisioning + Task Dispatch
echo ===========================================================

REM ── Step 1: Get v23-preview revision URL ────────────────────────────────────
echo [STEP 1] Discovering v23-preview revision URL...
FOR /F "usebackq delims=" %%i IN (`gcloud run services describe %SERVICE% --project=%PROJECT% --region=%REGION% --format="value(status.address.url)"`) DO SET PREVIEW_URL=%%i

IF "%PREVIEW_URL%"=="" (
    echo ERROR: Could not retrieve service URL. Check gcloud auth and service name.
    exit /b 1
)
echo [STEP 1] Service URL: %PREVIEW_URL%

REM ── Step 2: Apply env vars (OIDC audience + SA email) ───────────────────────
echo [STEP 2] Applying OIDC environment variables to %SERVICE%...
gcloud run services update %SERVICE% ^
  --project=%PROJECT% ^
  --region=%REGION% ^
  --update-env-vars="PIPELINE_MAIN_URL=%PREVIEW_URL%" ^
  --update-env-vars="PIPELINE_SA_EMAIL=%SA%" ^
  --update-env-vars="PROJECT_ID=%PROJECT%" ^
  --update-env-vars="LOCATION=%REGION%" ^
  --update-env-vars="CB_WINDOW_MINUTES=15"

IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to update service env vars.
    exit /b 1
)
echo [STEP 2] Env vars applied.

REM ── Step 3: Seed UAT Firestore tenant + campaign ──────────────────────────
echo [STEP 3] Seeding UAT Firestore tenant and campaign...
python services\pipeline-main\gcp_config\uat_seed_firestore.py seed
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Firestore seed failed. Check ADC credentials.
    exit /b 1
)

REM ── Step 4: Mint OIDC token ─────────────────────────────────────────────────
echo [STEP 4] Minting OIDC token from %SA%...
FOR /F "usebackq delims=" %%t IN (`gcloud auth print-identity-token --impersonate-service-account=%SA% --audiences=%PREVIEW_URL% --include-email`) DO SET OIDC_TOKEN=%%t

IF "%OIDC_TOKEN%"=="" (
    echo ERROR: Failed to mint OIDC token. Ensure impersonation is granted.
    echo Run: gcloud iam service-accounts add-iam-policy-binding %SA%
    echo       --member="user:shibu.thomas@sideio.com"
    echo       --role="roles/iam.serviceAccountTokenCreator"
    exit /b 1
)
echo [STEP 4] Token minted (preview: %OIDC_TOKEN:~0,30%...).

REM ── Step 5: Create Cloud Task targeting v23-preview ─────────────────────────
echo [STEP 5] Creating Cloud Task to %PREVIEW_URL%/produce...
SET PAYLOAD={"tenant_id":"%TENANT_ID%","campaign_id":"%CAMPAIGN_ID%"}

gcloud tasks create-http-task ^
  --project=%PROJECT% ^
  --location=%REGION% ^
  --queue=%QUEUE% ^
  --url=%PREVIEW_URL%/produce ^
  --method=POST ^
  --header="Content-Type:application/json" ^
  --header="X-CloudTasks-QueueName:%QUEUE%" ^
  --body-content="%PAYLOAD%" ^
  --oidc-service-account-email=%SA% ^
  --oidc-token-audience=%PREVIEW_URL%

IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Cloud Task creation failed.
    exit /b 1
)

echo.
echo [DONE] Cloud Task dispatched to v23-preview.
echo.
echo  Next steps:
echo  1. Open Cloud Logging, filter: resource.labels.service_name="lead-pipeline-main"
echo  2. Verify TRACE-1 through TRACE-10 appear sequentially.
echo  3. After ~60s, run verification:
echo     python services\pipeline-main\gcp_config\uat_seed_firestore.py verify
echo.
echo  Log Explorer quick filter:
echo  jsonPayload.message=~"TRACE-[0-9]+" AND resource.labels.service_name="lead-pipeline-main"
echo ===========================================================
