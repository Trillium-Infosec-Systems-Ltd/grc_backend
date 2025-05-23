

from fastapi import APIRouter, Depends, HTTPException, Path, Response,Request
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
            columns.append({
                "title": field.get("label", field["fieldname"]),
                "dataIndex": field["fieldname"],
                "key": field["fieldname"]
            })

    return {
        "form_id": doctype,
        "columns": columns
    }



@router.get("/csv_template/{node_type}")
async def generate_csv_template(node_type: str):
    try:
        schema = load_schema(node_type)

        fieldnames = [
            f["label"] for f in schema["fields"]
            if not f.get("hidden", False)
        ]

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        response = Response(content=output.getvalue(), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={node_type}_template.csv"
        return response

    except Exception as e:
        return {"error": str(e)}
    






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
