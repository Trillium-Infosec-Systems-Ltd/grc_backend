from fastapi import APIRouter, Depends, HTTPException
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
        result = await crud.create(data)  # Use await for async method
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
    print(f"🚀 GET endpoint hit with doctype={doctype}")
    crud = GenericCRUD(db, doctype)
    try:
        print("Before calling get_all")  # Debug print
        records = await crud.get_all()  # Use await for async method
        print("After calling get_all")  # Debug print
        if not records:
            raise HTTPException(status_code=404, detail="No items found")
        return [record["n"] for record in records]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
