from firebase_functions import identity_fn, https_fn
from firebase_admin import initialize_app, firestore
import logging

initialize_app()
db = firestore.client()

@identity_fn.before_user_created()
def process_user_creation(event: identity_fn.AuthBlockingEvent) -> identity_fn.BeforeCreateResponse | None:
    user_record = event.data
    uid = user_record.uid
    email = user_record.email

    if not uid:
        return

    try:
        if email:
            invites = db.collection("invitations").where(field_path="email", op_string="==", value=email).limit(1).get()
            for invite in invites:
                invite_data = invite.to_dict()
                tenant_id = invite_data.get("tenant_id")
                
                invite.reference.delete()
                logging.info(f"Minting MEMBER claims securely for {email} linking to Tenant {tenant_id}")
                return identity_fn.BeforeCreateResponse(
                    custom_claims={
                        "tenant": tenant_id,
                        "role": "member"
                    }
                )
        
        # Strict Root Admins
        logging.info(f"Minting ADMIN claims for isolated workspace (UID {uid})")
        return identity_fn.BeforeCreateResponse(
            custom_claims={
                "tenant": uid,
                "role": "admin"
            }
        )
    except Exception as e:
        logging.error(f"CRITICAL: Auth Engine Exception for {email} - {str(e)}")
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message="Secure Identity Alignment Failed. Registration inherently blocked."
        )
