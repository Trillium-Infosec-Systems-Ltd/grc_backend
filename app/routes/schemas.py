from fastapi import APIRouter, HTTPException, Depends
import os, json
from services.database import get_db
from services.db_CRUD import GenericCRUD
from neo4j import AsyncSession  # or from your actual Neo4j async client
from routes.auth_routes import get_user_by_id
from fastapi import Request# adjust path as needed
from services.dependencies import get_current_user
import math
from neo4j.graph import Node, Relationship

def sanitize_for_json(obj):
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    
    elif isinstance(obj, list):
        return [sanitize_for_json(item) for item in obj]

    elif isinstance(obj, float):
        # Convert NaN/inf to None (JSON-compliant)
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj

    elif isinstance(obj, Node):
        # Convert Neo4j Node to plain dict
        return sanitize_for_json(dict(obj))

    elif isinstance(obj, Relationship):
        # Convert Neo4j Relationship to plain dict
        return sanitize_for_json(dict(obj))

    # Optional: handle other Neo4j types if needed
    elif hasattr(obj, "__dict__"):
        # Generic object fallback (careful with circular refs)
        return sanitize_for_json(vars(obj))

    return obj

router = APIRouter()

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "schemas")
@router.get("/schemas/{schema_name}/{doc_id}")
@router.get("/schemas/{schema_name}")
async def get_schema(
    schema_name: str,
    doc_id: str = None,
    db: AsyncSession = Depends(get_db),
    request: Request = None,
    current_user: dict = Depends(get_current_user)
):
    file_path = os.path.join(SCHEMA_DIR, f"{schema_name}.json")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Schema not found")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Invalid JSON format")

    doc_data = {}
    relationships = []
    questions = []

    # If doc_id is provided, get the actual document data
    if doc_id:
        if schema_name == "users":
            # ✅ Extract token from request headers
            auth_header = request.headers.get("authorization")
            if not auth_header:
                raise HTTPException(status_code=401, detail="Authorization header missing")

            token = auth_header.split(" ")[1]
            current_user = await get_current_user(token)

            user_response = await get_user_by_id(user_id=doc_id, session=db, current_user=current_user)
            user_data = user_response["user"]
            relationships = user_response["relationships"]
            document = {"node": user_data,"relationships":relationships}
        else:
            crud = GenericCRUD(db, schema_name)
            document = await crud.get_by_id(doc_id)

        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        # import pdb;pdb.set_trace()
        doc_node = document.get("node", {})
        relationships = document.get("relationships", [])
        doc_data = dict(doc_node)

        # Special handling for control schema
        if schema_name == "control":
            control_id = doc_data.get("control_id", "")
            questions = []

            # Use org_id from current_user
            org_id = current_user.get("org_id") if current_user else None
            
            print('+++++++++++++++',org_id)

            assessment_found = False
            print(control_id)
            if org_id:
                # Try to get control_assessment node for this control and org
                assessment_query = """
                MATCH (a:control_assessment {control_id: $control_id, organization_id: $org_id})
                RETURN a
                """
                result = await db.run(assessment_query, control_id=control_id, org_id=org_id)
                record = await result.single()
                if record and record.get("a"):
                    assessment = record["a"]

                    print("i am here bro")
                    doc_data["rating"] = assessment.get("control_rating")
                    doc_data["compliance_status"] = assessment.get("control_compliance")
                    # Parse questions if present
                    questions_raw = assessment.get("control_assessment")
                    if questions_raw:
                        try:
                            questions = json.loads(questions_raw) if isinstance(questions_raw, str) else questions_raw
                        except Exception:
                            questions = []
                    doc_data["control_assessment"] = questions
                    assessment_found = True


            if not assessment_found:
                print("no assessment found")
                # Fallback: derive questions from control_question nodes
                query = """
                        MATCH (q:control_question)
                        WHERE q.control = $control_id
                        RETURN q
                """
                result = await crud.session.run(query, control_id=control_id)
                async for record in result:
                    q_node = record["q"]
                    q_dict = dict(q_node)
                    questions_text = q_dict.get("questions_text", [])
                    question_weightage = q_dict.get("weights", [])
                    q_list = [
                        {"question": text, "answer": False, "weight": weight}
                        for text, weight in zip(questions_text, question_weightage)
                    ]
                    questions.extend(q_list)
                    
                doc_data["rating"] = "LOW"
                doc_data["compliance_status"] = "Non-Compliant"
                doc_data["control_assessment"] = questions
                
                print("i am here on the bottom")

        # Special handling for control_question schema
        if schema_name == "control_question":
            questions_text = doc_data.get("questions_text", [])
            weights = doc_data.get("weights", [])

            question_list = [
                {"question": q, "wheightage": weights[i] if i < len(weights) else 0}
                for i, q in enumerate(questions_text)
            ]
            doc_data["question"] = sanitize_for_json(question_list)

            doc_data = sanitize_for_json(doc_data)
    # Inject default values into schema
    for field in schema.get("fields", []):
        # import pdb;pdb.set_trace()
        fieldname = field.get("fieldname")
        if fieldname == "control_assessment":
            field["default_value"] = sanitize_for_json(questions)
        else:
            field["default_value"] = sanitize_for_json(doc_data.get(fieldname))


    schema["relationships"] = sanitize_for_json(relationships)
    schema["data"] = sanitize_for_json(doc_data)
    sanitized_schema = sanitize_for_json(schema)
    print("schema:", sanitized_schema)
    return sanitized_schema