from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile,File,Request,Response
from fastapi.responses import FileResponse, StreamingResponse
from neo4j import AsyncSession
from services.dependencies import get_current_user
from services.db_CRUD import GenericCRUD
from services.schema_loader import load_schema


from services.database import get_db
from fastapi import Query
from neo4j import AsyncDriver
from typing import Optional, List, Dict, Any, Set, Tuple
import json
import os 
import ast
from datetime import datetime
import csv
import io
import os
import pandas as pd
import re



router = APIRouter()


import math

def sanitize_for_json(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(i) for i in obj]
    return obj


def _serialize_export_value(value):
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return "" if value is None else value


def _rows_to_excel_response(rows: list, filename: str):
    output = io.BytesIO()
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="data")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}.xlsx"}
    )


def _rows_to_pdf_response(rows: list, filename: str):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="PDF export requires reportlab. Install it with: pip install reportlab"
        )

    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    page_width, page_height = A4
    y = page_height - 40

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(30, y, filename)
    y -= 24
    pdf.setFont("Helvetica", 9)

    for idx, row in enumerate(rows, start=1):
        row_text = f"{idx}. " + " | ".join(
            f"{k}: {_serialize_export_value(v)}" for k, v in row.items()
        )
        line_chunks = [row_text[i:i + 150] for i in range(0, len(row_text), 150)]
        for chunk in line_chunks:
            if y < 40:
                pdf.showPage()
                y = page_height - 40
                pdf.setFont("Helvetica", 9)
            pdf.drawString(30, y, chunk)
            y -= 14
        y -= 6

    pdf.save()
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}.pdf"}
    )


def _build_export_response(rows: list, filename: str, export_format: str):
    if export_format == "excel":
        return _rows_to_excel_response(rows, filename)
    if export_format == "pdf":
        return _rows_to_pdf_response(rows, filename)
    raise HTTPException(status_code=400, detail="format must be either 'excel' or 'pdf'")


def _normalize_file_list(value: Any) -> List[str]:
    def _extract_url(item: Any) -> str:
        if item is None:
            return ""
        if isinstance(item, dict):
            return str(item.get("url") or item.get("path") or "").strip()

        text = str(item).strip()
        if not text:
            return ""

        # Recover legacy values accidentally stored as stringified dicts.
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
    if isinstance(value, list):
        return [u for u in (_extract_url(v) for v in value) if u]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [u for u in (_extract_url(v) for v in parsed) if u]
            except Exception:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]
    single = _extract_url(value)
    return [single] if single else []


def _to_public_static_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip()
    if not normalized:
        return ""
    # Remove accidental API prefix from older values.
    if normalized.startswith("/api/"):
        normalized = normalized[4:]
    elif normalized.startswith("api/"):
        normalized = normalized[3:]
    # Handle malformed values like "apistatic/...".
    if normalized.startswith("apistatic/"):
        normalized = normalized[3:]
    elif normalized.startswith("/apistatic/"):
        normalized = normalized[4:]
    if normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if normalized.startswith("static/"):
        return f"/{normalized}"
    return f"/{normalized}"


def _normalize_evidence_paths(value: Any) -> List[str]:
    return [
        _to_public_static_path(v)
        for v in _normalize_file_list(value)
        if _to_public_static_path(v)
    ]


def _evidence_file_objects(value: Any) -> List[Dict[str, str]]:
    urls = _normalize_evidence_paths(value)
    return [{"name": os.path.basename(url), "url": url} for url in urls]


def _safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "").strip()) or "unknown"


def _safe_filename(filename: str) -> str:
    # Keep the user's filename while preventing path traversal and illegal separators.
    name = os.path.basename(str(filename or "").strip())
    name = name.replace("\\", "_").replace("/", "_")
    return name or "uploaded_file"


def _is_safe_neo4j_identifier(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value or ""))


async def _fetch_display_map(
    db: AsyncSession,
    label: str,
    target_field: str,
    ids: Set[str]
) -> Dict[str, Any]:
    if not ids or not _is_safe_neo4j_identifier(label) or not _is_safe_neo4j_identifier(target_field):
        return {}

    query = f"""
    MATCH (n:{label})
    WHERE n.id IN $ids
    RETURN n.id AS id,
           coalesce(n[$target_field], n.name, n.id) AS display
    """
    result = await db.run(query, ids=list(ids), target_field=target_field)
    records = await result.data()

    return {
        str(record.get("id")): record.get("display")
        for record in records
        if record.get("id") is not None
    }


