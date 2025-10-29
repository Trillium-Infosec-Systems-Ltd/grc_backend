from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile,File,Request,Response
from fastapi.responses import FileResponse
from neo4j import AsyncSession
from services.dependencies import get_current_user
from services.db_CRUD import GenericCRUD
from services.schema_loader import load_schema


from services.database import get_db
from fastapi import Query
from neo4j import AsyncDriver
from typing import Optional, List
import json
import os 
from datetime import datetime
import csv
import io
import os



router = APIRouter()


import math

def sanitize_for_json(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(i) for i in obj]
    return obj

@router.post("/data/{doctype}")
async def create_item(
    doctype: str,
    data: dict,
    db: AsyncSession = Depends(get_db)
):
    crud = GenericCRUD(db, doctype)
    try:
        result = await crud.create(data)
        if not result:
            raise HTTPException(status_code=500, detail="Failed to create item")
        if doctype == "control_question":
            return result
        return result["n"]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

@router.get("/data/{doctype}")
async def get_all_items(
    doctype: str,
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    
    
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):
    
        
    filters = dict(request.query_params)
    if doctype == "control_question" and filters.get("control_id"):
        filters["control"] = filters.pop("control_id")
        filters['control'] = filters['control'].strip('\'"')
    filters.pop("skip", None)
    filters.pop("limit", None)

    crud = GenericCRUD(db, doctype)
    try:
        paginated_data = await crud.get_all(
            skip=skip,
            limit=limit,
            filters=filters,
            current_user=current_user
        )
        if not paginated_data["items"]:
            raise HTTPException(status_code=404, detail="No items found")

        return sanitize_for_json(paginated_data)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/data/{doctype}/{item_id}")
async def get_item(doctype: str, item_id: str, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
    item = await crud.get_by_id(item_id)
    if not item:
        raise HTTPException(404, detail="Item not found")
    return item

@router.put("/data/{doctype}/{item_id}")
async def update_item(doctype: str, item_id: str, data: dict, db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    crud = GenericCRUD(db, doctype, current_user)
    updated = await crud.update(item_id, data)
    if not updated:
        raise HTTPException(404, detail="Item not found or not updated")
    return updated

@router.delete("/data/{doctype}/{item_id}")
async def delete_item(doctype: str, item_id: str, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
    # Check relationships first — if any exist, refuse delete and return the first relation in the error
    item = await crud.get_by_id(item_id)
    if not item:
        raise HTTPException(404, detail="Item not found")

    relationships = item.get("relationships") or []
    # filter out empty/null relationship entries
    relationships = [r for r in relationships if r and r.get("node")]
    if relationships:
        first = relationships[0]
        node = first.get("node") or {}

        # Prefer a human-friendly name, fall back to schema-defined label field, then doctype.
        linked_name = node.get("name")
        linked_id = node.get("id")
        linked_doctype = None
        if linked_id and isinstance(linked_id, str) and "-" in linked_id:
            linked_doctype = linked_id.split("-")[0]
        where_desc = linked_doctype or "another item"

        # If node has a friendly name property, use it first
        if linked_name:
            target_desc = f"{where_desc} '{linked_name}'"
        else:
            # Try to load the doctype schema and pick a display field
            friendly_value = None
            if linked_doctype:
                try:
                    schema = load_schema(linked_doctype)
                    # 1) field with default_label
                    for f in schema.get("fields", []):
                        if f.get("default_label"):
                            fieldname = f.get("fieldname")
                            if node.get(fieldname):
                                friendly_value = node.get(fieldname)
                                break
                    # 2) first display_on_frontend
                    if not friendly_value:
                        for f in schema.get("fields", []):
                            if f.get("display_on_frontend"):
                                fieldname = f.get("fieldname")
                                if node.get(fieldname):
                                    friendly_value = node.get(fieldname)
                                    break
                    # 3) any field from schema that exists on node
                    if not friendly_value:
                        for f in schema.get("fields", []):
                            fieldname = f.get("fieldname")
                            if node.get(fieldname):
                                friendly_value = node.get(fieldname)
                                break
                except Exception:
                    friendly_value = None

            if friendly_value:
                target_desc = f"{where_desc} '{friendly_value}'"
            else:
                # Fall back to doctype only (avoid showing raw id if it's not user-friendly)
                target_desc = where_desc

        # Friendly, user-facing message — no developer-level details about relationship type/direction
        detail = f"Cannot delete this item because it is being used by {target_desc}. Remove that link first and try again."
        raise HTTPException(status_code=400, detail=detail)

    count = await crud.delete(item_id)
    if not count:
        raise HTTPException(404, detail="Item not found or not deleted")
    return {"detail": "Deleted successfully"}


@router.delete("/data/{doctype}")
async def delete_all_items(doctype: str, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
    count = await crud.delete_all()   # 👈 you'll implement this in GenericCRUD

    if not count:
        raise HTTPException(404, detail=f"No items found for doctype '{doctype}'")

    return {"detail": f"Deleted {count} items successfully"}


