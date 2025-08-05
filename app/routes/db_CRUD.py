from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile,File,Request,Response
from fastapi.responses import FileResponse
from neo4j import AsyncSession
from services.dependencies import get_current_user
from services.db_CRUD import GenericCRUD


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
    count = await crud.delete(item_id)
    if not count:
        raise HTTPException(404, detail="Item not found or not deleted")
    return {"detail": "Deleted successfully"}





