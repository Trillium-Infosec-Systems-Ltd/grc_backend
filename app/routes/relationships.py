from fastapi import APIRouter, Depends
from neo4j import AsyncDriver
from pydantic import BaseModel
from typing import Dict, Literal
from services.relationships import create_dynamic_relationship
from services.database import get_db

router = APIRouter()

class NodeModel(BaseModel):
    id: str
    label: str

class RelationshipModel(BaseModel):
    relation_type: str
    direction: Literal["OUTGOING", "INCOMING"]
    properties: Dict[str, str | int | float | bool]

class RelationshipRequest(BaseModel):
    node1: NodeModel
    node2: NodeModel
    relationship: RelationshipModel

@router.post("/create-relationship")
async def create_relationship(payload: RelationshipRequest, driver: AsyncDriver = Depends(get_db)):
    rel_type = await create_dynamic_relationship(driver, payload.node1.dict(), payload.node2.dict(), payload.relationship.dict())
    return {"message": "Relationship created", "relationship_type": rel_type}