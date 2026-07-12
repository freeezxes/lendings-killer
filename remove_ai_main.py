import re

with open("main.py") as f:
    lines = f.readlines()

def get_block(start_line):
    start_idx = start_line - 1
    end_idx = start_idx
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.startswith("def ") or line.startswith("async def "):
            if end_idx != start_idx:
                break
        if line.startswith("class ") or line.startswith("# auth middleware"):
            break
        end_idx += 1
    return start_idx, end_idx

funcs = ["_ask_llm", "_extract_design_tokens", "_fetch_url", "_is_url", "_ai_generate", "_agent_generate", "_calc_cost", "_tokens_to_ours", "_ai_edit_chat", "_ai_chat"]

to_delete = set()
for i, line in enumerate(lines):
    for f in funcs:
        if line.startswith(f"def {f}(") or line.startswith(f"async def {f}("):
            start_idx, end_idx = get_block(i + 1)
            for j in range(start_idx, end_idx):
                to_delete.add(j)

new_lines = [line for i, line in enumerate(lines) if i not in to_delete]

with open("main.py", "w") as f:
    f.writelines(new_lines)

