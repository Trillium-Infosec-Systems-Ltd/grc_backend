from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile,File,Request,Response
from fastapi.responses import FileResponse
from neo4j import AsyncSession
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
        return result["n"]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

@router.get("/data/{doctype}")
async def get_all_items(
    doctype: str,
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):
    # Extract query parameters as filters (except skip/limit)
    filters = dict(request.query_params)
    filters.pop("skip", None)
    filters.pop("limit", None)

    crud = GenericCRUD(db, doctype)
    try:
        paginated_data = await crud.get_all(skip=skip, limit=limit, filters=filters)
        if not paginated_data["items"]:
            raise HTTPException(status_code=404, detail="No items found")
        return paginated_data
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
async def update_item(doctype: str, item_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
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




@router.post("/data/{doctype}/{item_id}/upload")
async def upload_and_attach_files(
    doctype: str,
    item_id: str,
    files: List[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db)
):
    BASE_UPLOAD_DIR = "static/uploads"
    target_dir = os.path.join(BASE_UPLOAD_DIR)
    os.makedirs(target_dir, exist_ok=True)

    filepaths = []

    for file in files:
        original_filename = file.filename
        filepath = os.path.join(target_dir, original_filename)

        # Save the file
        with open(filepath, "wb") as f:
            content = await file.read()
            f.write(content)

        # Construct accessible URL (you might want to make this dynamic in production)
        url_path = f"http://127.0.0.1:8000/{filepath.replace(os.sep, '/')}"
        filepaths.append(url_path)


    query = f"""
    MATCH (n:{doctype} {{id: $item_id}})
    SET n.attachments = coalesce(n.attachments, []) + $filepaths,
        n.updated_at = $updated_at
    RETURN n
    """

    result = await db.run(
        query,
        item_id=item_id,
        filepaths=filepaths,
        updated_at=datetime.utcnow().isoformat()
    )
    record = await result.single()
    if not record:
        raise HTTPException(status_code=404, detail="Item not found")
    return record["n"]


