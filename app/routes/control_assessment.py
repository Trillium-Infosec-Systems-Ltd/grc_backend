from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime
from uuid import uuid4
from neo4j import AsyncSession
from services.database import get_db
from services.dependencies import get_current_user
from typing import List, Optional
from pydantic import BaseModel
from schemas.complaince_schema import ComplianceCreate, ComplianceUpdate, ComplianceQuestion
import json

router = APIRouter()


async def create_control_assessment_for_organization(
    session: AsyncSession,
    control_id: str,
    organization_id: str,
    current_user: dict = None
):
    """
    Create a control assessment for a specific organization when a control is created.
    This function can be called from the control creation process.
    """
    now = datetime.utcnow().isoformat()
    
    # Get control questions for this control
    questions_query = """
    MATCH (cq:control_question)-[:HAS_QUESTION]->(c:control {id: $control_id})
    RETURN cq.questions_text as questions, cq.weights as weights
    """
    result = await session.run(questions_query, control_id=control_id)
    questions_data = await result.data()
    
    if not questions_data:
        # If no questions found, create assessment with empty questions
        default_questions = []
    else:
        # Convert the questions and weights to assessment format
        questions_text = questions_data[0].get("questions_text", [])
        weights = questions_data[0].get("weights", [])
        default_questions = [
            {"question": q, "answer": False, "weight": weights[i] if i < len(weights) else 1}
            for i, q in enumerate(questions_text)
        ]
    
    # Generate assessment ID
    id_query = """
    MERGE (c:Counter {doctype: 'assessment'})
    ON CREATE SET c.current = 1
    ON MATCH SET c.current = c.current + 1
    RETURN c.current AS new_id
    """
    result = await session.run(id_query)
    record = await result.single()
    assessment_id = f"assessment-{record['new_id']}"
    
    # Create the assessment node
    create_query = """
    CREATE (n:assessment {
        id: $id,
        control_id: $control_id,
        organization_id: $organization_id,
        questions: $questions,
        control_rating: $control_rating,
        compliance_status: $compliance_status,
        effectiveness_percentage: $effectiveness_percentage,
        created_by: $created_by,
        created_at: $created_at,
        updated_at: $updated_at
    })
    RETURN n
    """
    
    parameters = {
        "id": assessment_id,
        "control_id": control_id,
        "organization_id": organization_id,
        "questions": json.dumps(default_questions),
        "control_rating": "Not Assessed",
        "compliance_status": "Not Assessed",
        "effectiveness_percentage": 0.0,
        "created_by": current_user.get("id") if current_user else None,
        "created_at": now,
        "updated_at": now
    }
    
    result = await session.run(create_query, parameters)
    record = await result.single()
    
    # Create relationships
    # Link to organization
    await session.run("""
    MATCH (o:organization {id: $org_id})
    MATCH (ca:assessment {id: $assessment_id})
    MERGE (o)-[:HAS_CONTROL_ASSESSMENT]->(ca)
    """, org_id=organization_id, assessment_id=assessment_id)
    
    # Link to control
    await session.run("""
    MATCH (c:control {id: $control_id})
    MATCH (ca:assessment {id: $assessment_id})
    MERGE (c)-[:HAS_ASSESSMENT]->(ca)
    """, control_id=control_id, assessment_id=assessment_id)
    
    return assessment_id


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

    # Calculate effectiveness percentage
    total_weight = sum(q.get("weight", 1) for q in questions)
    scored_weight = sum(q.get("weight", 1) for q in questions if q.get("answer"))
    effectiveness = round((scored_weight / total_weight) * 100, 2) if total_weight > 0 else 0.0

    # Determine compliance status based on effectiveness
    if effectiveness >= 80:
        compliance_status = "Compliant"
    elif effectiveness >= 50:
        compliance_status = "Partially Compliant"
    else:
        compliance_status = "Non-Compliant"

    id_query = """
    MERGE (c:Counter {doctype: 'control_assessment'})
    ON CREATE SET c.current = 1
    ON MATCH SET c.current = c.current + 1
    RETURN c.current AS new_id
    """
    result = await session.run(id_query)
    record = await result.single()
    node_id = f"control_assessment-{record['new_id']}"

    create_query = """
    CREATE (n:control_assessment {
        id: $id,
        control_id: $control_id,
        organization_id: $organization_id,
        control_rating: $control_rating,
        questions: $questions,
        compliance_status: $compliance_status,
        effectiveness_percentage: $effectiveness_percentage,
        created_by: $created_by,
        created_at: $created_at,
        updated_at: $updated_at
    })
    RETURN n
    """
    parameters = {
        "id": node_id,
        "control_id": control_id,
        "organization_id": org_id,
        "control_rating": control_rating,
        "questions": json.dumps(questions),
        "compliance_status": compliance_status,
        "effectiveness_percentage": effectiveness,
        "created_by": current_user.get("id"),
        "created_at": now,
        "updated_at": now
    }

    result = await session.run(create_query, parameters)
    record = await result.single()
    node = record["n"]

    # Create relationships
    await session.run("""
    MATCH (o:organization {id: $org_id})
    MATCH (ca:control_assessment {id: $assessment_id})
    MERGE (o)-[:HAS_CONTROL_ASSESSMENT]->(ca)
    """, org_id=org_id, assessment_id=node_id)

    await session.run("""
    MATCH (c:control {id: $control_id})
    MATCH (ca:control_assessment {id: $assessment_id})
    MERGE (c)-[:HAS_ASSESSMENT]->(ca)
    """, control_id=control_id, assessment_id=node_id)

    return {"message": "Created", "id": node_id}


