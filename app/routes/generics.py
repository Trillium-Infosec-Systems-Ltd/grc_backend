

from fastapi import APIRouter, Depends, HTTPException, Path
from neo4j import AsyncSession
from services.db_CRUD import GenericCRUD
from services.generics import get_link_options_service
from services.database import get_db
from fastapi import Query
from neo4j import AsyncDriver
from typing import Optional, List
import json
from services.schema_loader import load_schema
router = APIRouter()

@router.get("/link-options")
async def get_link_options(
    document_type: str = Query(..., description="Node label to query, e.g. 'User', 'Department'"),
    field: str = Query("name", description="Property to use as the label"),
    search_term: Optional[str] = Query(None, description="Text to filter dropdown results (typeahead)"),
    filters: Optional[str] = Query(None, description="JSON string for filtering nodes"),
    offset: int = Query(0, ge=0),
    driver: AsyncDriver = Depends(get_db),
):
    limit = 20
    try:
        data = await get_link_options_service(
            driver=driver,
            document_type=document_type,
            field=field,
            search_term=search_term,
            filters=filters,
            limit=limit,
            offset=offset
        )
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@router.get("/table_meta/{doctype}")
async def get_form_metadata(doctype: str):
    try:
        schema = load_schema(doctype)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Schema not found for {doctype}")

    columns = []

    # First: fields from schema
    for field in schema.get("fields", []):
        if field.get("display_on_frontend", False):
            columns.append({
                "title": field.get("label", field["fieldname"]),
                "dataIndex": field["fieldname"],
                "key": field["fieldname"]
            })

    # Second: Add system fields manually
    system_fields = [
        # {
        #     "title": "Created At",
        #     "dataIndex": "created_at",
        #     "key": "created_at"
        # },
        {
            "title": "Last Update",
            "dataIndex": "updated_at",
            "key": "updated_at"
        }
    ]
    columns.extend(system_fields)

    return {
        "form_id": doctype,
        "columns": columns
    }