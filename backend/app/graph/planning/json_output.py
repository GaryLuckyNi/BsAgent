import json
import re


def extract_json_object(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match is None:
        raise ValueError("Planner LLM output did not contain JSON object.")
    return json.loads(match.group(0))
