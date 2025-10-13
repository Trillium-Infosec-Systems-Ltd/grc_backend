
from fastapi import APIRouter, Depends, HTTPException, Path, Response ,Request
from fastapi.responses import FileResponse
from neo4j import AsyncSession
from services.db_CRUD import GenericCRUD
from services.generics import get_link_options_service
from services.database import get_db
from fastapi import Query
from neo4j import AsyncDriver
from typing import Optional, List
import json
from services.schema_loader import load_schema
import csv
import io
import os
from datetime import datetime
from typing import List

from fastapi import APIRouter, UploadFile, File, HTTPException
from datetime import datetime
import os
import uuid
from typing import List
import pandas as pd





router = APIRouter()


@router.get("/link-options")
async def get_link_options(
        document_type: str = Query(..., description="Node label to query, e.g. 'User', 'Department'"),
        field: Optional[str] = Query(None, description="Comma-separated fields like 'name,id'"),
        search_term: Optional[str] = Query(None, description="Search values, e.g. 'john,123'"),
        filters: Optional[str] = Query(None, description="JSON string for filtering nodes"),
        offset: int = Query(0, ge=0),
        driver: AsyncDriver = Depends(get_db),
):
    limit = 20

    try:
        schema = load_schema(document_type)


        schema_fields = {f["fieldname"] for f in schema.get("fields", [])}

        # Parse fields and search terms
        field_list = field.split(",") if field else []
        search_terms = search_term.split(",") if search_term else []

        # If no valid fields given, fallback to default_label
        if not field_list or any(f not in schema_fields for f in field_list):
            default_field = next((f["fieldname"] for f in schema["fields"] if f.get("default_label")), None)
            if not default_field:
                raise HTTPException(status_code=400, detail="No valid fields or default_label found.")
            field_list = [default_field]
            search_terms = [search_term] if search_term else []

        # Adjust lengths
        if len(search_terms) < len(field_list):
            search_terms += [""] * (len(field_list) - len(search_terms))
        elif len(search_terms) > len(field_list):
            search_terms = search_terms[:len(field_list)]

        search_fields = list(zip(field_list, search_terms))

        data = await get_link_options_service(
            driver=driver,
            document_type=document_type,
            search_fields=search_fields,
            filters=filters,
            limit=limit,
            offset=offset
        )
        return data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/table_meta/{doctype}")
async def get_form_metadata(doctype: str):
    try:
        schema = load_schema(doctype)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Schema not found for {doctype}")

    columns = []

    # First: fields from schema
    for field in schema.get("fields", []):
        if field.get("display_on_frontend", False):
            column = {
                "title": field.get("label", field["fieldname"]),
                "dataIndex": field["fieldname"],
                "key": field["fieldname"],
                "fieldType": field["fieldtype"]
            }
            if "is_colorful" in field:
                column["isColorful"] = field["is_colorful"]

            columns.append(column)
            # columns.append({
            #     "title": field.get("label", field["fieldname"]),
            #     "dataIndex": field["fieldname"],
            #     "key": field["fieldname"],
            #     "fieldType": field["fieldtype"],
            #     "isColorful": field["is_colorful"]
            # })

    return {
        "form_id": doctype,
        "columns": columns
    }



# @router.get("/csv_template/{node_type}")
# async def generate_csv_template(node_type: str):
#     try:
#         schema = load_schema(node_type)

#         fieldnames = [
#             f["label"] for f in schema["fields"]
#             if not f.get("hidden", False)
#         ]

#         output = io.StringIO()
#         writer = csv.DictWriter(output, fieldnames=fieldnames)
#         writer.writeheader()
#         response = Response(content=output.getvalue(), media_type="text/csv")
#         response.headers["Content-Disposition"] = f"attachment; filename={node_type}_template.csv"
#         return response

#     except Exception as e:
#         return {"error": str(e)}

from pathlib import Path as PathLib
from fastapi import Path   # for API params

BASE_DIR = PathLib(__file__).resolve().parent.parent.parent
STATIC_FOLDER = BASE_DIR / "static" / "templates"

@router.get("/csv_template/{node_type}")
async def get_csv_template(node_type: str):
    file_path = STATIC_FOLDER / f"{node_type}_template.xlsx"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Template not found")

    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{node_type}_template.xlsx"
    )






