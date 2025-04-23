

from fastapi import APIRouter, Depends, HTTPException, Path
from neo4j import AsyncSession
from services.db_CRUD import GenericCRUD
from services.generics import get_link_options_service
from services.database import get_db
from fastapi import Query
from neo4j import AsyncDriver
from typing import Optional, List
import json

router = APIRouter()

@router.get("/link-options")
async def get_link_options(
    document_type: str = Query(..., description="Node label to query, e.g. 'User', 'Department'"),
    field: str = Query("name", description="Property to use as the label"),
    search_term: Optional[str] = Query(None, description="Text to filter dropdown results (typeahead)"),
    filters: Optional[str] = Query(None, description="JSON string for filtering nodes"),
    limit: int = Query(10, ge=1),
    offset: int = Query(0, ge=0),
    driver: AsyncDriver = Depends(get_db),
):
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