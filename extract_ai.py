import re

with open("main.py") as f:
    lines = f.readlines()

def get_block(start_line):
    start_idx = start_line - 1
    end_idx = start_idx
    in_def = False
    brace_level = 0
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.startswith("def ") or line.startswith("async def "):
            if end_idx != start_idx:
                break
        if line.startswith("class ") or line.startswith("# auth middleware"):
            break
        end_idx += 1
    return "".join(lines[start_idx:end_idx]), end_idx

funcs = ["_extract_design_tokens", "_fetch_url", "_is_url", "_ai_generate", "_agent_generate", "_calc_cost", "_tokens_to_ours", "_ai_edit_chat", "_ai_chat", "_ask_llm"]

extracted = []
for i, line in enumerate(lines):
    for f in funcs:
        if line.startswith(f"def {f}(") or line.startswith(f"async def {f}("):
            block, _ = get_block(i + 1)
            extracted.append(block)

with open("services/ai_service.py", "w") as f:
    f.write("import re, json, httpx, uuid, base64\n")
    f.write("from core.config import settings\n")
    f.write("import logging\n")
    f.write("logger = logging.getLogger(__name__)\n\n")
    f.write("ALEM_API_KEY = settings.alem_api_key\n")
    f.write("ALEM_API_URL = settings.alem_api_url\n")
    f.write("ALEM_MODEL = settings.alem_model\n")
    f.write("PRICE_INPUT = settings.price_input\n")
    f.write("PRICE_OUTPUT = settings.price_output\n")
    f.write("SYSTEM_PROMPT = settings.system_prompt\n")
    f.write("EDIT_CHAT_SYSTEM = settings.edit_chat_system\n")
    f.write("CHAT_SYSTEM = settings.chat_system\n")
    f.write("from pathlib import Path\n")
    f.write("GENERATED_DIR = Path('generated_sites')\n\n")
    f.write("\n".join(extracted))