@router.get("/export_csv/{doctype}")
async def export_csv(
        doctype: str,
        request: Request,
        skip: int = Query(0, ge=0),
        limit: int = Query(1000, ge=1, le=10000),
        db: AsyncSession = Depends(get_db)
):
    filters = dict(request.query_params)
    filters.pop("skip", None)
    filters.pop("limit", None)

    crud = GenericCRUD(db, doctype)

    try:
        # Get data
        data_response = await crud.get_all(skip=skip, limit=limit, filters=filters)
        items = data_response["items"]
        if not items:
            raise HTTPException(status_code=404, detail="No data to export")

        # Load schema fieldnames
        schema = load_schema(doctype)
        schema_fields = [f["label"] for f in schema["fields"] if not f.get("hidden", False)]

        # Get all possible fields from data
        actual_fields = set()
        for item in items:
            actual_fields.update(item.keys())

        # Combine both schema + actual fields
        all_fields = list(set(schema_fields).union(actual_fields))

        # Prepare CSV
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=all_fields, extrasaction='ignore')
        writer.writeheader()

        for item in items:
            row = {}
            for field in all_fields:
                value = item.get(field)
                if isinstance(value, list):
                    value = ", ".join(map(str, value))  # convert list to CSV-safe string
                row[field] = value if value is not None else ""
            writer.writerow(row)

        response = Response(content=output.getvalue(), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={doctype}.csv"
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@router.post("/upload_files")
async def upload_files(files: List[UploadFile] = File(...)):
    BASE_UPLOAD_DIR = "static/uploads"
    os.makedirs(BASE_UPLOAD_DIR, exist_ok=True)

    filepaths = []

    for file in files:
        ext = os.path.splitext(file.filename)[1]  # get extension
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        unique_id = uuid.uuid4().hex[:6]  # add uniqueness
        new_filename = f"{timestamp}_{unique_id}{ext}"
        full_path = os.path.join(BASE_UPLOAD_DIR, new_filename)

        with open(full_path, "wb") as f:
            content = await file.read()
            f.write(content)

        relative_path = full_path.replace(os.sep, "/")  # for URL-friendly path
        filepaths.append(relative_path)

    return {"uploaded_paths": filepaths}


@router.get("/assets_info/{asset_id}")
async def get_asset_summary(
        asset_id: str,
        db: AsyncSession = Depends(get_db)
):
    query = """
    MATCH (a:assets {id: $asset_id})
    OPTIONAL MATCH (a)-[:HAS_CONTROL]->(sc:control)
    OPTIONAL MATCH (sc)-[:MITIGATES]->(t:threat)
    WITH a, head(collect(sc)) AS first_control, head(collect(t)) AS first_threat
    RETURN 
        a.type AS asset_type, 
        a.asset_value AS asset_value, 
        first_control.id AS security_control_id,
        first_threat.id AS threat_id
    """
    # first_threat.threat_name AS threat_name
    # MATCH (a:assets {id: $asset_id})
    # OPTIONAL MATCH (a)-[:HAS_CONTROL]->(sc:control)
    # RETURN a.type AS asset_type, a.asset_value AS asset_value, head(collect(sc.id)) AS security_control_id
    # """
    result = await db.run(query, asset_id=asset_id)
    record = await result.single()

    if not record:
        raise HTTPException(status_code=404, detail="Asset not found")

    # import pdb;pdb.set_trace()

    calculated_risk = await compute_threat_info(record["threat_id"], record["asset_value"], db)

    response = {
        "type": record["asset_type"],
        "asset_value": record["asset_value"],
        "associated_threats": record["threat_id"],
        "threat_probability": calculated_risk["likelihood"],
        "ease_of_exploitation": calculated_risk["ease_of_exploitation"],
        "related_vulnerabilities": calculated_risk["vulnerabilities"],
        "control_ids": calculated_risk["control_id"],
        "residual_risk": calculated_risk["risk"],
    }

    print("+++++++++++++++" ,response)

    return response
    # return {
    #     "asset_type": record["asset_type"],
    #     "asset_value": record["asset_value"]
    # }


@router.get("/threat_info/{threat_id}")
async def get_threat_info(threat_id: str ,asset_value: str = Query(...), db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, "threat")
    data = await crud.get_by_id(threat_id)

    if not data:
        raise HTTPException(status_code=404, detail="Threat not found")

    node = data.get("node", {})
    relationships = data.get("relationships", [])
    # print('+++++',relationships)

    # query = """
    # MATCH (a:assets {id: $asset_id})
    # RETURN a.type AS asset_type, a.criticality AS criticality_level
    # """
    # asset_value = "Unknown"
    # if asset_id:
    #     result = await db.run(query, asset_id=asset_id)
    #     record = await result.single()
    #     asset_value = record["criticality_level"]

    #     print('++++++++++++++++++asset value',asset_value)




    threat_name = node.get("threat_name")
    likelihood = node.get("likelihood")


    vulnerabilities = None
    control_name = None
    control_rating = None

    for rel in relationships:


        if rel["type"] == "CAUSES_THREAT":
            vuln = rel["node"]

            vulnerabilities = vuln.get("vulnerability_name")
        elif rel["type"] == "MITIGATES":
            ctrl = rel["node"]
            control_name = ctrl.get("control_id")
            print(control_name)
            control_rating = ctrl.get("rating")

    ease_map = {
        "High": "Low",
        "Medium": "Medium",
        "Low": "High"
    }
    ease_of_exploitation = ease_map.get(str(control_rating).strip(), "Unknown")


    RISK_MATRIX = {
        "Low": {
            "Low":   {"Low": "Low", "Medium": "Low", "High": "Medium"},
            "Medium": {"Low": "Low", "Medium": "Medium", "High": "Medium"},
            "High": {"Low": "Medium", "Medium": "Medium", "High": "High"},
        },
        "Medium": {
            "Low":   {"Low": "Low", "Medium": "Medium", "High": "Medium"},
            "Medium": {"Low": "Medium", "Medium": "Medium", "High": "Medium"},
            "High": {"Low": "Medium", "Medium": "High", "High": "Very High"},
        },
        "High": {
            "Low":   {"Low": "Medium", "Medium": "Medium", "High": "Medium"},
            "Medium": {"Low": "Medium", "Medium": "Medium", "High": "High"},
            "High": {"Low": "High", "Medium": "Very High", "High": "Very High"},
        }
    }

    try:
        risk = RISK_MATRIX[likelihood][asset_value][ease_of_exploitation]
    except KeyError:
        risk = "Unknown"


    return {
        "threat_name": threat_name,
        "likelihood": likelihood,
        "vulnerabilities": vulnerabilities,
        "control_id": control_name,
        "ease_of_exploitation": ease_of_exploitation,
        "risk": risk
    }




# @router.post("/bulk_upload/{doctype}")
# async def bulk_upload_nodes(
#     doctype: str,
#     file: UploadFile = File(...),
#     db: AsyncSession = Depends(get_db)
# ):
#     try:
#         contents = await file.read()

#         # Parse file
#         if file.filename.endswith(".csv"):
#             df = pd.read_csv(io.BytesIO(contents))
#         elif file.filename.endswith(".xlsx"):
#             df = pd.read_excel(io.BytesIO(contents))
#         else:
#             raise HTTPException(status_code=400, detail="Unsupported file format")

#         schema = load_schema(doctype)
#         field_map = {f["label"]: f["fieldname"] for f in schema["fields"]}
#         required_fields = [f["fieldname"] for f in schema["fields"] if f.get("required")]
                    # Special handling for threat: create MITIGATES relationship to relevant control(s)

#         linked_fields = [f["fieldname"] for f in schema["fields"] if f.get("fieldtype") in ["Link", "MultiLink"]]

#         crud = GenericCRUD(db, doctype)

#         created = []
#         errors = []


#         for index, row in df.iterrows():
#             try:
#                 # Step 1: Map label -> fieldname
#                 data = {field_map.get(k, k): v for k, v in row.items() if k in field_map}

#                 # Step 2: Validate required fields
#                 for field in required_fields:
#                     if not data.get(field):
#                         raise ValueError(f"Missing required field: {field}")

#                 # Step 3: Generate ID

#                 id_query = """
#                 MERGE (c:Counter {doctype: $doctype})
#                 ON CREATE SET c.current = 1
#                 ON MATCH SET c.current = c.current + 1
#                 RETURN c.current AS new_id
#                 """
#                 result = await db.run(id_query, doctype=doctype)
#                 record = await result.single()
#                 new_id = record["new_id"]
#                 data["id"] = f"{doctype.lower()}-{new_id}"


#                 now = datetime.utcnow().isoformat()
#                 data["created_at"] = now
#                 data["updated_at"] = now
#                 if field in linked_fields:
#                     quer = """MATCH (target:{rel["target_doctype"]} {{{field_key}: $val}})
#                     """





#                 # Save relationships for later creation
#                 relationships = []


#                 for field in schema["fields"]:
#                     if field.get("fieldtype") not in ["Link", "MultiLink"]:
#                         continue

#                     fname = field["fieldname"]
#                     if fname not in data or not data[fname]:
#                         continue

#                     target_doctype = field["link_to"]
#                     relationship_type = field.get("relationship_type", "RELATED_TO").upper()
#                     direction = field.get("relationship_direction", "outgoing")
#                     target_field = field.get("target_field","")

#                     values = data[fname]
#                     if not isinstance(values, list):
#                         values = [v.strip() for v in str(values).split(",") if v.strip()]

#                     # Store for later relation creation
#                     relationships.append({
#                         "fieldname": fname,
#                         "values": values,
#                         "target_doctype": target_doctype,
#                         "relationship_type": relationship_type,
#                         "direction": direction,
#                         "target_field":target_field
#                     })

#                     # Remove from node data to avoid saving as list string
#                     # del data[fname]

#                 # Step 4: Create node


#                 create_query = f"""
#                 CREATE (n:{doctype} $data)
#                 RETURN n
#                 """
#                 result = await db.run(create_query, data=data)
#                 record = await result.single()
#                 node = record["n"]

#                 # Step 5: Create relationships
#                 for rel in relationships:
#                     # import pdb;pdb.set_trace()
#                     field_key =rel["target_field"]

#                     for val in rel["values"]:
#                         if rel["direction"] == "incoming":
#                             relation_query = f"""
#                             MATCH (target:{rel["target_doctype"]} {{{field_key}: $val}})
#                             MATCH (source:{doctype} {{id: $source_id}})
#                             MERGE (target)-[:{rel["relationship_type"]}]->(source)
#                             """
#                         else:
#                             relation_query = f"""
#                             MATCH (target:{rel["target_doctype"]} {{{field_key}: $val}})
#                             MATCH (source:{doctype} {{id: $source_id}})
#                             MERGE (source)-[:{rel["relationship_type"]}]->(target)
#                             """
#                         await db.run(relation_query, val=val, source_id=data["id"])

#                 created.append(data["id"])

#             except Exception as e:
#                 errors.append({"row": index + 2, "error": str(e)})

#         return {
#             "created_count": len(created),
#             "failed_count": len(errors),
#             "created_ids": created,
#             "errors": errors
#         }

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to process file: {str(e)}")
import math
def sanitize_dict_values(obj, replace_with=None):
    if isinstance(obj, dict):
        return {k: sanitize_dict_values(v, replace_with) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_dict_values(i, replace_with) for i in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return replace_with
    return obj


@router.post("/bulk_upload/{doctype}")
async def bulk_upload_nodes(
        doctype: str,
        file: UploadFile = File(...),
        db: AsyncSession = Depends(get_db)
):
    try:
        contents = await file.read()

        if file.filename.endswith(".csv"):
            df = pd.read_csv(
                io.BytesIO(contents),
                dtype=str,
                keep_default_na=False,   # Prevents pandas from converting blanks or NA
                na_filter=False          # Keeps all text as-is (e.g. "5.10")
            )

        elif file.filename.endswith(".xlsx"):
            df = pd.read_excel(
                io.BytesIO(contents),
                dtype=str,
                keep_default_na=False,
                na_filter=False,
                engine="openpyxl"         # Ensures modern Excel support
            )

        else:
            raise HTTPException(status_code=400, detail="Unsupported file format")

        # Clean up whitespace
        df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)

        # Replace empty strings with None
        df = df.replace(r'^\s*$', None, regex=True)

        df = df.where(pd.notnull(df), None)  # Replace NaN with None

        schema = load_schema(doctype)
        field_map = {f["label"]: f["fieldname"] for f in schema["fields"]}
        required_fields = [f["fieldname"] for f in schema["fields"] if f.get("required")]

        created = []
        errors = []

        if doctype == "control_question":
            controls_data = {}
            for index, row in df.iterrows():
                control = row.get("Control", "")
                question = row.get("Questions", "").strip()
                weightage = row.get("Weightage", 1)

                if not control or not question:
                    continue

                try:
                    weightage = float(weightage)
                except (ValueError, TypeError):
                    weightage = 1.0

                if control not in controls_data:
                    controls_data[control] = []

                controls_data[control].append({
                    "question": question,
                    "wheightage": weightage
                })

            from services.db_CRUD import GenericCRUD
            crud = GenericCRUD(db, "control_question")

            for control_name, question_list in controls_data.items():
                try:
                    match_query = """
                    MATCH (c:control {control_id: $val})
                    RETURN c.id AS id
                    """
                    result = await db.run(match_query, val=control_name)
                    record = await result.single()
                    if not record:
                        raise ValueError(f"Control not found where control_name = '{control_name}'")

                    control_id = record["id"]

                    data = {
                        "control_id": control_id,
                        "question": question_list
                    }

                    created_node = await crud.create(data)
                    created.append(created_node["id"])

                except Exception as e:
                    errors.append({"control": control_name, "error": str(e)})

        else:
            for index, row in df.iterrows():
                try:
                    data = {field_map.get(k, k): v for k, v in row.items() if k in field_map}

                    for field in required_fields:
                        if not data.get(field):
                            raise ValueError(f"Missing required field: {field}")

                    id_query = """
                    MERGE (c:Counter {doctype: $doctype})
                    ON CREATE SET c.current = 1
                    ON MATCH SET c.current = c.current + 1
                    RETURN c.current AS new_id
                    """
                    result = await db.run(id_query, doctype=doctype)
                    record = await result.single()
                    new_id = record["new_id"]
                    data["id"] = f"{doctype.lower()}-{new_id}"

                    now = datetime.utcnow().isoformat()
                    data["created_at"] = now
                    data["updated_at"] = now


                    # Special handling for control: calculate ease_of_exploitation only if rating is present and not None
                    if doctype == "control" and data.get("rating") is not None:
                        ease_map = {
                            "High": "Low",
                            "Medium": "Medium",
                            "Low": "High"
                        }
                        ease_of_exploitation = ease_map.get(str(data["rating"]).strip(), "Unknown")
                        data["ease_of_exploitation"] = ease_of_exploitation
                        

                    # Special handling for vulnerability: set ease_of_exploitation from linked control's rating if available
                    if doctype == "vulnerability":
                        id = data.get("relevant_control_id")
                        if id:
                            # Try to fetch the control node's rating (fix property name in Cypher)
                            control_query = """
                                MATCH (c:control {control_id: $control_id})
                                RETURN c.rating AS rating
                            """
                            result = await db.run(control_query, control_id=id)
                            record = await result.single()
                            if record and record.get("rating"):
                                ease_map = {
                                    "High": "Low",
                                    "Medium": "Medium",
                                    "Low": "High"
                                }
                                rating_val = str(record["rating"]).strip()
                                ease_of_exploitation = ease_map.get(record.get("rating"), "High")
                                data["ease_of_exploitation"] = ease_of_exploitation

                    relationships = []

                    for field in schema["fields"]:
                        if field.get("fieldtype") == "MultiLink":
                            fname = field["fieldname"]
                            if fname in data and data[fname]:
                                data[fname] = [v.strip() for v in str(data[fname]).split(",") if v.strip()]

                    for field in schema["fields"]:
                        if field.get("fieldtype") not in ["Link", "MultiLink"]:
                            continue
                        fname = field["fieldname"]
                        if fname not in data or not data[fname]:
                            continue
                        target_doctype = field["link_to"]
                        target_field = field.get("target_field", "id")
                        raw_values = data[fname] if isinstance(data[fname], list) else [v.strip() for v in str(data[fname]).split(",") if v.strip()]

                        resolved_ids = []
                        for val in raw_values:
                            match_query = f"""
                            MATCH (n:{target_doctype} {{{target_field}: $val}})
                            RETURN n.id AS id
                            """
                            result = await db.run(match_query, val=val)
                            record = await result.single()
                            if not record:
                                raise ValueError(f"{target_doctype} not found where {target_field} = '{val}'")
                            resolved_ids.append(record["id"])

                        data[fname] = resolved_ids[0] if field["fieldtype"] == "Link" else resolved_ids

                        relationships.append({
                            "fieldname": fname,
                            "values": resolved_ids,
                            "target_doctype": target_doctype,
                            "relationship_type": field.get("relationship_type", "RELATED_TO"),
                            "direction": field.get("relationship_direction", "outgoing")
                        })

                    data = sanitize_dict_values(data, replace_with=None)

                    create_query = f"""
                    CREATE (n:{doctype} $data)
                    RETURN n
                    """
                    await db.run(create_query, data=data)

                    for rel in relationships:
                        for val in rel["values"]:
                            if rel["direction"] == "incoming":
                                query = f"""
                                MATCH (target:{rel["target_doctype"]} {{id: $val}})
                                MATCH (source:{doctype} {{id: $source_id}})
                                MERGE (target)-[:{rel["relationship_type"]}]->(source)
                                """
                            else:
                                query = f"""
                                MATCH (target:{rel["target_doctype"]} {{id: $val}})
                                MATCH (source:{doctype} {{id: $source_id}})
                                MERGE (source)-[:{rel["relationship_type"]}]->(target)
                                """
                            await db.run(query, val=val, source_id=data["id"])

                    if doctype == "assets":
                        from services.db_CRUD import GenericCRUD
                        crud = GenericCRUD(db, doctype)
                        await crud.create_risks_for_asset(data)

                    created.append(data["id"])

                except Exception as e:
                    errors.append({"row": index + 2, "error": str(e)})

        return {
            "created_count": len(created),
            "failed_count": len(errors),
            "created_ids": created,
            "errors": errors
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process file: {str(e)}")



async def compute_threat_info(threat_id: str, asset_value: str, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, "threat")
    data = await crud.get_by_id(threat_id)

    if not data:
        raise HTTPException(status_code=404, detail="Threat not found")

    node = data.get("node", {})
    relationships = data.get("relationships", [])

    threat_name = node.get("threat_name")
    likelihood = node.get("likelihood")

    vulnerabilities = None
    control_name = None
    control_rating = None

    for rel in relationships:
        if rel["type"] == "CAUSES_THREAT":
            vuln = rel["node"]
            vulnerabilities = vuln.get("vulnerability_name")
        elif rel["type"] == "MITIGATES":
            ctrl = rel["node"]
            control_name = ctrl.get("control_id")
            control_rating = ctrl.get("rating")

    ease_map = {
        "High": "Low",
        "Medium": "Medium",
        "Low": "High"
    }
    ease_of_exploitation = ease_map.get(str(control_rating).strip(), "Unknown")

    RISK_MATRIX = {
        "Low": {
            "Low":   {"Low": "Low", "Medium": "Low", "High": "Medium"},
            "Medium": {"Low": "Low", "Medium": "Medium", "High": "Medium"},
            "High": {"Low": "Medium", "Medium": "Medium", "High": "High"},
        },
        "Medium": {
            "Low":   {"Low": "Low", "Medium": "Medium", "High": "Medium"},
            "Medium": {"Low": "Medium", "Medium": "Medium", "High": "Medium"},
            "High": {"Low": "Medium", "Medium": "High", "High": "Very High"},
        },
        "High": {
            "Low":   {"Low": "Medium", "Medium": "Medium", "High": "Medium"},
            "Medium": {"Low": "Medium", "Medium": "Medium", "High": "High"},
            "High": {"Low": "High", "Medium": "Very High", "High": "Very High"},
        }
    }

    try:
        risk = RISK_MATRIX[likelihood][asset_value][ease_of_exploitation]
    except KeyError:
        risk = "Unknown"

    return {
        "threat_name": threat_name,
        "likelihood": likelihood,
        "vulnerabilities": vulnerabilities,
        "control_id": control_name,
        "ease_of_exploitation": ease_of_exploitation,
        "risk": risk
    }
    
    
# @app.post("/upload-control-questions-raw/")
# async def upload_control_questions_raw(file: UploadFile = File(...)):
#     """
#     Alternative approach using raw CSV parsing without pandas
#     """
#     import csv
    
#     if not file.filename.endswith('.csv'):
#         raise HTTPException(status_code=400, detail="File must be a CSV")
    
#     try:
#         contents = await file.read()
#         csv_string = contents.decode('utf-8')
        
#         # Parse CSV
#         csv_reader = csv.DictReader(io.StringIO(csv_string))
        
#         # Group data by control
#         controls_data = {}
        
#         for row in csv_reader:
#             control = row.get('Control', '').strip()
#             question = row.get('Questions', '').strip()
#             weightage = row.get('Weightage', '0')
            
#             # Skip empty controls or questions
#             if not control or not question:
#                 continue
            
#             # Convert weightage to float
#             try:
#                 weightage = float(weightage)
#             except (ValueError, TypeError):
#                 weightage = 0.0
            
#             # Initialize control if not exists
#             if control not in controls_data:
#                 controls_data[control] = []
            
#             controls_data[control].append({
#                 "question": question,
#                 "weightage": weightage
#             })
        
#         # Format the result
#         result = []
#         for control_id, questions in controls_data.items():
#             result.append({
#                 "control": control_id,
#                 "questions": questions,
#                 "control_questions_no": len(questions)
#             })
        
#         return JSONResponse(content=result)
        
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")  
