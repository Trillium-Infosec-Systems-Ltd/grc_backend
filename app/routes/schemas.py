from fastapi import APIRouter, HTTPException, Depends
import os, json
from services.database import get_db
from services.db_CRUD import GenericCRUD
from neo4j import AsyncSession  # or from your actual Neo4j async client
from routes.auth_routes import get_user_by_id
from fastapi import Request# adjust path as needed
from services.dependencies import get_current_user

router = APIRouter()

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "schemas")
@router.get("/schemas/{schema_name}/{doc_id}")
@router.get("/schemas/{schema_name}")
async def get_schema(schema_name: str, doc_id: str = None, db: AsyncSession = Depends(get_db),request: Request = None,):
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

            # First try: fetch from control node's saved assessment
            query = """
                MATCH (q:control)
                WHERE q.id = $doc_id
                RETURN q
            """
            result = await crud.session.run(query, doc_id=doc_id)
            record = await result.single()

            use_fallback = True  # Flag to determine if we need fallback
            if record:
                node = record.get("q")
                if node:
                    raw_assessment = node.get("control_assessment")
                    if raw_assessment:
                        try:
                            questions = json.loads(raw_assessment)
                            if isinstance(questions, list) and questions:
                                use_fallback = False
                            else:
                                print("control_assessment is not a non-empty list")
                                questions = []
                        except json.JSONDecodeError:
                            print("Invalid JSON in control_assessment")
                            questions = []
            # Fallback: derive questions from control_question nodes
            if use_fallback:
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

        # Special handling for control_question schema
        if schema_name == "control_question":
            questions_text = doc_data.get("questions_text", [])
            weights = doc_data.get("weights", [])

            question_list = [
                {"question": q, "wheightage": weights[i] if i < len(weights) else 0}
                for i, q in enumerate(questions_text)
            ]
            doc_data["question"] = question_list


    # Inject default values into schema
    for field in schema.get("fields", []):
        # import pdb;pdb.set_trace()
        fieldname = field.get("fieldname")
        if fieldname == "control_assessment":
            field["default_value"] = questions
        else:
            field["default_value"] = doc_data.get(fieldname)


    schema["relationships"] = relationships

    return schema