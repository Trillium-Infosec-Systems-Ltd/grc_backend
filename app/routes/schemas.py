from fastapi import APIRouter, HTTPException, Depends
import os, json
from services.database import get_db
from services.db_CRUD import GenericCRUD
from neo4j import AsyncSession  # or from your actual Neo4j async client

router = APIRouter()

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "schemas")

@router.get("/schemas/{schema_name}/{doc_id}")
@router.get("/schemas/{schema_name}")
async def get_schema(schema_name: str, doc_id: str = None, db: AsyncSession = Depends(get_db)):
    file_path = os.path.join(SCHEMA_DIR, f"{schema_name}.json")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Schema not found")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Invalid JSON format")

    # If doc_id is provided, get the actual document data
    if doc_id:
        crud = GenericCRUD(db, schema_name)
        document = await crud.get_by_id(doc_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # Extract node data and relationships
        doc_node = document.get("node", {})
        relationships = document.get("relationships", [])

        # ✅ Convert Neo4j Node object to Python dict
        doc_data = dict(doc_node)

        # ✅ Special case: transform control_question's fields
        if schema_name == "control_question":
            questions_text = doc_data.get("questions_text", [])
            weights = doc_data.get("weights", [])
            question_list = [
                {"question": q, "wheightage": weights[i] if i < len(weights) else 0}
                for i, q in enumerate(questions_text)
            ]
            doc_data["question"] = question_list

        # ✅ Inject values into schema fields
        for field in schema.get("fields", []):
            field_name = field.get("fieldname")
            field["default_value"] = doc_data.get(field_name)

        # Attach relationships for frontend rendering (optional)
        schema["relationships"] = relationships

    return schema