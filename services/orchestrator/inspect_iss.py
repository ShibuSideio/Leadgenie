"""Inspect actual symbols in inbound_sentiment_service.py"""
import ast, os, re, sys

p = os.path.join(os.path.dirname(__file__), "services", "inbound_sentiment_service.py")
src = open(p, encoding="utf-8").read()
tree = ast.parse(src)

names = set()
for n in ast.walk(tree):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(n.name)
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                names.add(t.id)

print("TOP-LEVEL NAMES:")
for nm in sorted(names):
    print(" ", nm)

# Find query modes - look for strings ending in _query or _mode
modes_q  = re.findall(r'"([a-z][a-z_]*_query)"', src)
modes_m  = re.findall(r'"([a-z][a-z_]*_mode)"', src)
modes_qo = [s.strip('"\'') for s in re.findall(r'["\'][a-z][a-z_]+["\']', src) if 'query' in s or 'mode' in s]

print("\nQuoted strings containing 'query' or 'mode':")
for m in sorted(set(modes_q + modes_m + modes_qo)):
    print(" ", m)

# Show first 60 lines for context
print("\nFIRST 60 LINES:")
for i, line in enumerate(src.splitlines()[:60], 1):
    print(f"{i:3}: {line}")
