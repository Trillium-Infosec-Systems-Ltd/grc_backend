from pathlib import Path
import json
from fastapi import HTTPException
from neo4j import AsyncDriver

SCHEMA_DIR = Path(__file__).parent.parent / "schemas"

def load_schema(doctype: str) -> dict:
    """Load schema JSON with validation"""
    try:
        
        filepath = SCHEMA_DIR / f"{doctype.lower()}.json"
        if not filepath.exists():
            available = [f.stem for f in SCHEMA_DIR.glob("*.json")]
            raise HTTPException(404, detail={
                "error": f"Schema '{doctype}' not found",
                "available_schemas": available
            })
        
        with open(filepath) as f:
            return json.load(f)
            
    except json.JSONDecodeError:
        raise HTTPException(500, detail="Invalid schema JSON")
    



