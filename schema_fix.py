import re

with open('services/pipeline-main/main.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Clean the Imports
old_imports = """try:
    from vertexai.generative_models import GenerativeModel, GenerationConfig, Schema, Type
except ImportError:
    # Enterprise fallback for preview namespaces
    from vertexai.preview.generative_models import GenerativeModel, GenerationConfig, Schema, Type"""

new_imports = """from vertexai.generative_models import GenerativeModel, GenerationConfig"""

text = text.replace(old_imports, new_imports)

# 2. Convert Schema to Dictionary
old_schema = """    schema = Schema(
        type=Type.OBJECT,
        properties={
            "score": Schema(type=Type.INTEGER),
            "dm": Schema(type=Type.STRING, description="If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."),
            "pain_point": Schema(type=Type.STRING, description="If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."),
            "icebreaker_angle": Schema(type=Type.STRING, description="If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."),
            "hiring_intent_found": Schema(
                type=Type.STRING,
                enum=["Yes", "No"]
            ),
            "tech_stack_found": Schema(
                type=Type.ARRAY,
                items=Schema(type=Type.STRING),
                description="Only include real, verified software technologies (e.g., 'wordpress', 'shopify', 'stripe'). Do NOT include internal system notes."
            ),
            "whatsapp_draft": Schema(type=Type.STRING),
            "email": Schema(type=Type.STRING),
            "phone": Schema(type=Type.STRING),
            "linkedin": Schema(type=Type.STRING),
            "decision_maker_name": Schema(type=Type.STRING, description="Specific human name found, else 'Unknown'"),
            "decision_maker_title": Schema(type=Type.STRING, description="Title of the decision maker, else 'Unknown'"),
            "company_size_tier": Schema(
                type=Type.STRING, 
                description="Must be strictly one of: 'Startup', 'Mid-Market', 'Enterprise', or 'Unknown'"
            ),
            "primary_objection_hypothesis": Schema(type=Type.STRING, description="A 1-sentence prediction of why they might reject our bio/pitch based on their site context.")
        },
        required=["score", "dm", "pain_point", "icebreaker_angle", "hiring_intent_found", "tech_stack_found", "decision_maker_name", "decision_maker_title", "company_size_tier", "primary_objection_hypothesis"]
    )"""

new_schema = """    schema = {
        "type": "OBJECT",
        "properties": {
            "score": {"type": "INTEGER"},
            "dm": {"type": "STRING", "description": "If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."},
            "pain_point": {"type": "STRING", "description": "If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."},
            "icebreaker_angle": {"type": "STRING", "description": "If the scraped text does not represent a valid B2B prospect or lacks sufficient information to draft a message, you MUST output the exact string 'N/A' for this field. Do not leave it blank or null."},
            "hiring_intent_found": {
                "type": "STRING",
                "enum": ["Yes", "No"]
            },
            "tech_stack_found": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Only include real, verified software technologies (e.g., 'wordpress', 'shopify', 'stripe'). Do NOT include internal system notes."
            },
            "whatsapp_draft": {"type": "STRING"},
            "email": {"type": "STRING"},
            "phone": {"type": "STRING"},
            "linkedin": {"type": "STRING"},
            "decision_maker_name": {"type": "STRING", "description": "Specific human name found, else 'Unknown'"},
            "decision_maker_title": {"type": "STRING", "description": "Title of the decision maker, else 'Unknown'"},
            "company_size_tier": {
                "type": "STRING", 
                "description": "Must be strictly one of: 'Startup', 'Mid-Market', 'Enterprise', or 'Unknown'"
            },
            "primary_objection_hypothesis": {"type": "STRING", "description": "A 1-sentence prediction of why they might reject our bio/pitch based on their site context."}
        },
        "required": ["score", "dm", "pain_point", "icebreaker_angle", "hiring_intent_found", "tech_stack_found", "decision_maker_name", "decision_maker_title", "company_size_tier", "primary_objection_hypothesis"]
    }"""

text = text.replace(old_schema, new_schema)

with open('services/pipeline-main/main.py', 'w', encoding='utf-8') as f:
    f.write(text)
