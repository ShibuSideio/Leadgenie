import sys
sys.stdout.reconfigure(encoding='utf-8')
KEYWORDS = [
    'def dispatch', 'def consume', 'def _settle', 'def finalize',
    'PrismPipeline', 'pre_filter_gemini', 'final_score_and_dm',
    'unprocessed_queue', 'global_lead_lock', 'exclusivity_lock',
    '@app.route("/dispatch")', '@app.route("/finalize")',
    'settle_credit', 'batch_size', 'process_url',
]
with open('services/pipeline-main/main.py', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()
hits = [(i+1, l.rstrip()) for i, l in enumerate(lines)
        if any(k in l for k in KEYWORDS)]
for ln, text in hits[:80]:
    print(f'{ln:5}: {text}')
