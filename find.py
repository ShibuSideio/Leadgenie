with open('services/orchestrator/main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    for i, line in enumerate(lines):
        if '/api/campaigns' in line and 'GET' in line:
            print(f"{i+1}: {line.strip()}")
            # Print next 10 lines
            for j in range(i, min(i+10, len(lines))):
                print(f"  {j+1}: {lines[j].strip()}")
