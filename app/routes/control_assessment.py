from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime
from uuid import uuid4
from neo4j import AsyncSession
from services.database import get_db
from services.dependencies import  get_current_user
from typing import List, Optional
from pydantic import BaseModel
from schemas.complaince_schema import ComplianceCreate,ComplianceUpdate,ComplianceQuestion
import json

router = APIRouter()


@router.post("/control-assessment")
async def create_control_compliance(
    data: dict,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    now = datetime.utcnow().isoformat()

    org_id = data.get("organization_id")
    control_id = data.get("control_id")
    control_rating = data.get("control_rating")
    questions = data.get("questions", [])

    if not org_id or not control_id or not questions:
        raise HTTPException(status_code=400, detail="Missing required fields")

    # total_weight = sum(q.get("weight", 1) for q in questions)
    # scored_weight = sum(q.get("weight", 1) for q in questions if q.get("answer"))
    # effectiveness = round((scored_weight / total_weight) * 100, 2) if total_weight > 0 else 0.0

    id_query = """
    MERGE (c:Counter {doctype: 'control_compliance'})
    ON CREATE SET c.current = 1
    ON MATCH SET c.current = c.current + 1
    RETURN c.current AS new_id
    """
    result = await session.run(id_query)
    record = await result.single()
    node_id = f"control_compliance-{record['new_id']}"

    create_query = """
    CREATE (n:control_compliance {
        id: $id,
        organization_id: $organization_id,
        control_id: $control_id,
        control_rating: $control_rating,
        questions: $questions,
        created_at: $created_at,
        updated_at: $updated_at
    })
    RETURN n
    """
    parameters = {
        "id": node_id,
        "organization_id": org_id,
        "control_id": control_id,
        "control_rating": control_rating,
        "questions": json.dumps(questions),
        "created_at": now,
        "updated_at": now
    }

    result = await session.run(create_query, parameters)
    record = await result.single()
    node = record["n"]

    await session.run("""
    MATCH (o:organization {id: $org_id})
    MATCH (c:control_compliance {id: $compliance_id})
    MERGE (o)-[:HAS_CONTROL_COMPLIANCE]->(c)
    """, org_id=org_id, compliance_id=node_id)

    return {"message": "Created", "id": node_id}


@router.get("/control-assessment/{compliance_id}")
async def get_control_compliance(compliance_id: str, session: AsyncSession = Depends(get_db)):
    query = """
    MATCH (n:control_compliance {id: $id})
    RETURN n
    """
    result = await session.run(query, id=compliance_id)
    record = await result.single()
    if not record:
        raise HTTPException(status_code=404, detail="Compliance record not found")

    node = dict(record["n"])
    node["questions"] = json.loads(node["questions"])
    return node


@router.put("/control-assessment/{compliance_id}")
async def update_control_compliance(
    compliance_id: str,
    data: dict,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    now = datetime.utcnow().isoformat()

    control_rating = data.get("control_rating")
    questions = data.get("questions", [])

    total_weight = sum(q.get("weight", 1) for q in questions)
    scored_weight = sum(q.get("weight", 1) for q in questions if q.get("answer"))

    update_query = """
    MATCH (n:control_compliance {id: $id})
    SET n.control_rating = $control_rating,

        n.questions = $questions,
        n.updated_at = $updated_at
    RETURN n
    """
    parameters = {
        "id": compliance_id,
        "control_rating": control_rating,

        "questions": json.dumps(questions),
        "updated_at": now
    }

    result = await session.run(update_query, parameters)
    record = await result.single()
    if not record:
        raise HTTPException(status_code=404, detail="Compliance record not found")

    return {"message": "Updated"}


@router.delete("/control-assessment/{compliance_id}")
async def delete_control_compliance(compliance_id: str, session: AsyncSession = Depends(get_db)):
    query = """
    MATCH (n:control_compliance {id: $id})
    DETACH DELETE n
    RETURN COUNT(n) AS deleted
    """
    result = await session.run(query, id=compliance_id)
    summary = await result.consume()
    return {"message": "Deleted", "id": compliance_id}
