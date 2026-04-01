from firebase_functions.auth_fn import on_user_created, AuthEvent
from firebase_admin import initialize_app, firestore, auth

initialize_app()
db = firestore.client()

@on_user_created()
def process_user_creation(event: AuthEvent):
    user_record = event.data
    uid = user_record.uid
    email = user_record.email

    if not uid:
        return

    try:
        if email:
            invites = db.collection("invitations").where("email", "==", email).limit(1).get()
            for invite in invites:
                invite_data = invite.to_dict()
                tenant_id = invite_data.get("tenant_id")
                
                auth.set_custom_user_claims(uid, {
                    "tenant": tenant_id,
                    "role": "member"
                })
                invite.reference.delete()
                print(f"Minted MEMBER claims securely for {email} linking to Tenant {tenant_id}")
                return
        
        # Strict Root Admins
        auth.set_custom_user_claims(uid, {
            "tenant": uid,
            "role": "admin"
        })
        print(f"Minted ADMIN claims for isolated workspace (UID {uid})")
    except Exception as e:
        print(f"Auth Security Engine Exception: {e}")
