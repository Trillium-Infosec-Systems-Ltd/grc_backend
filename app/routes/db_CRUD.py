from fastapi import APIRouter, Depends, HTTPException, Path
from neo4j import AsyncSession
from services.db_CRUD import GenericCRUD
from services.database import get_db

router = APIRouter(prefix="/api", tags=["CRUD"])

@router.post("/{doctype}")
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

@router.get("/{doctype}")
async def get_all_items(
    doctype: str,
    db: AsyncSession = Depends(get_db)
):
    crud = GenericCRUD(db, doctype)
    try:
        records = await crud.get_all()
        if not records:
            raise HTTPException(status_code=404, detail="No items found")
        return [record["n"] for record in records]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{doctype}/{item_id}")
async def get_item(doctype: str, item_id: str, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
    item = await crud.get_by_id(item_id)
    if not item:
        raise HTTPException(404, detail="Item not found")
    return item

@router.put("/{doctype}/{item_id}")
async def update_item(doctype: str, item_id: str, data: dict, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
    updated = await crud.update(item_id, data)
    if not updated:
        raise HTTPException(404, detail="Item not found or not updated")
    return updated

@router.delete("/{doctype}/{item_id}")
async def delete_item(doctype: str, item_id: str, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
    count = await crud.delete(item_id)
    if not count:
        raise HTTPException(404, detail="Item not found or not deleted")
    return {"detail": "Deleted successfully"}