@router.get("/control-assessment/{assessment_id}")
async def get_control_assessment(
    assessment_id: str, 
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get assessment by ID with control details and questions.
    This endpoint returns the assessment along with the control information
    and questions that were attached to the control.
    """
    # Get assessment with control details and questions
    query = """
    MATCH (ca:assessment {id: $assessment_id})
    MATCH (c:control)-[:HAS_ASSESSMENT]->(ca)
    OPTIONAL MATCH (cq:control_question)-[:HAS_QUESTION]->(c)
    RETURN ca, c, collect(cq) as control_questions
    """
    result = await session.run(query, assessment_id=assessment_id)
    record = await result.single()
    if not record:
        raise HTTPException(status_code=404, detail="Assessment record not found")

    # Extract assessment data
    assessment = dict(record["ca"])
    control = dict(record["c"])
    control_questions = record["control_questions"]

    # Deserialize questions from assessment
    assessment["questions"] = json.loads(assessment["questions"]) if assessment.get("questions") else []

    # Get control questions (original questions attached to control)
    original_questions = []
    for cq in control_questions:
        if cq:  # Check if not None
            cq_dict = dict(cq)
            questions_text = cq_dict.get("questions_text", [])
            weights = cq_dict.get("weights", [])
            for i, question_text in enumerate(questions_text):
                original_questions.append({
                    "question": question_text,
                    "weight": weights[i] if i < len(weights) else 1
                })

    # Prepare response with control details and questions
    response = {
        "assessment": assessment,
        "control": {
            "id": control.get("id"),
            "control_id": control.get("control_id"),
            "control_name": control.get("control_name"),
            "category": control.get("category"),
            "description": control.get("description"),
            "rating": control.get("rating"),
            "ease_of_exploitation": control.get("ease_of_exploitation")
        },
        "control_questions": original_questions,  # Original questions attached to control
        "organization_id": assessment.get("organization_id")
    }

    return response


@router.get("/control-assessment/my-organization")
async def get_my_organization_assessments(
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all control assessments for the current user's organization.
    This is the main endpoint users will use to see their organization's assessments.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="User not associated with any organization")
    
    query = """
    MATCH (o:organization {id: $org_id})-[:HAS_CONTROL_ASSESSMENT]->(ca:control_assessment)
    MATCH (c:control)-[:HAS_ASSESSMENT]->(ca)
    RETURN ca, c.control_name as control_name, c.control_id as control_id, c.category as category
    ORDER BY c.category, c.control_name
    """
    result = await session.run(query, org_id=org_id)
    records = await result.data()
    
    assessments = []
    for record in records:
        assessment = dict(record["ca"])
        assessment["questions"] = json.loads(assessment["questions"])
        assessment["control_name"] = record["control_name"]
        assessment["control_id"] = record["control_id"]
        assessment["category"] = record["category"]
        assessments.append(assessment)
    
    return {
        "organization_id": org_id,
        "assessments": assessments,
        "total_count": len(assessments)
    }


@router.get("/control-assessment/organization/{organization_id}")
async def get_organization_assessments(
    organization_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all control assessments for a specific organization.
    This ensures users only see assessments for their organization.
    """
    # Verify user has access to this organization
    if current_user.get("org_id") != organization_id:
        raise HTTPException(status_code=403, detail="Access denied to this organization")
    
    query = """
    MATCH (o:organization {id: $org_id})-[:HAS_CONTROL_ASSESSMENT]->(ca:control_assessment)
    MATCH (c:control)-[:HAS_ASSESSMENT]->(ca)
    RETURN ca, c.control_name as control_name, c.control_id as control_id, c.category as category
    ORDER BY c.category, c.control_name
    """
    result = await session.run(query, org_id=organization_id)
    records = await result.data()
    
    assessments = []
    for record in records:
        assessment = dict(record["ca"])
        assessment["questions"] = json.loads(assessment["questions"])
        assessment["control_name"] = record["control_name"]
        assessment["control_id"] = record["control_id"]
        assessment["category"] = record["category"]
        assessments.append(assessment)
    
    return {
        "organization_id": organization_id,
        "assessments": assessments,
        "total_count": len(assessments)
    }


@router.get("/control-assessment/control/{control_id}/organization/{organization_id}")
async def get_control_assessment_by_control_and_org(
    control_id: str,
    organization_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get assessment for a specific control and organization.
    """
    # Verify user has access to this organization
    if current_user.get("org_id") != organization_id:
        raise HTTPException(status_code=403, detail="Access denied to this organization")
    
    query = """
    MATCH (ca:control_assessment {control_id: $control_id, organization_id: $org_id})
    RETURN ca
    """
    result = await session.run(query, control_id=control_id, org_id=organization_id)
    record = await result.single()
    
    if not record:
        raise HTTPException(status_code=404, detail="Assessment not found for this control and organization")
    
    assessment = dict(record["ca"])
    assessment["questions"] = json.loads(assessment["questions"])
    return assessment


@router.put("/control-assessment/{assessment_id}")
async def update_control_compliance(
    assessment_id: str,
    data: dict,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    now = datetime.utcnow().isoformat()

    control_rating = data.get("control_rating")
    questions = data.get("questions", [])

    # Calculate effectiveness percentage
    total_weight = sum(q.get("weight", 1) for q in questions)
    scored_weight = sum(q.get("weight", 1) for q in questions if q.get("answer"))
    effectiveness = round((scored_weight / total_weight) * 100, 2) if total_weight > 0 else 0.0

    # Determine compliance status based on effectiveness
    if effectiveness >= 80:
        compliance_status = "Compliant"
    elif effectiveness >= 50:
        compliance_status = "Partially Compliant"
    else:
        compliance_status = "Non-Compliant"

    update_query = """
    MATCH (n:control_assessment {id: $id})
    SET n.control_rating = $control_rating,
        n.questions = $questions,
        n.compliance_status = $compliance_status,
        n.effectiveness_percentage = $effectiveness_percentage,
        n.updated_at = $updated_at
    RETURN n
    """
    parameters = {
        "id": assessment_id,
        "control_rating": control_rating,
        "questions": json.dumps(questions),
        "compliance_status": compliance_status,
        "effectiveness_percentage": effectiveness,
        "updated_at": now
    }

    result = await session.run(update_query, parameters)
    record = await result.single()
    if not record:
        raise HTTPException(status_code=404, detail="Assessment record not found")

    return {"message": "Updated", "effectiveness_percentage": effectiveness, "compliance_status": compliance_status}


@router.delete("/control-assessment/{assessment_id}")
async def delete_control_compliance(
    assessment_id: str, 
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    query = """
    MATCH (n:control_assessment {id: $id})
    DETACH DELETE n
    RETURN COUNT(n) AS deleted
    """
    result = await session.run(query, id=assessment_id)
    summary = await result.consume()
    return {"message": "Deleted", "id": assessment_id}


@router.get("/control-assessment/statistics/my-organization")
async def get_my_organization_assessment_statistics(
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get assessment statistics for the current user's organization.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="User not associated with any organization")
    
    query = """
    MATCH (o:organization {id: $org_id})-[:HAS_CONTROL_ASSESSMENT]->(ca:control_assessment)
    RETURN 
        count(ca) as total_assessments,
        count(CASE WHEN ca.compliance_status = 'Compliant' THEN 1 END) as compliant_count,
        count(CASE WHEN ca.compliance_status = 'Partially Compliant' THEN 1 END) as partially_compliant_count,
        count(CASE WHEN ca.compliance_status = 'Non-Compliant' THEN 1 END) as non_compliant_count,
        count(CASE WHEN ca.compliance_status = 'Not Assessed' THEN 1 END) as not_assessed_count,
        avg(ca.effectiveness_percentage) as avg_effectiveness
    """
    result = await session.run(query, org_id=org_id)
    record = await result.single()
    
    if not record:
        return {
            "organization_id": org_id,
            "total_assessments": 0,
            "compliant_count": 0,
            "partially_compliant_count": 0,
            "non_compliant_count": 0,
            "not_assessed_count": 0,
            "avg_effectiveness": 0.0
        }
    
    return {
        "organization_id": org_id,
        "total_assessments": record["total_assessments"],
        "compliant_count": record["compliant_count"],
        "partially_compliant_count": record["partially_compliant_count"],
        "non_compliant_count": record["non_compliant_count"],
        "not_assessed_count": record["not_assessed_count"],
        "avg_effectiveness": round(record["avg_effectiveness"] or 0, 2)
    }


@router.post("/control-assessment/bulk-update")
async def bulk_update_assessments(
    updates: List[dict],
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Bulk update multiple assessments for the current user's organization.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="User not associated with any organization")
    
    updated_count = 0
    errors = []
    
    for update_data in updates:
        assessment_id = update_data.get("assessment_id")
        questions = update_data.get("questions", [])
        control_rating = update_data.get("control_rating")
        
        if not assessment_id:
            errors.append({"assessment_id": "missing", "error": "Assessment ID is required"})
            continue
        
        try:
            # Calculate effectiveness percentage
            total_weight = sum(q.get("weight", 1) for q in questions)
            scored_weight = sum(q.get("weight", 1) for q in questions if q.get("answer"))
            effectiveness = round((scored_weight / total_weight) * 100, 2) if total_weight > 0 else 0.0

            # Determine compliance status based on effectiveness
            if effectiveness >= 80:
                compliance_status = "Compliant"
            elif effectiveness >= 50:
                compliance_status = "Partially Compliant"
            else:
                compliance_status = "Non-Compliant"

            now = datetime.utcnow().isoformat()
            
            update_query = """
            MATCH (n:control_assessment {id: $id, organization_id: $org_id})
            SET n.control_rating = $control_rating,
                n.questions = $questions,
                n.compliance_status = $compliance_status,
                n.effectiveness_percentage = $effectiveness_percentage,
                n.updated_at = $updated_at
            RETURN n
            """
            parameters = {
                "id": assessment_id,
                "org_id": org_id,
                "control_rating": control_rating,
                "questions": json.dumps(questions),
                "compliance_status": compliance_status,
                "effectiveness_percentage": effectiveness,
                "updated_at": now
            }

            result = await session.run(update_query, parameters)
            record = await result.single()
            
            if record:
                updated_count += 1
            else:
                errors.append({"assessment_id": assessment_id, "error": "Assessment not found or access denied"})
                
        except Exception as e:
            errors.append({"assessment_id": assessment_id, "error": str(e)})
    
    return {
        "message": f"Updated {updated_count} assessments",
        "updated_count": updated_count,
        "errors": errors
    }


@router.get("/control-assessment/control/{control_id}")
async def get_assessments_by_control(
    control_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all assessments for a specific control across all organizations the user has access to.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="User not associated with any organization")
    
    query = """
    MATCH (ca:assessment {control_id: $control_id, organization_id: $org_id})
    MATCH (c:control {id: $control_id})
    RETURN ca, c.control_name as control_name, c.control_id as control_id, c.category as category
    """
    result = await session.run(query, control_id=control_id, org_id=org_id)
    records = await result.data()
    
    assessments = []
    for record in records:
        assessment = dict(record["ca"])
        assessment["questions"] = json.loads(assessment["questions"])
        assessment["control_name"] = record["control_name"]
        assessment["control_id"] = record["control_id"]
        assessment["category"] = record["category"]
        assessments.append(assessment)
    
    return {
        "control_id": control_id,
        "assessments": assessments,
        "total_count": len(assessments)
    }


@router.get("/control-assessment/statistics/control/{control_id}")
async def get_control_assessment_statistics(
    control_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get assessment statistics for a specific control.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="User not associated with any organization")
    
    query = """
    MATCH (ca:assessment {control_id: $control_id, organization_id: $org_id})
    RETURN 
        ca.compliance_status as compliance_status,
        ca.effectiveness_percentage as effectiveness_percentage,
        ca.control_rating as control_rating
    """
    result = await session.run(query, control_id=control_id, org_id=org_id)
    record = await result.single()
    
    if not record:
        return {
            "control_id": control_id,
            "compliance_status": "Not Assessed",
            "effectiveness_percentage": 0.0,
            "control_rating": "Not Assessed"
        }
    
    return {
        "control_id": control_id,
        "compliance_status": record["compliance_status"],
        "effectiveness_percentage": record["effectiveness_percentage"],
        "control_rating": record["control_rating"]
    }


@router.post("/control-assessment/questions")
async def create_control_questions(
    data: dict,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Create questions and attach them to a control.
    This will also update existing assessments for all organizations.
    """
    control_id = data.get("control_id")
    questions = data.get("questions", [])
    
    if not control_id or not questions:
        raise HTTPException(status_code=400, detail="Missing control_id or questions")
    
    # Verify control exists
    control_query = """
    MATCH (c:control {id: $control_id})
    RETURN c
    """
    result = await session.run(control_query, control_id=control_id)
    if not await result.single():
        raise HTTPException(status_code=404, detail="Control not found")
    
    # Create control_question node
    now = datetime.utcnow().isoformat()
    
    # Generate ID for control_question
    id_query = """
    MERGE (c:Counter {doctype: 'control_question'})
    ON CREATE SET c.current = 1
    ON MATCH SET c.current = c.current + 1
    RETURN c.current AS new_id
    """
    result = await session.run(id_query)
    record = await result.single()
    question_id = f"control_question-{record['new_id']}"
    
    # Prepare question data
    questions_text = [q.get("question", "") for q in questions]
    weights = [q.get("weight", 1) for q in questions]
    
    # Create control_question node
    create_query = """
    CREATE (n:control_question {
        id: $id,
        control: $control_id,
        questions_text: $questions_text,
        weights: $weights,
        created_at: $created_at,
        updated_at: $updated_at
    })
    RETURN n
    """
    
    parameters = {
        "id": question_id,
        "control_id": control_id,
        "questions_text": questions_text,
        "weights": weights,
        "created_at": now,
        "updated_at": now
    }
    
    result = await session.run(create_query, **parameters)
    record = await result.single()
    
    # Create relationship to control
    relation_query = """
    MATCH (c:control {id: $control_id})
    MATCH (cq:control_question {id: $question_id})
    MERGE (c)-[:HAS_QUESTION]->(cq)
    """
    await session.run(relation_query, control_id=control_id, question_id=question_id)
    
    # Update existing assessments for all organizations
    await update_assessments_for_all_organizations(session, control_id, questions)
    
    return {
        "message": "Questions created and attached to control",
        "question_id": question_id,
        "control_id": control_id,
        "questions_count": len(questions)
    }


async def update_assessments_for_all_organizations(session: AsyncSession, control_id: str, questions: list):
    """
    Update existing assessments for all organizations when new questions are added to a control.
    """
    # Get all organizations
    org_query = """
    MATCH (o:organization)
    RETURN o.id as org_id
    """
    result = await session.run(org_query)
    organizations = await result.data()
    
    # Update assessment for each organization
    for org in organizations:
        org_id = org["org_id"]
        try:
            await update_assessment_for_organization(session, control_id, org_id, questions)
        except Exception as e:
            print(f"Failed to update assessment for organization {org_id}: {str(e)}")


async def update_assessment_for_organization(session: AsyncSession, control_id: str, organization_id: str, questions: list):
    """
    Update assessment for a specific organization with new questions.
    """
    # Find existing assessment
    find_query = """
    MATCH (ca:assessment {control_id: $control_id, organization_id: $organization_id})
    RETURN ca
    """
    result = await session.run(find_query, control_id=control_id, organization_id=organization_id)
    record = await result.single()
    
    if record:
        # Update existing assessment with new questions
        assessment_questions = [
            {"question": q.get("question", ""), "answer": False, "weight": q.get("weight", 1)}
            for q in questions
        ]
        
        update_query = """
        MATCH (ca:assessment {control_id: $control_id, organization_id: $organization_id})
        SET ca.questions = $questions,
            ca.updated_at = $updated_at
        RETURN ca
        """
        
        now = datetime.utcnow().isoformat()
        await session.run(update_query, 
                         control_id=control_id, 
                         organization_id=organization_id,
                         questions=json.dumps(assessment_questions),
                         updated_at=now)
    else:
        # Create new assessment if it doesn't exist
        await create_control_assessment_for_organization(session, control_id, organization_id)


@router.put("/control-assessment/{assessment_id}/answers")
async def save_assessment_answers(
    assessment_id: str,
    data: dict,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Save assessment answers with organization-specific logic.
    This endpoint updates the assessment with answers and calculates effectiveness.
    """
    questions = data.get("questions", [])
    control_rating = data.get("control_rating", "Not Assessed")
    
    if not questions:
        raise HTTPException(status_code=400, detail="Questions data is required")
    
    # Verify assessment exists and belongs to user's organization
    verify_query = """
    MATCH (ca:assessment {id: $assessment_id})
    MATCH (o:organization {id: $org_id})-[:HAS_CONTROL_ASSESSMENT]->(ca)
    RETURN ca
    """
    result = await session.run(verify_query, 
                              assessment_id=assessment_id, 
                              org_id=current_user.get("org_id"))
    if not await result.single():
        raise HTTPException(status_code=404, detail="Assessment not found or access denied")
    
    # Calculate effectiveness percentage
    total_weight = sum(q.get("weight", 1) for q in questions)
    scored_weight = sum(q.get("weight", 1) for q in questions if q.get("answer"))
    effectiveness = round((scored_weight / total_weight) * 100, 2) if total_weight > 0 else 0.0
    
    # Determine compliance status based on effectiveness
    if effectiveness >= 80:
        compliance_status = "Compliant"
    elif effectiveness >= 50:
        compliance_status = "Partially Compliant"
    else:
        compliance_status = "Non-Compliant"
    
    # Update assessment
    now = datetime.utcnow().isoformat()
    update_query = """
    MATCH (ca:assessment {id: $assessment_id})
    SET ca.questions = $questions,
        ca.control_rating = $control_rating,
        ca.compliance_status = $compliance_status,
        ca.effectiveness_percentage = $effectiveness_percentage,
        ca.updated_at = $updated_at
    RETURN ca
    """
    
    result = await session.run(update_query,
                              assessment_id=assessment_id,
                              questions=json.dumps(questions),
                              control_rating=control_rating,
                              compliance_status=compliance_status,
                              effectiveness_percentage=effectiveness,
                              updated_at=now)
    
    record = await result.single()
    if not record:
        raise HTTPException(status_code=500, detail="Failed to update assessment")
    
    updated_assessment = dict(record["ca"])
    updated_assessment["questions"] = json.loads(updated_assessment["questions"])
    
    return {
        "message": "Assessment answers saved successfully",
        "assessment": updated_assessment,
        "effectiveness_percentage": effectiveness,
        "compliance_status": compliance_status
    }


@router.get("/control-assessment/control/{control_id}/questions")
async def get_control_questions(
    control_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all questions attached to a specific control.
    """
    query = """
    MATCH (c:control {id: $control_id})
    OPTIONAL MATCH (cq:control_question)-[:HAS_QUESTION]->(c)
    RETURN c, collect(cq) as control_questions
    """
    result = await session.run(query, control_id=control_id)
    record = await result.single()
    
    if not record:
        raise HTTPException(status_code=404, detail="Control not found")
    
    control = dict(record["c"])
    control_questions = record["control_questions"]
    
    # Extract questions from control_question nodes
    questions = []
    for cq in control_questions:
        if cq:  # Check if not None
            cq_dict = dict(cq)
            questions_text = cq_dict.get("questions_text", [])
            weights = cq_dict.get("weights", [])
            for i, question_text in enumerate(questions_text):
                questions.append({
                    "question": question_text,
                    "weight": weights[i] if i < len(weights) else 1
                })
    
    return {
        "control": {
            "id": control.get("id"),
            "control_id": control.get("control_id"),
            "control_name": control.get("control_name"),
            "category": control.get("category"),
            "description": control.get("description")
        },
        "questions": questions,
        "questions_count": len(questions)
    }
