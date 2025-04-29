from fastapi import APIRouter, HTTPException
import os, json

router = APIRouter()  # ✅ Only this one

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "schemas")

@router.get("/schemas/{schema_name}")
async def get_schema(schema_name: str):

    file_path = os.path.join(SCHEMA_DIR, f"{schema_name}.json")

    if not os.path.exists(file_path): 
        raise HTTPException(status_code=404, detail="Schema not found")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = json.load(f)
        return content
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Invalid JSON format")
