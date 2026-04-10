import os

with open('services/orchestrator/main.py', 'r', encoding='utf-8') as f:
    text = f.read()

import_snippet = '''from google.cloud import secretmanager, firestore, bigquery, storage
import PyPDF2
import io'''

if 'import PyPDF2' not in text:
    text = text.replace('from google.cloud import secretmanager', import_snippet)
    
new_endpoint = '''            elif request.path == "/api/tenant_profiles/extract-kb" and request.method == "POST":
                filepath = data.get("filepath")
                if not filepath:
                    return jsonify({"error": "Missing filepath"}), 400
                
                bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", f"{PROJECT_ID}.appspot.com")
                print(f"[KB] Extracting document from gs://{bucket_name}/{filepath}")
                
                try:
                    storage_client = storage.Client()
                    bucket = storage_client.bucket(bucket_name)
                    blob = bucket.blob(filepath)
                    file_bytes = blob.download_as_bytes()
                    
                    extracted_text = ""
                    if filepath.lower().endswith('.pdf'):
                        pdf = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                        extracted_text = "\\n".join(page.extract_text() for page in pdf.pages if page.extract_text())
                    elif filepath.lower().endswith('.txt'):
                        extracted_text = file_bytes.decode('utf-8', errors='ignore')
                    else:
                        return jsonify({"error": "Unsupported file format. Use PDF or TXT."}), 400
                        
                    if extracted_text.strip():
                        # Cap the length to avoid firing massive arrays into Firestore limits blindly
                        extracted_text = extracted_text.strip()[:10000] 
                        # Use ArrayUnion to push to knowledge_base_text
                        db.collection("tenant_profiles").document(tenant_id).update({
                            "knowledge_base_text": firestore.ArrayUnion([extracted_text])
                        })
                        
                        return jsonify({"status": "success", "message": "Knowledge base appended"}), 200
                    return jsonify({"error": "No textual content extracted"}), 400
                except Exception as e:
                    print(f"[KB] Failed extraction: {e}")
                    return jsonify({"error": f"Extraction failed: {str(e)}"}), 500
                
'''

if '/api/tenant_profiles/extract-kb' not in text:
    target = 'elif request.path == "/api/campaigns" and request.method == "POST":'
    text = text.replace(target, new_endpoint + '            ' + target)
    
with open('services/orchestrator/main.py', 'w', encoding='utf-8') as f:
    f.write(text)
print('Orchestrator KB endpoint injected')
