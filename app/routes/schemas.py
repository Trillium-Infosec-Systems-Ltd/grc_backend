from fastapi import APIRouter, HTTPException, Depends
import os, json
from services.database import get_db
from services.db_CRUD import GenericCRUD
from neo4j import AsyncSession  # or from your actual Neo4j async client
from routes.auth_routes import get_user_by_id
from fastapi import Request# adjust path as needed
from services.dependencies import get_current_user
import math
import ast
from neo4j.graph import Node, Relationship


def _to_public_static_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip()
    if not normalized:
        return ""
    if normalized.startswith("/api/"):
        normalized = normalized[4:]
    elif normalized.startswith("api/"):
        normalized = normalized[3:]
    if normalized.startswith("apistatic/"):
        normalized = normalized[3:]
    elif normalized.startswith("/apistatic/"):
        normalized = normalized[4:]
    normalized = normalized.lstrip("/")
    if normalized.startswith("static/"):
        return f"/{normalized}"
    return f"/{normalized}"


def _normalize_attached_files(value):
    def _extract_url(item):
        if item is None:
            return ""
        if isinstance(item, dict):
            return str(item.get("url") or item.get("path") or "").strip()

        text = str(item).strip()
        if not text:
            return ""

        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = ast.literal_eval(text)
                if isinstance(parsed, dict):
                    return str(parsed.get("url") or parsed.get("path") or "").strip()
            except Exception:
                pass
        return text

    if value is None:
        return []

    if isinstance(value, str):
        value = [value]

    if not isinstance(value, list):
        value = [value]

    normalized = []
    for item in value:
        url = _to_public_static_path(_extract_url(item))
        if url:
            normalized.append(url)
    return normalized


def _attached_file_objects(value):
    urls = _normalize_attached_files(value)
    return [{"name": os.path.basename(url), "url": url} for url in urls]

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
    # Treat "complaince" as alias for "control"
    actual_schema_name = "control" if schema_name == "complaince" else schema_name

    file_path = os.path.join(SCHEMA_DIR, f"{actual_schema_name}.json")

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
        if actual_schema_name == "users":
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
            crud = GenericCRUD(db, actual_schema_name)
            document = await crud.get_by_id(doc_id)

        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        # import pdb;pdb.set_trace()
        doc_node = document.get("node", {})
        relationships = document.get("relationships", [])
        doc_data = dict(doc_node)

        # Special handling for control schema (also applies to complaince)
        if actual_schema_name == "control":
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
                    doc_data["remarks"] = assessment.get("remarks", "")
                    doc_data["observation"] = assessment.get("observation", "")
                    doc_data["reference_evidence"] = assessment.get("reference_evidence", "")
                    doc_data["attached_files"] = _attached_file_objects(assessment.get("attached_files", []))
                    doc_data["next_review_date"] = assessment.get("next_review_date", "")
                    doc_data["control_applicable"] = assessment.get("control_applicable", "Yes")
                    # Always load fresh question answers from shared question_response nodes
                    # (not from stale control_assessment JSON stored in the node)
                    assessment_found = True


            # ALWAYS load fresh question answers from shared question_response nodes (if org_id exists)
            # This ensures cross-framework sync works — answers are shared across all controls
            if org_id:
                canon_query = """
                MATCH (c:control {control_id: $control_id})-[:HAS_QUESTION]->(q:question)
                OPTIONAL MATCH (q)-[:HAS_RESPONSE]->(r:question_response {organization_id: $org_id})
                RETURN q.id AS qid, q.text AS text, q.weight AS weight,
                       r.answer AS answer, r.evidence AS evidence, r.remarks AS remarks
                ORDER BY q.text
                """
                canon_result = await crud.session.run(canon_query, control_id=control_id, org_id=org_id)
                async for rec in canon_result:
                    questions.append({
                        "question": rec["text"],
                        "answer": bool(rec["answer"]) if rec["answer"] is not None else False,
                        "weight": float(rec["weight"] or 1.0),
                        "question_id": rec["qid"],
                        "evidence": rec["evidence"] or "",
                        "remarks": rec["remarks"] or "",
                    })

            # Fallback to legacy control_question nodes if no canonical questions found
            if not questions:
                legacy_query = """
                MATCH (q:control_question)
                WHERE q.control = $control_id
                RETURN q
                """
                legacy_result = await crud.session.run(legacy_query, control_id=control_id)
                async for record in legacy_result:
                    q_node = record["q"]
                    q_dict = dict(q_node)
                    questions_text = q_dict.get("questions_text", [])
                    question_weightage = q_dict.get("weights", [])
                    q_list = [
                        {"question": text, "answer": False, "weight": float(weight or 1.0)}
                        for text, weight in zip(questions_text, question_weightage)
                    ]
                    questions.extend(q_list)

            # Set defaults only if no assessment node was found
            if not assessment_found:
                doc_data["rating"] = "Low"
                doc_data["compliance_status"] = "Non Compliant"
                doc_data["remarks"] = ""
                doc_data["observation"] = ""
                doc_data["reference_evidence"] = ""
                doc_data["attached_files"] = []

            doc_data["control_assessment"] = questions

            # Set ease_of_exploitation based on rating (case-insensitive, Low->Low, Medium->Medium, High->High)
            ease_map = {
                "High": "Low",
                "Medium": "Medium",
                "Low": "High"
            }
            rating_val = str(doc_data["rating"]).strip().lower()
            print(doc_data["rating"])
            ease_of_exploitation = ease_map.get(doc_data["rating"], "Unknown")
            doc_data["ease_of_exploitation"] = ease_of_exploitation

                
        # Special handling for control_question schema
        if actual_schema_name == "control_question":
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
        elif fieldname == "attached_files":
            field["default_value"] = sanitize_for_json(_attached_file_objects(doc_data.get(fieldname, [])))
        elif fieldname == "control_id" and actual_schema_name == "control_question":
            field["default_value"] = doc_data.get("control_id", [])
        elif fieldname == "description" and actual_schema_name == "control_question":
            field["default_value"] = doc_data.get("description", "")
        elif fieldname == "question" and actual_schema_name == "control_question":
            # For canonical questions, map text field to question field
            field["default_value"] = doc_data.get("text", "")
        else:
            field["default_value"] = sanitize_for_json(doc_data.get(fieldname))


    schema["relationships"] = sanitize_for_json(relationships)
    schema["data"] = sanitize_for_json(doc_data)
    sanitized_schema = sanitize_for_json(schema)
    return sanitized_schema