async def _resolve_export_link_values(
    db: AsyncSession,
    doctype: str,
    rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not rows:
        return rows

    try:
        schema = load_schema(doctype)
    except Exception:
        return rows

    link_field_config: Dict[str, Tuple[str, str, bool]] = {}
    for field in schema.get("fields", []):
        fieldtype = field.get("fieldtype")
        if fieldtype not in {"Link", "MultiLink"}:
            continue

        fieldname = field.get("fieldname")
        link_to = field.get("link_to")
        target_field = field.get("target_field") or "name"
        is_multi = fieldtype == "MultiLink"

        if fieldname and link_to:
            link_field_config[fieldname] = (link_to, target_field, is_multi)

    if not link_field_config:
        return rows

    ids_by_target: Dict[Tuple[str, str], Set[str]] = {}
    for row in rows:
        for fieldname, (link_to, target_field, is_multi) in link_field_config.items():
            raw_value = row.get(fieldname)
            if raw_value in (None, ""):
                continue

            values: List[Any]
            if isinstance(raw_value, list):
                values = raw_value
            elif is_multi and isinstance(raw_value, str) and "," in raw_value:
                values = [item.strip() for item in raw_value.split(",") if item.strip()]
            else:
                values = [raw_value]

            bucket = ids_by_target.setdefault((link_to, target_field), set())
            for value in values:
                if value in (None, ""):
                    continue
                bucket.add(str(value))

    display_maps: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for key, ids in ids_by_target.items():
        link_to, target_field = key
        display_maps[key] = await _fetch_display_map(db, link_to, target_field, ids)

    transformed_rows: List[Dict[str, Any]] = []
    for row in rows:
        transformed = dict(row)
        for fieldname, (link_to, target_field, is_multi) in link_field_config.items():
            raw_value = transformed.get(fieldname)
            if raw_value in (None, ""):
                continue

            display_map = display_maps.get((link_to, target_field), {})
            if isinstance(raw_value, list):
                transformed[fieldname] = [display_map.get(str(v), v) for v in raw_value]
            elif is_multi and isinstance(raw_value, str) and "," in raw_value:
                items = [item.strip() for item in raw_value.split(",") if item.strip()]
                transformed[fieldname] = [display_map.get(str(v), v) for v in items]
            else:
                transformed[fieldname] = display_map.get(str(raw_value), raw_value)

        transformed_rows.append(transformed)

    return transformed_rows


# ============== COMPLAINCE ROUTES (must be before generic /data/{doctype} routes) ==============

@router.get("/data/complaince")
async def get_complaince_data(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get compliance data - controls merged with control_assessment for the current organization.
    This is a virtual view that combines control and control_assessment data.
    """
    filters = dict(request.query_params)
    filters.pop("skip", None)
    filters.pop("limit", None)

    org_id = current_user.get("org_id")
    print(f"[COMPLAINCE] Route hit! org_id={org_id}, filters={filters}")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    try:
        # Build filter conditions for controls
        where_clauses = []
        params = {"skip": skip, "limit": limit, "org_id": org_id}

        # Handle filters
        filter_idx = 0
        for key, value in filters.items():
            if key in ["control_id", "control_name", "category", "framework"]:
                param_key = f"filter_{filter_idx}"
                if key == "control_id":
                    where_clauses.append(f"toString(c.{key}) = ${param_key}")
                else:
                    where_clauses.append(f"toLower(toString(c.{key})) CONTAINS toLower(${param_key})")
                params[param_key] = str(value)
                filter_idx += 1
            elif key in ["compliance_status", "rating"]:
                # These come from control_assessment
                param_key = f"filter_{filter_idx}"
                if key == "compliance_status":
                    where_clauses.append(f"COALESCE(ca.control_compliance, 'Non Compliant') = ${param_key}")
                elif key == "rating":
                    where_clauses.append(f"COALESCE(ca.control_rating, 'Low') = ${param_key}")
                params[param_key] = str(value)
                filter_idx += 1

        filter_str = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # Count query
        count_query = f"""
        MATCH (c:control)
        OPTIONAL MATCH (ca:control_assessment {{control_id: c.control_id, organization_id: $org_id}})
        OPTIONAL MATCH (f:framework)-[:owns]->(c)
        WITH c, ca, coalesce(f.framework_name, c.framework) AS framework_name
        {filter_str}
        RETURN count(c) AS total
        """
        count_result = await db.run(count_query, **params)
        total = (await count_result.single())["total"]
        print(f"[COMPLAINCE] Total controls found: {total}")

        # Main data query - join controls with control_assessment for this org
        data_query = f"""
        MATCH (c:control)
        OPTIONAL MATCH (ca:control_assessment {{control_id: c.control_id, organization_id: $org_id}})
        OPTIONAL MATCH (f:framework)-[:owns]->(c)
        WITH c, ca, coalesce(f.framework_name, c.framework) AS framework_name
        {filter_str}
        RETURN c, ca, framework_name
        ORDER BY
            toInteger(split(toString(c.control_id), '.')[0]) ASC,
            CASE WHEN size(split(toString(c.control_id), '.')) > 1
                 THEN toInteger(split(toString(c.control_id), '.')[1])
                 ELSE 0 END ASC
        SKIP $skip
        LIMIT $limit
        """

        data_result = await db.run(data_query, **params)
        records = await data_result.data()
        print(f"[COMPLAINCE] Records returned: {len(records)}")

        items = []
        for record in records:
            control = dict(record["c"]) if record["c"] else {}
            assessment = dict(record["ca"]) if record["ca"] else {}

            # Merge control data with assessment data
            node = {
                "id": control.get("id"),
                "control_id": control.get("control_id"),
                "control_name": control.get("control_name"),
                "category": control.get("category"),
                "sub_category": control.get("sub_category"),
                "framework": record.get("framework_name"),
                "confidentiality": control.get("confidentiality", "No"),
                "integrity": control.get("integrity", "No"),
                "availability": control.get("availability", "No"),
                "control_applicable": assessment.get("control_applicable", control.get("control_applicable", "Yes")),
                "rating": assessment.get("control_rating", "Low"),
                "compliance_status": assessment.get("control_compliance", "Non Compliant"),
                "control_assessment": assessment.get("control_assessment"),
                "remarks": assessment.get("remarks", ""),
                "observation": assessment.get("observation", ""),
                "reference_evidence": assessment.get("reference_evidence", ""),
                "attached_files": _normalize_evidence_paths(assessment.get("attached_files", [])),
                "next_review_date": assessment.get("next_review_date", ""),
                "updated_at": assessment.get("updated_at") or control.get("updated_at"),
                "created_at": control.get("created_at")
            }

            items.append({
                "node": node,
                "relationships": []
            })

        return sanitize_for_json({
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": items
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/data/complaince/{item_id}")
async def get_complaince_item(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get a single compliance item (control with assessment data) by control ID.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    try:
        # Query to get control with its assessment for this org
        query = """
        MATCH (c:control {id: $item_id})
        OPTIONAL MATCH (ca:control_assessment {control_id: c.control_id, organization_id: $org_id})
        OPTIONAL MATCH (f:framework)-[:owns]->(c)
        RETURN c, ca, coalesce(f.framework_name, c.framework) AS framework_name
        """

        result = await db.run(query, item_id=item_id, org_id=org_id)
        record = await result.single()

        if not record or not record["c"]:
            raise HTTPException(status_code=404, detail="Item not found")

        control = dict(record["c"])
        assessment = dict(record["ca"]) if record["ca"] else {}

        node = {
            "id": control.get("id"),
            "control_id": control.get("control_id"),
            "control_name": control.get("control_name"),
            "category": control.get("category"),
            "sub_category": control.get("sub_category"),
            "framework": record.get("framework_name"),
            "confidentiality": control.get("confidentiality", "No"),
            "integrity": control.get("integrity", "No"),
            "availability": control.get("availability", "No"),
            "control_applicable": assessment.get("control_applicable", control.get("control_applicable", "Yes")),
            "rating": assessment.get("control_rating", "Low"),
            "compliance_status": assessment.get("control_compliance", "Non Compliant"),
            "control_assessment": assessment.get("control_assessment"),
            "remarks": assessment.get("remarks", ""),
            "observation": assessment.get("observation", ""),
            "reference_evidence": assessment.get("reference_evidence", ""),
            "attached_files": _normalize_evidence_paths(assessment.get("attached_files", [])),
            "next_review_date": assessment.get("next_review_date", ""),
            "updated_at": assessment.get("updated_at") or control.get("updated_at"),
            "created_at": control.get("created_at")
        }

        return {
            "node": node,
            "relationships": []
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/data/complaince/{item_id}")
async def update_complaince_item(
    item_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Update compliance data (control_assessment) for a control.
    This updates or creates the control_assessment node for the current organization.
    """
    # Use the existing control update logic which handles control_assessment
    crud = GenericCRUD(db, "control", current_user)

    # First get the control to get its control_id
    control = await crud.get_by_id(item_id)
    if not control:
        raise HTTPException(status_code=404, detail="Control not found")

    control_node = control.get("node", {})
    data["control_id"] = control_node.get("control_id")

    updated = await crud.update(item_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found or not updated")

    return updated


@router.post("/data/complaince/{item_id}/upload-evidence")
async def upload_complaince_evidence(
    item_id: str,
    files: List[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    control_query = """
    MATCH (c:control {id: $item_id})
    RETURN c.control_id AS control_id
    """
    control_result = await db.run(control_query, item_id=item_id)
    control_record = await control_result.single()
    if not control_record:
        raise HTTPException(status_code=404, detail="Control not found")

    control_id_value = str(control_record["control_id"]).strip()

    safe_org_id = _safe_slug(org_id)
    safe_control_id = _safe_slug(control_id_value)
    upload_dir = os.path.join("static", "uploads", "evidence", safe_org_id, safe_control_id)
    os.makedirs(upload_dir, exist_ok=True)

    allowed_extensions = {
        ".pdf", ".png", ".jpg", ".jpeg", ".csv", ".xlsx", ".xls", ".doc", ".docx", ".txt"
    }
    max_bytes = 10 * 1024 * 1024

    uploaded_paths: List[str] = []
    for upload in files:
        original_filename = _safe_filename(upload.filename)
        _, ext = os.path.splitext(original_filename)
        ext = ext.lower()
        if ext not in allowed_extensions:
            raise HTTPException(status_code=400, detail=f"Unsupported evidence file type: {ext or 'unknown'}")

        content = await upload.read()
        if len(content) > max_bytes:
            raise HTTPException(status_code=400, detail=f"File '{upload.filename}' exceeds 10MB limit")

        new_filename = original_filename
        full_path = os.path.join(upload_dir, new_filename)

        with open(full_path, "wb") as f:
            f.write(content)

        uploaded_paths.append(_to_public_static_path(full_path))

    assessment_query = """
    MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
    RETURN a
    """
    assessment_result = await db.run(
        assessment_query,
        control_id=control_id_value,
        organization_id=org_id
    )
    assessment_record = await assessment_result.single()

    now = datetime.utcnow().isoformat()
    existing_files: List[str] = []
    if assessment_record and assessment_record.get("a"):
        existing_files = _normalize_evidence_paths(assessment_record["a"].get("attached_files", []))

    merged_files = list(dict.fromkeys(existing_files + uploaded_paths))

    if not assessment_record:
        create_assessment_query = """
        CREATE (a:control_assessment {
            control_id: $control_id,
            organization_id: $organization_id,
            attached_files: $attached_files,
            control_compliance: 'Non Compliant',
            control_rating: 'Low',
            control_applicable: 'Yes',
            created_at: $now,
            updated_at: $now
        })
        RETURN a
        """
        await db.run(
            create_assessment_query,
            control_id=control_id_value,
            organization_id=org_id,
            attached_files=merged_files,
            now=now
        )
    else:
        update_assessment_query = """
        MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
        SET a.attached_files = $attached_files,
            a.updated_at = $updated_at
        RETURN a
        """
        await db.run(
            update_assessment_query,
            control_id=control_id_value,
            organization_id=org_id,
            attached_files=merged_files,
            updated_at=now
        )

    return {
        "organization_id": org_id,
        "control_node_id": item_id,
        "control_id": control_id_value,
        "uploaded_paths": uploaded_paths,
        "attached_files": _evidence_file_objects(merged_files)
    }


@router.get("/data/complaince/{item_id}/evidence")
async def get_complaince_evidence(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    control_query = """
    MATCH (c:control {id: $item_id})
    RETURN c.control_id AS control_id
    """
    control_result = await db.run(control_query, item_id=item_id)
    control_record = await control_result.single()
    if not control_record:
        raise HTTPException(status_code=404, detail="Control not found")

    control_id_value = str(control_record["control_id"]).strip()

    assessment_query = """
    MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
    RETURN a.attached_files AS attached_files
    """
    assessment_result = await db.run(
        assessment_query,
        control_id=control_id_value,
        organization_id=org_id
    )
    assessment_record = await assessment_result.single()

    attached_files = []
    if assessment_record:
        attached_files = _normalize_evidence_paths(assessment_record.get("attached_files", []))

    return {
        "organization_id": org_id,
        "control_node_id": item_id,
        "control_id": control_id_value,
        "evidence_count": len(attached_files),
        "attached_files": attached_files
    }


@router.delete("/data/complaince/{item_id}/evidence")
async def delete_complaince_evidence(
    item_id: str,
    evidence_path: str = Query(..., description="Exact evidence file path from attached_files"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    control_query = """
    MATCH (c:control {id: $item_id})
    RETURN c.control_id AS control_id
    """
    control_result = await db.run(control_query, item_id=item_id)
    control_record = await control_result.single()
    if not control_record:
        raise HTTPException(status_code=404, detail="Control not found")

    control_id_value = str(control_record["control_id"]).strip()

    assessment_query = """
    MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
    RETURN a
    """
    assessment_result = await db.run(
        assessment_query,
        control_id=control_id_value,
        organization_id=org_id
    )
    assessment_record = await assessment_result.single()

    if not assessment_record or not assessment_record.get("a"):
        raise HTTPException(status_code=404, detail="No compliance assessment found for this control and organization")

    existing_files = _normalize_evidence_paths(assessment_record["a"].get("attached_files", []))
    requested_path = _to_public_static_path(evidence_path)
    if requested_path not in existing_files:
        raise HTTPException(status_code=404, detail="Evidence path not found for this control and organization")

    updated_files = [p for p in existing_files if p != requested_path]
    now = datetime.utcnow().isoformat()

    update_assessment_query = """
    MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
    SET a.attached_files = $attached_files,
        a.updated_at = $updated_at
    RETURN a
    """
    await db.run(
        update_assessment_query,
        control_id=control_id_value,
        organization_id=org_id,
        attached_files=updated_files,
        updated_at=now
    )

    deleted_from_disk = False
    safe_delete_root = os.path.abspath(os.path.join("static", "uploads", "evidence"))
    candidate_rel_path = requested_path.lstrip("/")
    candidate_abs_path = os.path.abspath(candidate_rel_path)

    # Only allow deleting evidence files under the evidence upload directory.
    if candidate_rel_path.startswith("static/uploads/evidence/") and os.path.commonpath([safe_delete_root, candidate_abs_path]) == safe_delete_root:
        if os.path.isfile(candidate_abs_path):
            os.remove(candidate_abs_path)
            deleted_from_disk = True

    return {
        "organization_id": org_id,
        "control_node_id": item_id,
        "control_id": control_id_value,
        "deleted_path": requested_path,
        "deleted_from_disk": deleted_from_disk,
        "attached_files": updated_files
    }


@router.get("/dashboard/compliance")
async def get_compliance_dashboard_graph(
    framework: Optional[str] = Query(None),
    framework_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get compliance graph data for dashboard.
    Returns framework options and compliance split for selected framework.
    If no framework is selected, the first framework in the list is used.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    try:
        frameworks_query = """
        MATCH (f:framework)
        RETURN DISTINCT f.id AS id, f.framework_name AS framework_name
        ORDER BY toLower(f.framework_name) ASC
        """
        frameworks_result = await db.run(frameworks_query)
        frameworks_records = await frameworks_result.data()

        frameworks = [
            {
                "id": record.get("id"),
                "label": record.get("framework_name"),
                "value": record.get("id")
            }
            for record in frameworks_records
            if record.get("id") and record.get("framework_name")
        ]

        if not frameworks:
            return {
                "frameworks": [],
                "selected_framework": None,
                "data": [
                    {"name": "Compliant", "value": 0},
                    {"name": "Non-Compliant", "value": 0},
                    {"name": "Partially Compliant", "value": 0}
                ]
            }

        requested_framework = framework or framework_id
        framework_ids = {item["id"] for item in frameworks}
        selected_framework_id = requested_framework if requested_framework in framework_ids else frameworks[0]["id"]
        selected_framework = next(
            (item for item in frameworks if item["id"] == selected_framework_id),
            frameworks[0]
        )

        counts_query = """
        MATCH (c:control)
        WHERE
            EXISTS {
                MATCH (f:framework {id: $framework_id})-[:owns]->(c)
            }
            OR toLower(trim(toString(c.framework))) = toLower(trim($framework_id))
            OR toLower(trim(toString(c.framework))) = toLower(trim($framework_name))
        OPTIONAL MATCH (ca:control_assessment {control_id: c.control_id, organization_id: $org_id})
        WITH CASE
            WHEN ca.control_compliance = 'Compliant' THEN 'Compliant'
            WHEN ca.control_compliance IN ['Partially Compliant', 'Partially-Compliant'] THEN 'Partially Compliant'
            ELSE 'Non-Compliant'
        END AS compliance_status
        RETURN compliance_status, count(*) AS value
        """
        counts_result = await db.run(
            counts_query,
            framework_id=selected_framework_id,
            framework_name=selected_framework["label"],
            org_id=org_id
        )
        counts_records = await counts_result.data()

        counts_map = {
            "Compliant": 0,
            "Non-Compliant": 0,
            "Partially Compliant": 0
        }
        for row in counts_records:
            status = row.get("compliance_status")
            if status in counts_map:
                counts_map[status] = row.get("value", 0)

        return sanitize_for_json({
            "frameworks": frameworks,
            "selected_framework": selected_framework,
            "data": [
                {"name": "Compliant", "value": counts_map["Compliant"]},
                {"name": "Non-Compliant", "value": counts_map["Non-Compliant"]},
                {"name": "Partially Compliant", "value": counts_map["Partially Compliant"]}
            ]
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/risks-by-asset-category")
@router.get("/dashboard/risk-by-asset-category")
async def get_risks_by_asset_category_graph(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get dashboard graph data for risks grouped by asset category.
    Returns each category with Low/Medium/High/Very High counts.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    try:
        query = """
        MATCH (r:risks)
        WHERE r.organization_id = $org_id
          AND (r.is_deleted IS NULL OR toLower(toString(r.is_deleted)) <> 'true')
        WITH
            CASE
                WHEN toLower(trim(toString(r.residual_risk))) = 'low' THEN 'Low'
                WHEN toLower(trim(toString(r.residual_risk))) = 'medium' THEN 'Medium'
                WHEN toLower(trim(toString(r.residual_risk))) = 'high' THEN 'High'
                WHEN toLower(trim(toString(r.residual_risk))) IN ['very high', 'very_high', 'veryhigh'] THEN 'Very High'
                ELSE NULL
            END AS risk_level,
            coalesce(nullif(trim(toString(r.type)), ''), 'Uncategorized') AS raw_category
        OPTIONAL MATCH (at:asset_type {id: raw_category})
        WITH coalesce(at.type_name, raw_category) AS category_name, risk_level
        RETURN
            category_name AS name,
            sum(CASE WHEN risk_level = 'Low' THEN 1 ELSE 0 END) AS Low,
            sum(CASE WHEN risk_level = 'Medium' THEN 1 ELSE 0 END) AS Medium,
            sum(CASE WHEN risk_level = 'High' THEN 1 ELSE 0 END) AS High,
            sum(CASE WHEN risk_level = 'Very High' THEN 1 ELSE 0 END) AS `Very High`
        ORDER BY toLower(category_name) ASC
        """

        result = await db.run(query, org_id=org_id)
        rows = await result.data()

        return sanitize_for_json({
            "RISK_BY_ASSET_CATEGORY": rows
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/risks-by-status")
@router.get("/dashboard/risk-by-status")
async def get_risk_by_status_graph(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Get dashboard graph data for risks grouped by residual risk status.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    try:
        query = """
        MATCH (r:risks)
        WHERE r.organization_id = $org_id
          AND (r.is_deleted IS NULL OR toLower(toString(r.is_deleted)) <> 'true')
        WITH CASE
            WHEN toLower(trim(toString(r.residual_risk))) = 'low' THEN 'Low'
            WHEN toLower(trim(toString(r.residual_risk))) = 'medium' THEN 'Medium'
            WHEN toLower(trim(toString(r.residual_risk))) = 'high' THEN 'High'
            WHEN toLower(trim(toString(r.residual_risk))) IN ['very high', 'very_high', 'veryhigh'] THEN 'Very High'
            ELSE NULL
        END AS risk_level
        RETURN risk_level, count(*) AS value
        """

        result = await db.run(query, org_id=org_id)
        rows = await result.data()

        counts_map = {
            "Low": 0,
            "Medium": 0,
            "High": 0,
            "Very High": 0
        }

        for row in rows:
            status = row.get("risk_level")
            if status in counts_map:
                counts_map[status] = row.get("value", 0)

        return sanitize_for_json({
            "RISK_BY_STATUS": [
                {"name": "Low", "value": counts_map["Low"]},
                {"name": "Medium", "value": counts_map["Medium"]},
                {"name": "High", "value": counts_map["High"]},
                {"name": "Very High", "value": counts_map["Very High"]}
            ]
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/export/risks")
async def export_risks_data(
    format: str = Query("excel"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    query = """
    MATCH (r:risks)
    WHERE r.organization_id = $org_id
      AND (r.is_deleted IS NULL OR toLower(toString(r.is_deleted)) <> 'true')
    RETURN r
    ORDER BY coalesce(r.created_at, '') DESC
    """
    result = await db.run(query, org_id=org_id)
    records = await result.data()
    rows = [sanitize_for_json(dict(record.get("r") or {})) for record in records]
    rows = await _resolve_export_link_values(db, "risks", rows)

    if not rows:
        raise HTTPException(status_code=404, detail="No risks found to export")

    normalized_rows = [
        {k: _serialize_export_value(v) for k, v in row.items()}
        for row in rows
    ]
    return _build_export_response(normalized_rows, "risks_export", format.lower())


@router.get("/dashboard/export/framework-controls")
async def export_framework_controls_data(
    framework: Optional[str] = Query(None),
    framework_id: Optional[str] = Query(None),
    format: str = Query("excel"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    frameworks_query = """
    MATCH (f:framework)
    RETURN DISTINCT f.id AS id, f.framework_name AS framework_name
    ORDER BY toLower(f.framework_name) ASC
    """
    frameworks_result = await db.run(frameworks_query)
    frameworks_records = await frameworks_result.data()

    frameworks = [
        {
            "id": rec.get("id"),
            "label": rec.get("framework_name")
        }
        for rec in frameworks_records
        if rec.get("id") and rec.get("framework_name")
    ]

    if not frameworks:
        raise HTTPException(status_code=404, detail="No frameworks found")

    requested_framework = framework or framework_id
    framework_ids = {item["id"] for item in frameworks}
    selected_framework_id = requested_framework if requested_framework in framework_ids else frameworks[0]["id"]
    selected_framework = next(item for item in frameworks if item["id"] == selected_framework_id)

    controls_query = """
    MATCH (c:control)
    WHERE
        EXISTS {
            MATCH (f:framework {id: $framework_id})-[:owns]->(c)
        }
        OR toLower(trim(toString(c.framework))) = toLower(trim($framework_id))
        OR toLower(trim(toString(c.framework))) = toLower(trim($framework_name))
    OPTIONAL MATCH (ca:control_assessment {control_id: c.control_id, organization_id: $org_id})
    RETURN c,
           coalesce(ca.control_compliance, 'Non Compliant') AS compliance_status,
           coalesce(ca.control_rating, 'Low') AS rating
    ORDER BY
        toInteger(split(toString(c.control_id), '.')[0]) ASC,
        CASE WHEN size(split(toString(c.control_id), '.')) > 1
             THEN toInteger(split(toString(c.control_id), '.')[1])
             ELSE 0 END ASC
    """
    controls_result = await db.run(
        controls_query,
        framework_id=selected_framework_id,
        framework_name=selected_framework["label"],
        org_id=org_id
    )
    controls_records = await controls_result.data()

    rows = []
    for record in controls_records:
        control = dict(record.get("c") or {})
        control["framework_id"] = selected_framework_id
        control["framework_name"] = selected_framework["label"]
        control["compliance_status"] = record.get("compliance_status")
        control["rating"] = record.get("rating")
        rows.append(sanitize_for_json(control))

    if not rows:
        raise HTTPException(status_code=404, detail="No controls found for selected framework")

    normalized_rows = [
        {k: _serialize_export_value(v) for k, v in row.items()}
        for row in rows
    ]
    safe_framework = selected_framework_id.replace(" ", "_")
    filename = f"framework_controls_{safe_framework}"
    return _build_export_response(normalized_rows, filename, format.lower())


# ============== GENERIC CRUD ROUTES ==============

@router.post("/data/{doctype}")
async def create_item(
    doctype: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    crud = GenericCRUD(db, doctype, current_user)
    try:
        result = await crud.create(data)
        if not result:
            raise HTTPException(status_code=500, detail="Failed to create item")
        if doctype == "control_question":
            return result
        return result["n"]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

@router.get("/data/{doctype}")
async def get_all_items(
    doctype: str,
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    
    
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
    ):
    
        
    filters = dict(request.query_params)
    if doctype == "control_question" and filters.get("control_id"):
        filters["control"] = filters.pop("control_id")
        filters['control'] = filters['control'].strip('\'"')
    filters.pop("skip", None)
    filters.pop("limit", None)
    
    # Handle array-style parameters like related_vulnerabilities[]=value
    # Convert keys like "related_vulnerabilities[]" to "related_vulnerabilities"
    normalized_filters = {}
    for key, value in filters.items():
        clean_key = key.rstrip("[]")
        if clean_key in normalized_filters:
            # Append to existing list
            existing = normalized_filters[clean_key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                normalized_filters[clean_key] = [existing, value]
        else:
            normalized_filters[clean_key] = value
    # Remove empty string filters to avoid spurious WHERE clauses
    filters = {k: v for k, v in normalized_filters.items() if v is not None and v != ""}
    print(f"[get_all_items] doctype={doctype}, filters={filters}")

    crud = GenericCRUD(db, doctype)
    try:
        paginated_data = await crud.get_all(
            skip=skip,
            limit=limit,
            filters=filters,
            current_user=current_user
        )
        if not paginated_data["items"]:
            raise HTTPException(status_code=404, detail="No items found")

        return sanitize_for_json(paginated_data)

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/data/{doctype}/{item_id}")
async def get_item(doctype: str, item_id: str, db: AsyncSession = Depends(get_db)):
    crud = GenericCRUD(db, doctype)
    item = await crud.get_by_id(item_id)
    if not item:
        raise HTTPException(404, detail="Item not found")
    return item

@router.put("/data/{doctype}/{item_id}")
async def update_item(doctype: str, item_id: str, data: dict, db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    crud = GenericCRUD(db, doctype, current_user)
    updated = await crud.update(item_id, data)
    if not updated:
        raise HTTPException(404, detail="Item not found or not updated")
    return updated

@router.delete("/data/{doctype}")
async def delete_items(
    doctype: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Delete one or multiple items by IDs.
    Expects: {"ids": ["item-1", "item-2", ...]} or {"ids": ["item-1"]}
    For risks: Only super_admin can delete, and no relationship check is performed.
    For other doctypes: Relationship check is performed for each item.
    """
    item_ids = data.get("ids", [])
    if not item_ids:
        raise HTTPException(status_code=400, detail="No item IDs provided")

    crud = GenericCRUD(db, doctype, current_user)

    # Special handling for risks: only super_admin, no relationship check
    if doctype == "risks":
        user_role = current_user.get("role", "").lower()
        if user_role != "super_admin":
            raise HTTPException(
                status_code=403,
                detail="Only super_admin can delete risks"
            )
        
        count = await crud.delete_bulk(item_ids)
        if not count:
            raise HTTPException(status_code=404, detail="No risks found with the provided IDs")
        return {"detail": f"Deleted {count} risk(s) successfully", "deleted_count": count}

    # For other doctypes: check relationships before deleting
    deleted_count = 0
    failed_items = []

    for item_id in item_ids:
        item = await crud.get_by_id(item_id)
        if not item:
            failed_items.append({"id": item_id, "reason": "Item not found"})
            continue

        relationships = item.get("relationships") or []
        # filter out empty/null relationship entries
        relationships = [r for r in relationships if r and r.get("node")]
        
        if relationships:
            first = relationships[0]
            node = first.get("node") or {}

            # Prefer a human-friendly name, fall back to schema-defined label field, then doctype.
            linked_name = node.get("name")
            linked_id = node.get("id")
            linked_doctype = None
            if linked_id and isinstance(linked_id, str) and "-" in linked_id:
                linked_doctype = linked_id.split("-")[0]
            where_desc = linked_doctype or "another item"

            # If node has a friendly name property, use it first
            if linked_name:
                target_desc = f"{where_desc} '{linked_name}'"
            else:
                # Try to load the doctype schema and pick a display field
                friendly_value = None
                if linked_doctype:
                    try:
                        schema = load_schema(linked_doctype)
                        # 1) field with default_label
                        for f in schema.get("fields", []):
                            if f.get("default_label"):
                                fieldname = f.get("fieldname")
                                if node.get(fieldname):
                                    friendly_value = node.get(fieldname)
                                    break
                        # 2) first display_on_frontend
                        if not friendly_value:
                            for f in schema.get("fields", []):
                                if f.get("display_on_frontend"):
                                    fieldname = f.get("fieldname")
                                    if node.get(fieldname):
                                        friendly_value = node.get(fieldname)
                                        break
                        # 3) any field from schema that exists on node
                        if not friendly_value:
                            for f in schema.get("fields", []):
                                fieldname = f.get("fieldname")
                                if node.get(fieldname):
                                    friendly_value = node.get(fieldname)
                                    break
                    except Exception:
                        friendly_value = None

                if friendly_value:
                    target_desc = f"{where_desc} '{friendly_value}'"
                else:
                    target_desc = where_desc

            failed_items.append({
                "id": item_id,
                "reason": f"Cannot delete because it is being used by {target_desc}"
            })
            continue

        count = await crud.delete(item_id)
        if count:
            deleted_count += 1
        else:
            failed_items.append({"id": item_id, "reason": "Failed to delete"})

    if deleted_count == 0 and failed_items:
        raise HTTPException(status_code=400, detail={"message": "No items deleted", "failed": failed_items})

    return {
        "detail": f"Deleted {deleted_count} item(s) successfully",
        "deleted_count": deleted_count,
        "failed": failed_items if failed_items else None
    }


@router.delete("/data/{doctype}/all")
async def delete_all_items(
    doctype: str,
    db: AsyncSession = Depends(get_db),

):
    """
    Delete ALL items of a specific doctype. Only super_admin can perform this action.
    This will delete all nodes and their relationships for the given doctype.
    """
    # Only super_admin can delete all items

    crud = GenericCRUD(db, doctype)
    count = await crud.delete_all()

    if not count:
        raise HTTPException(404, detail=f"No items found for doctype '{doctype}'")

    return {"detail": f"Deleted all {count} {doctype} items successfully", "deleted_count": count}


# ---------------------------------------------------------------------------
# Question response endpoint
# ---------------------------------------------------------------------------

class QuestionResponsePayload(dict):
    """Thin wrapper — accepts any dict with answer/evidence/remarks."""
    pass


@router.post("/question/{question_id}/response")
async def upsert_question_response(
    question_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Save a per-organization answer for a single canonical question node and
    return the recomputed compliance status for every control that is linked
    to that question.

    Request body:
        {
            "answer": true | false,
            "evidence": "optional string",
            "remarks":  "optional string"
        }

    Response:
        {
            "question_id": "...",
            "answer": true,
            "affected_controls": [
                {"control_id": "A.5.1", "control_node_id": "control-12", "compliance": "Compliant"},
                ...
            ]
        }
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    answer_val = bool(body.get("answer", False))
    evidence = str(body.get("evidence", ""))
    remarks = str(body.get("remarks", ""))
    now = datetime.utcnow().isoformat()

    try:
        # Verify the question exists
        q_check = await db.run(
            "MATCH (q:question {id: $qid}) RETURN q.id AS qid",
            qid=question_id,
        )
        q_rec = await q_check.single()
        if not q_rec:
            raise HTTPException(status_code=404, detail=f"Question '{question_id}' not found")

        # Upsert question_response node
        await db.run(
            """
            MERGE (r:question_response {question_id: $qid, organization_id: $org_id})
            ON CREATE SET
                r.id         = 'qr-' + toString(id(r)),
                r.answer     = $answer,
                r.evidence   = $evidence,
                r.remarks    = $remarks,
                r.created_at = $now,
                r.updated_at = $now
            ON MATCH SET
                r.answer     = $answer,
                r.evidence   = $evidence,
                r.remarks    = $remarks,
                r.updated_at = $now
            WITH r
            MATCH (q:question {id: $qid})
            MERGE (q)-[:HAS_RESPONSE]->(r)
            """,
            qid=question_id,
            org_id=org_id,
            answer=answer_val,
            evidence=evidence,
            remarks=remarks,
            now=now,
        )

        # Find all controls linked to this question
        affected_result = await db.run(
            """
            MATCH (c:control)-[:HAS_QUESTION]->(q:question {id: $qid})
            RETURN DISTINCT c.id AS ctrl_node_id, c.control_id AS ctrl_id_val
            """,
            qid=question_id,
        )
        affected_controls_raw = await affected_result.data()

        crud = GenericCRUD(db, "control", current_user=current_user)
        affected_controls = []

        for ctrl in affected_controls_raw:
            ctrl_node_id = ctrl["ctrl_node_id"]
            ctrl_id_val = ctrl["ctrl_id_val"]
            if not ctrl_id_val or not ctrl_node_id:
                continue

            new_compliance = await crud.recompute_control_compliance(ctrl_id_val, org_id)
            print(f"[QRESP] Control {ctrl_id_val} recomputed => {new_compliance} for org {org_id}")

            if new_compliance == "Compliant":
                await crud._handle_compliant_control_trigger(ctrl_node_id, org_id)
            else:
                await crud._handle_non_compliant_control_trigger(ctrl_node_id, org_id)

            affected_controls.append({
                "control_id": ctrl_id_val,
                "control_node_id": ctrl_node_id,
                "compliance": new_compliance,
            })

        return sanitize_for_json({
            "question_id": question_id,
            "answer": answer_val,
            "affected_controls": affected_controls,
        })

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/question/{question_id}/response")
async def get_question_response(
    question_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get the current per-organization response for a question, plus which
    controls it is linked to and their current compliance status.
    """
    org_id = current_user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found in user token")

    try:
        result = await db.run(
            """
            MATCH (q:question {id: $qid})
            OPTIONAL MATCH (q)-[:HAS_RESPONSE]->(r:question_response {organization_id: $org_id})
            OPTIONAL MATCH (c:control)-[:HAS_QUESTION]->(q)
            OPTIONAL MATCH (ca:control_assessment {control_id: c.control_id, organization_id: $org_id})
            RETURN
                q.id AS question_id,
                q.text AS text,
                q.weight AS weight,
                q.iso_control_id AS iso_control_id,
                r.answer AS answer,
                r.evidence AS evidence,
                r.remarks AS remarks,
                collect(DISTINCT {
                    control_id: c.control_id,
                    control_node_id: c.id,
                    control_name: c.control_name,
                    framework: c.framework,
                    compliance: coalesce(ca.control_compliance, 'Non Compliant')
                }) AS linked_controls
            """,
            qid=question_id,
            org_id=org_id,
        )
        record = await result.single()
        if not record:
            raise HTTPException(status_code=404, detail=f"Question '{question_id}' not found")

        return sanitize_for_json({
            "question_id": record["question_id"],
            "text": record["text"],
            "weight": float(record["weight"] or 1.0),
            "iso_control_id": record["iso_control_id"],
            "answer": bool(record["answer"]) if record["answer"] is not None else False,
            "evidence": record["evidence"] or "",
            "remarks": record["remarks"] or "",
            "linked_controls": [c for c in (record["linked_controls"] or []) if c.get("control_id")],
        })

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/backfill-control-framework")
async def backfill_control_framework(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    One-time backfill: sets c.framework from the [:owns] relationship for all
    control nodes missing the property, and creates [:owns] for controls that
    have the property but no relationship. Safe to call multiple times.
    """
    try:
        # Step 1: copy framework_name from relationship -> property where missing
        r1 = await db.run("""
            MATCH (f:framework)-[:owns]->(c:control)
            WHERE c.framework IS NULL OR c.framework = ''
            SET c.framework = f.framework_name
            RETURN count(c) AS updated
        """)
        rec1 = await r1.single()
        updated_from_rel = rec1["updated"] if rec1 else 0

        # Step 2: create [:owns] for controls that have the property but no rel
        r2 = await db.run("""
            MATCH (c:control)
            WHERE c.framework IS NOT NULL AND c.framework <> ''
            MATCH (f:framework {framework_name: c.framework})
            WHERE NOT (f)-[:owns]->(c)
            MERGE (f)-[:owns]->(c)
            RETURN count(c) AS linked
        """)
        rec2 = await r2.single()
        linked = rec2["linked"] if rec2 else 0

        return {
            "controls_updated_from_relationship": updated_from_rel,
            "controls_linked_to_framework": linked,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
