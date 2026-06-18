"""Diagnose SIGNAL_MODES AST and loadMe content."""
import ast, os, re

ORCH = os.path.dirname(os.path.abspath(__file__))
ISS = os.path.join(ORCH, "services", "inbound_sentiment_service.py")
APP_JS = os.path.join(os.path.dirname(os.path.dirname(ORCH)), "public", "app.js")

src = open(ISS, encoding="utf-8").read()
tree = ast.parse(src)

print("=== Checking SIGNAL_MODES ===")
for node in ast.walk(tree):
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.target.id == "SIGNAL_MODES":
            print(f"  AnnAssign SIGNAL_MODES: value type = {type(node.value).__name__}")
            if isinstance(node.value, ast.Dict):
                print(f"  Dict keys count: {len(node.value.keys)}")
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "SIGNAL_MODES":
                print(f"  Assign SIGNAL_MODES: value type = {type(node.value).__name__}")
                if isinstance(node.value, ast.Dict):
                    print(f"  Dict keys count: {len(node.value.keys)}")

print("\n=== Checking loadMe in app.js ===")
js = open(APP_JS, encoding="utf-8").read()
idx = js.find("async function loadMe()")
if idx == -1:
    idx = js.find("function loadMe")
print(f"loadMe found at index: {idx}")
if idx != -1:
    body = js[idx:idx+5000]
    print("Contains _renderInboundRadarBanner:", "_renderInboundRadarBanner" in body)
    print("Contains inbound_radar:", "inbound_radar" in body)
    print("Contains loadInboundSignals:", "loadInboundSignals" in body)
    print("Contains inbound-signals-panel:", "inbound-signals-panel" in body)
    # Show first 1000 chars of loadMe
    print("\nFirst 1000 chars of loadMe:")
    print(body[:1000])

print("\n=== Firestore indexes ===")
idx_path = os.path.join(os.path.dirname(os.path.dirname(ORCH)), "firestore.indexes.json")
print(f"Path: {idx_path}")
print(f"Exists: {os.path.isfile(idx_path)}")
if os.path.isfile(idx_path):
    import json
    data = json.load(open(idx_path, encoding="utf-8"))
    for ix in data.get("indexes", []):
        print(f"  Index on: {ix.get('collectionGroup')} - {ix.get('fields')}")
