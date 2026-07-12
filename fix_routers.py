import os
import re

for root, dirs, files in os.walk("api/routers"):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            with open(path) as f:
                content = f.read()
            
            content = content.replace("main.services.ai_service.", "ai_service.")
            
            replacements = {
                "_ai_generate": "from services.ai_service import _ai_generate",
                "_calc_cost": "from services.ai_service import _calc_cost",
                "_tokens_to_ours": "from services.ai_service import _tokens_to_ours",
                "_ai_edit_chat": "from services.ai_service import _ai_edit_chat",
                "_ai_chat": "from services.ai_service import _ai_chat",
                "_extract_design_tokens": "from services.ai_service import _extract_design_tokens"
            }
            
            imports_to_add = set()
            for func, imp in replacements.items():
                if func in content and imp not in content:
                    imports_to_add.add(imp)
            
            if imports_to_add:
                lines = content.split("\n")
                lines.insert(2, "\n".join(imports_to_add))
                content = "\n".join(lines)
            
            with open(path, "w") as f:
                f.write(content)
