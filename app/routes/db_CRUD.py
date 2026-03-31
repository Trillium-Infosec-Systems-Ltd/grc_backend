from fastapi import APIRouter, Depends, HTTPException, Path, UploadFile,File,Request,Response
from fastapi.responses import FileResponse
from neo4j import AsyncSession
from services.dependencies import get_current_user
from services.db_CRUD import GenericCRUD
from services.schema_loader import load_schema


from services.database import get_db
from fastapi import Query
from neo4j import AsyncDriver
from typing import Optional, List
import json
import os 
from datetime import datetime
import csv
import io
import os



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

        where_str = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # Count query
        count_query = f"""
        MATCH (c:control)
        OPTIONAL MATCH (ca:control_assessment {{control_id: c.control_id, organization_id: $org_id}})
        {where_str}
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
        {where_str}
        RETURN c, ca, f.framework_name AS framework_name
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
        RETURN c, ca, f.framework_name AS framework_name
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
    filters = normalized_filters
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

    except Exception as e:
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
