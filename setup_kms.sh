#!/bin/bash
# Sideio Leads V13 KMS Setup Script

echo "Starting KMS Setup for Sideio Leads..."

# Set variables
PROJECT_ID=$(gcloud config get-value project)
LOCATION="global"
KEYRING_NAME="sideio-vault-ring"
KEY_NAME="sideio-wa-key"

echo "Current Project: $PROJECT_ID"
echo "Enabling KMS API..."
gcloud services enable cloudkms.googleapis.com

echo "Creating KeyRing: $KEYRING_NAME"
gcloud kms keyrings create $KEYRING_NAME \
    --location $LOCATION

echo "Creating CryptoKey: $KEY_NAME"
gcloud kms keys create $KEY_NAME \
    --location $LOCATION \
    --keyring $KEYRING_NAME \
    --purpose "encryption"

echo "Extracting Full Resource ID Path..."
KMS_KEY_PATH="projects/$PROJECT_ID/locations/$LOCATION/keyRings/$KEYRING_NAME/cryptoKeys/$KEY_NAME"

echo "Success! Your KMS Resource Path is:"
echo "$KMS_KEY_PATH"

echo "Uploading KMS path to Secret Manager as 'kms_wa_key_path'..."
printf "$KMS_KEY_PATH" | gcloud secrets create kms_wa_key_path --data-file=- --replication-policy="automatic"

echo "Granting Cloud Run Services access to decrypt..."
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
SA_EMAIL="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

gcloud kms keys add-iam-policy-binding $KEY_NAME \
    --location $LOCATION \
    --keyring $KEYRING_NAME \
    --member "serviceAccount:$SA_EMAIL" \
    --role "roles/cloudkms.cryptoKeyEncrypterDecrypter"

echo "Setup Complete! The workers will now natively fetch the key path from Secret Manager."
