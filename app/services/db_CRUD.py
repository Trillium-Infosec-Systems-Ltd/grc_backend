# this is a comment
from neo4j import AsyncSession
from services.schema_loader import load_schema
import uuid
from neo4j import AsyncDriver
import json
import ast
from datetime import datetime
from typing import Any, Dict
import json
from fastapi import APIRouter, Depends
from services.database import get_db
import json
from fastapi.responses import JSONResponse

import os
import re as _re


def _normalize_question_text(text: str) -> str:
    """Lower-case + collapse whitespace for stable question dedup key."""
    return _re.sub(r"\s+", " ", str(text or "").strip().lower())


def control_order_clause(alias: str = "n") -> str:
    """Return a Cypher ORDER BY clause that naturally sorts control IDs.

    Handles both ISO 27002 style (5.1, 5.2, 5.10) and ISO 27001 style
    (A.5.1, A.5.2, A.5.10) IDs so the minor number is ordered numerically.
    """
    cid = f"toString({alias}.control_id)"
    return f"""ORDER BY
        CASE WHEN {cid} =~ '^[A-Za-z].*' THEN left({cid}, 1) ELSE '' END ASC,
        coalesce(toInteger(CASE WHEN {cid} =~ '^[A-Za-z].*' THEN split({cid}, '.')[1] ELSE split({cid}, '.')[0] END), 0) ASC,
        coalesce(toInteger(CASE WHEN {cid} =~ '^[A-Za-z].*' AND size(split({cid}, '.')) > 2 THEN split({cid}, '.')[2] WHEN NOT ({cid} =~ '^[A-Za-z].*') AND size(split({cid}, '.')) > 1 THEN split({cid}, '.')[1] ELSE '0' END), 0) ASC,
        coalesce(toInteger(CASE WHEN {cid} =~ '^[A-Za-z].*' AND size(split({cid}, '.')) > 3 THEN split({cid}, '.')[3] WHEN NOT ({cid} =~ '^[A-Za-z].*') AND size(split({cid}, '.')) > 2 THEN split({cid}, '.')[2] ELSE '0' END), 0) ASC"""


def _derive_compliance(answered: int, total: int, weighted_yes: float, weighted_total: float) -> str:
    """
    Compute a control's compliance status from its question responses.
      - weighted_yes / weighted_total >= 1.0  => Compliant
      - weighted_yes / weighted_total >  0.0  => Partially Compliant
      - otherwise                             => Non Compliant
    Falls back to simple answered/total when weights are zero.
    """
    if total == 0:
        return "Non Compliant"
    if weighted_total <= 0:
        if answered == total:
            return "Compliant"
        if answered > 0:
            return "Partially Compliant"
        return "Non Compliant"
    ratio = weighted_yes / weighted_total
    if ratio >= 1.0:
        return "Compliant"
    if ratio > 0.0:
        return "Partially Compliant"
    return "Non Compliant"



def _normalize_file_list(value):
    def _extract_url(item):
        if item is None:
            return ""
        if isinstance(item, dict):
            return str(item.get("url") or item.get("path") or "").strip()

        text = str(item).strip()
        if not text:
            return ""

        # Recover legacy values sent as stringified dicts.
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


def _file_objects(value):
    """Return attached files as [{name, url}] objects (name = basename only)."""
    urls = _normalize_file_list(value)
    return [{"name": os.path.basename(url), "url": url} for url in urls]


class GenericCRUD:
    def __init__(self, session: AsyncSession, doctype: str, current_user: dict = None   ):
        self.session = session
        self.schema = load_schema(doctype)
        self.doctype = doctype
        self.current_user = current_user

    async def create_risks_for_asset(self, asset_data: dict):
        """
        Given an asset node data, create risks based on the relationship chain:
        Asset -> Controls -> Vulnerabilities -> Threats
        Creates a separate risk for each unique (asset, control, vulnerability, threat) combination.
        """
        security_controls = asset_data.get("security_controls", [])
        if isinstance(security_controls, str):
            security_controls = [security_controls]

        if not security_controls:
            return

        org_id = asset_data.get("organization_id")

        # Query to get all combinations: control -> vulnerability -> threat
        # for the given controls attached to this asset
        # Only include active (not soft-deleted) vulnerabilities and threats
        # For global entities (no org_id), also check deleted_for_orgs array
        chain_query = """
        MATCH (c:control)-[:MITIGATES]->(v:vulnerability)-[:CAUSES_THREAT]->(t:threat)
        WHERE c.id IN $control_ids
        AND (
            (v.organization_id IS NOT NULL AND (v.is_deleted IS NULL OR v.is_deleted = 'false'))
            OR
            (v.organization_id IS NULL AND (v.deleted_for_orgs IS NULL OR NOT $org_id IN v.deleted_for_orgs))
        )
        AND (
            (t.organization_id IS NOT NULL AND (t.is_deleted IS NULL OR t.is_deleted = 'false'))
            OR
            (t.organization_id IS NULL AND (t.deleted_for_orgs IS NULL OR NOT $org_id IN t.deleted_for_orgs))
        )
        RETURN c.id AS control_id,
               c.rating AS control_rating,
               v.id AS vulnerability_id,
               v.vulnerability_name AS vulnerability_name,
               v.ease_of_exploitation AS vuln_ease,
               t.id AS threat_id,
               t.likelihood AS threat_likelihood
        """

        result = await self.session.run(chain_query, control_ids=security_controls, org_id=org_id)
        combinations = await result.data()
        
        if not combinations:
            return
        
        # Risk matrix for calculating residual risk
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
        
        ease_map = {
            "High": "Low",
            "Medium": "Medium",
            "Low": "High"
        }
        
        for combo in combinations:
            control_id = combo["control_id"]
            control_rating = combo.get("control_rating", "Low")
            vulnerability_id = combo["vulnerability_id"]
            vulnerability_name = combo.get("vulnerability_name", "")
            vuln_ease = combo.get("vuln_ease")  # Get vulnerability's ease_of_exploitation
            threat_id = combo["threat_id"]
            threat_likelihood = combo.get("threat_likelihood", "Medium")
            
            # Use vulnerability's ease_of_exploitation if available, otherwise derive from control rating
            if vuln_ease and vuln_ease in ["Low", "Medium", "High"]:
                ease_of_exploitation = vuln_ease
            else:
                ease_of_exploitation = ease_map.get(str(control_rating).strip() if control_rating else "Low", "High")
            
            # Calculate residual risk using the matrix
            asset_value = asset_data.get("asset_value", "Medium")
            try:
                residual_risk = RISK_MATRIX[threat_likelihood][asset_value][ease_of_exploitation]
            except KeyError:
                residual_risk = "Unknown"
            
            # Generate ID for risk node
            id_query = """
            MERGE (c:Counter {doctype: $doctype})
            ON CREATE SET c.current = 1
            ON MATCH SET c.current = c.current + 1
            RETURN c.current AS new_id
            """
            id_result = await self.session.run(id_query, doctype="risks")
            id_record = await id_result.single()
            new_id = id_record["new_id"]
            risk_id = f"risks-{new_id}"
            
            risk_data = {
                "id": risk_id,
                "risk_by_asset": "Name",
                "associated_assets": asset_data["id"],
                "type": asset_data.get("type"),
                "asset_value": asset_value,
                "associated_threats": threat_id,
                "threat_probability": threat_likelihood,
                "related_vulnerabilities": [vulnerability_id],
                "control_ids": [control_id],
                "ease_of_exploitation": ease_of_exploitation,
                "residual_risk": residual_risk,
                "organization_id": asset_data.get("organization_id"),
                "is_deleted": "false"
            }
            
            # Create risk node
            create_query = """
            CREATE (n:risks $data)
            RETURN n
            """
            await self.session.run(create_query, data=risk_data)
            
            # Create relationships based on schema
            schema = load_schema("risks")
            for field in schema["fields"]:
                if field.get("fieldtype") not in ["Link", "MultiLink"]:
                    continue

                fieldname = field["fieldname"]
                target_value = risk_data.get(fieldname)
                if not target_value:
                    continue

                target_doctype = field["link_to"]
                relationship_type = field.get("relationship_type", "RELATED_TO").upper()
                direction = field.get("relationship_direction", "outgoing")
                values = target_value if isinstance(target_value, list) else [target_value]

                for val in values:
                    if direction == "incoming":
                        relation_query = f"""
                        MATCH (target:{target_doctype} {{id: $val}})
                        MATCH (source:risks {{id: $source_id}})
                        MERGE (target)-[r:{relationship_type}]->(source)
                        """
                    else:
                        relation_query = f"""
                        MATCH (target:{target_doctype} {{id: $val}})
                        MATCH (source:risks {{id: $source_id}})
                        MERGE (source)-[r:{relationship_type}]->(target)
                        """
                    await self.session.run(relation_query, val=val, source_id=risk_id)


    async def create(self, data: dict):

        # Special case for 'control_question' - canonical question creation (new format)
        if self.doctype == "control_question" and data.get("text"):
            return await self._create_canonical_question(data)

        # Legacy 'control_question' with multiple questions (old format)
        if self.doctype == "control_question" and isinstance(data.get("question"), list):
            control_id = data.get("control_id")  # This is now the control node id like "control-123"
            
            # First get the control's control_id value (like "5.10")
            get_control_query = """
                MATCH(c:control {id:$control_id})
                RETURN c.control_id as control_id_value
            """
            result = await self.session.run(get_control_query, control_id=control_id)
            record = await result.single()
            
            if record:
                control = str(record["control_id_value"])  # e.g. "5.10" - ensure it's a string
            else:
                control = str(control_id)  # Fallback to passed value as string
            
            # Check if control_question already exists for this control
            # Use toString() for comparison to handle both string and numeric control_id values
            check_query = """
                MATCH(c:control_question)
                WHERE toString(c.control) = $control
                RETURN c.id as control_question
            """
            result = await self.session.run(check_query, control=str(control))
            record = await result.single()
            if record:
                raise ValueError(f"Control Question already exists for control '{control}'")

            # control = data.get("control_id")
            question_list = data.get("question", [])

            if not control_id or not question_list:
                raise ValueError("Missing 'control' or 'question' list in request")

            # Generate ID
            id_query = """
            MERGE (c:Counter {doctype: $doctype})
            ON CREATE SET c.current = 1
            ON MATCH SET c.current = c.current + 1
            RETURN c.current AS new_id
            """
            result = await self.session.run(id_query, doctype=self.doctype)
            record = await result.single()
            new_id = record["new_id"]
            node_id = f"{self.doctype.lower()}-{new_id}"

            now = datetime.utcnow().isoformat()

            # ✅ Define node_data here before using it
            node_data = {
                "id": node_id,
                "control": control,
                "questions_text": [q["question"] for q in question_list],
                "weights": [q.get("wheightage", 1) for q in question_list],
                "description": data.get("description", ""),
                "created_at": now,
                "updated_at": now
            }

            # ✅ Unpack manually in query
            create_query = f"""
            CREATE (n:{self.doctype} {{
                id: $id,
                control: $control,
                questions_text: $questions_text,
                description: $description,
                weights: $weights,
                created_at: $created_at,
                updated_at: $updated_at
            }})
            RETURN n
            """
            result = await self.session.run(create_query, **node_data)
            record = await result.single()
            node = record["n"]

            # Create legacy relationship (control -> control_question node)
            relation_query = f"""
            MATCH (target:control {{id: $control_id}})
            MATCH (source:{self.doctype} {{id: $question_id}})
            MERGE (target)-[:HAS_QUESTION]->(source)
            """
            await self.session.run(relation_query, control_id=control_id, question_id=node_id)

            # Also create/merge canonical question nodes and link control -> question
            for q_item in question_list:
                q_text = str(q_item.get("question", "")).strip()
                q_weight = float(q_item.get("wheightage", 1) or 1)
                if not q_text:
                    continue
                dkey = f"{control}||{_normalize_question_text(q_text)}"
                await self.session.run(
                    """
                    MERGE (q:question {dedup_key: $key})
                    ON CREATE SET
                        q.id             = 'question-' + toString(id(q)),
                        q.text           = $text,
                        q.weight         = $weight,
                        q.iso_control_id = $iso_control_id,
                        q.created_at     = $now,
                        q.updated_at     = $now
                    ON MATCH SET
                        q.updated_at     = $now
                    WITH q
                    MATCH (c:control {id: $ctrl_node_id})
                    MERGE (c)-[:HAS_QUESTION]->(q)
                    """,
                    key=dkey, text=q_text, weight=q_weight,
                    iso_control_id=control, ctrl_node_id=control_id, now=now,
                )

            return {
                "control_id": control_id,
                "questions": question_list,
                "id": node_id
            }
        # ---- Generic Flow (for all other doctypes) ----
        # Validate required fields
        required_fields = [
            f["fieldname"] for f in self.schema["fields"]
            if f.get("required")
        ]
        for field in required_fields:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

        # Generate new ID
        id_query = """
        MERGE (c:Counter {doctype: $doctype})
        ON CREATE SET c.current = 1
        ON MATCH SET c.current = c.current + 1
        RETURN c.current AS new_id
        """
        result = await self.session.run(id_query, doctype=self.doctype)
        record = await result.single()
        new_id = record["new_id"]
        data["id"] = f"{self.doctype.lower()}-{new_id}"

        # Add timestamps
        now = datetime.utcnow().isoformat()
        data["created_at"] = now
        data["updated_at"] = now

        # Set organization_id for vulnerability, risks, threat, assets
        if self.doctype in ["vulnerability", "risks", "threat", "assets"]:
            if self.current_user:
                data["organization_id"] = self.current_user.get("org_id")
            # Set is_deleted to false for vulnerability, risks, and threat
            if self.doctype in ["vulnerability", "risks", "threat"]:
                data["is_deleted"] = "false"


        # Special handling for control: keep control_id as string to preserve values like "5.10"
        if self.doctype == "control" and "control_id" in data:
            data["control_id"] = str(data["control_id"]).strip()

        # Keep control_assessment list directly in the node (if any)
        assessment_items = data.get("control_assessment", [])
        data["control_assessment"] = assessment_items
        
        # Create the main node
        create_query = f"""
        CREATE (n:{self.doctype} $data)
        RETURN n
        """
        result = await self.session.run(create_query, data=data)
        record = await result.single()
        node = record["n"]

        # Store relationship info for response
        relationship_data = []

        # Handle Link / MultiLink relationships
        for field in self.schema["fields"]:
            if field.get("fieldtype") not in ["Link", "MultiLink"]:
                continue

            fieldname = field.get("fieldname")
            target_value = data.get(fieldname)
            if not target_value:
                continue

            target_doctype = field["link_to"]
            relationship_type = field.get("relationship_type", "RELATED_TO").upper()
            direction = field.get("relationship_direction", "outgoing")
            values = target_value if isinstance(target_value, list) else [target_value]

            for val in values:
                if direction == "incoming":
                    relation_query = f"""
                    MATCH (target:{target_doctype} {{id: $val}})
                    MATCH (source:{self.doctype} {{id: $source_id}})
                    MERGE (target)-[r:{relationship_type}]->(source)
                    """
                else:
                    relation_query = f"""
                    MATCH (target:{target_doctype} {{id: $val}})
                    MATCH (source:{self.doctype} {{id: $source_id}})
                    MERGE (source)-[r:{relationship_type}]->(target)
                    """
                await self.session.run(relation_query, val=val, source_id=data["id"])

        if self.doctype == "assets":
            print("document is assets so creating risks")
            await self.create_risks_for_asset(data)

        # Check if vulnerability has all compliant controls - if so, handle immediately
        if self.doctype == "vulnerability":
            org_id = data.get("organization_id")
            vuln_id = data["id"]
            if org_id:
                await self._check_vulnerability_controls_compliance(vuln_id, org_id)

        return {"n": node, "id": data["id"]}
    
    async def get_all(self, current_user, skip: int = 0, limit: int = 10, filters: dict = None):
        filters = filters or {}
        where_clauses = []
        params = {"skip": skip, "limit": limit}
        org_id = current_user.get("org_id")

        # Filter by organization_id for vulnerability, risks, threat, assets
        # For vulnerability and threat: Also include global items (no org_id) that are not deleted for this org
        if self.doctype in ["vulnerability", "threat"] and org_id:
            where_clauses.append("""(
                n.organization_id = $org_id
                OR
                (n.organization_id IS NULL AND (n.deleted_for_orgs IS NULL OR NOT $org_id IN n.deleted_for_orgs))
            )""")
            params["org_id"] = org_id
        elif self.doctype in ["risks", "assets"] and org_id:
            where_clauses.append("n.organization_id = $org_id")
            params["org_id"] = org_id

        # Filter out deleted items for vulnerability, risks, and threat
        # For org-specific items: check is_deleted flag
        # For global items: already handled above via deleted_for_orgs
        if self.doctype in ["vulnerability", "threat"]:
            where_clauses.append("""(
                (n.organization_id IS NOT NULL AND (n.is_deleted IS NULL OR n.is_deleted = 'false'))
                OR
                n.organization_id IS NULL
            )""")
        elif self.doctype == "risks":
            where_clauses.append("(n.is_deleted IS NULL OR n.is_deleted = 'false')")

        filterable_fields = {f["fieldname"]: f for f in self.schema["fields"] if f.get("is_filter")}
        
        # Separate Link filters (need MATCH) from regular filters (can use WHERE)
        link_match_clauses = []
        
        for i, (key, value) in enumerate(filters.items()):
            if key not in filterable_fields:
                continue  # only allow fields marked as is_filter

            param_key = f"filter_{i}"
            field_info = filterable_fields[key]
            fieldtype = field_info.get("fieldtype", "Data")

            # --- Text fields ---
            if fieldtype in ["Data", "LongText"]:
                # For control_id, always use string comparison to preserve "5.10" vs "5.1"
                if key == "control_id":
                    where_clauses.append(f"toString(n.{key}) = ${param_key}")
                    params[param_key] = str(value)
                    continue
                try:
                    float(value)
                    where_clauses.append(f"n.{key} = ${param_key}")
                except ValueError:
                    where_clauses.append(f"toLower(toString(n.{key})) CONTAINS toLower(${param_key})")

            elif fieldtype == "Link":
                # For Link fields, add to MATCH clause for relationship-based filtering
                rel_type = field_info.get("relationship_type", "")
                rel_direction = field_info.get("relationship_direction", "outgoing")
                link_to = field_info.get("link_to", key)
                
                if rel_type:
                    # Build relationship pattern for MATCH clause
                    if rel_direction == "incoming":
                        # (:target {id: $val})-[:REL]->(n)
                        link_match_clauses.append(f"(:{link_to} {{id: ${param_key}}})-[:{rel_type}]->(n)")
                    else:
                        # (n)-[:REL]->(:target {id: $val})
                        link_match_clauses.append(f"(n)-[:{rel_type}]->(:{link_to} {{id: ${param_key}}})")
                    params[param_key] = value
                    continue
                else:
                    # Fallback: simple property match in WHERE
                    where_clauses.append(f"n.{key} = ${param_key}")
                    params[param_key] = value

            elif fieldtype == "MultiLink":
                # For MultiLink fields (stored as arrays), check if any filter value is in the array
                # Value can come as: single ID string, comma-separated string, JSON array string, or list
                filter_values = value
                
                # Parse value if it's a string that looks like JSON array
                if isinstance(value, str):
                    if value.startswith('['):
                        try:
                            import json
                            filter_values = json.loads(value)
                        except:
                            filter_values = value
                    elif ',' in value:
                        # Comma-separated values
                        filter_values = [v.strip() for v in value.split(',')]
                
                print(f"[MultiLink Filter] key={key}, original_value={value}, filter_values={filter_values}")
                
                # Also ensure the array field exists
                if isinstance(filter_values, list):
                    # If multiple values selected, check if ANY of them is in the array
                    where_clauses.append(f"(n.{key} IS NOT NULL AND ANY(v IN ${param_key} WHERE v IN n.{key}))")
                else:
                    # Single value - check if it's in the array
                    where_clauses.append(f"(n.{key} IS NOT NULL AND ${param_key} IN n.{key})")
                params[param_key] = filter_values
                continue

            elif fieldtype in ["Radio", "Select"]:
                where_clauses.append(f"n.{key} = ${param_key}")

            elif fieldtype == "Date":
                if filters.get(f"{key}_min"):
                    where_clauses.append(f"n.{key} >= ${param_key}_min")
                    params[f"{param_key}_min"] = filters[f"{key}_min"]
                if filters.get(f"{key}_max"):
                    where_clauses.append(f"n.{key} <= ${param_key}_max")
                    params[f"{param_key}_max"] = filters[f"{key}_max"]
                continue

            else:
                where_clauses.append(f"n.{key} = ${param_key}")

            params[param_key] = value

        # Build WHERE clause (excluding Link filters which are in MATCH)
        where_str = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        
        # Build additional MATCH clauses for Link filters
        link_match_str = ""
        if link_match_clauses:
            link_match_str = "\n        " + "\n        ".join(f"MATCH {clause}" for clause in link_match_clauses)
        
        print(f"[get_all] doctype={self.doctype}, link_match={link_match_str}, where={where_str}, params={params}")

        # Get total count
        count_label = "question" if self.doctype == "control_question" else self.doctype
        count_query = f"""
        MATCH (n:{count_label})
        {link_match_str}
        {where_str}
        RETURN count(n) AS total
        """
        count_result = await self.session.run(count_query, **params)
        total = (await count_result.single())["total"]

        # ✅ Order by control_id if doctype is 'control' - handle version-style IDs like 5.1, 5.10
        if self.doctype == "control":
            # Natural sort: supports ISO 27002 (5.1, 5.10) and ISO 27001 (A.5.1, A.5.10)
            order_clause = control_order_clause("n")
        elif self.doctype == "control_question":
            # control_question uses 'control' field, not 'control_id'
            order_clause = """ORDER BY 
                toInteger(split(toString(n.control), '.')[0]) ASC,
                CASE WHEN size(split(toString(n.control), '.')) > 1 
                     THEN toInteger(split(toString(n.control), '.')[1]) 
                     ELSE 0 END ASC"""
        else:
            order_clause = "ORDER BY n.created_at DESC"

        # Get main data
        if self.doctype == "control":
            data_query = f"""
            MATCH (n:control)
            {link_match_str}
            {where_str}
            OPTIONAL MATCH (source)-[r1]->(n)
            WITH n, collect(DISTINCT {{
                direction: "incoming",
                type: type(r1),
                node: source
            }}) AS incoming_rels
            OPTIONAL MATCH (n)-[r2]->(target)
            WITH n, incoming_rels, collect(DISTINCT {{
                direction: "outgoing",
                type: type(r2),
                node: target
            }}) AS outgoing_rels
            RETURN n, incoming_rels + outgoing_rels AS relationships
            {order_clause}
            SKIP $skip
            LIMIT $limit
            """
        elif self.doctype == "control_question":
            # Fetch canonical questions with attached controls for frontend display
            data_query = f"""
            MATCH (n:question)
            {link_match_str}
            {where_str}
            OPTIONAL MATCH (c:control)-[:HAS_QUESTION]->(n)
            WITH n, collect(DISTINCT c.control_id) AS control_ids, collect(DISTINCT c) AS controls
            RETURN n, control_ids, controls
            ORDER BY n.text
            SKIP $skip
            LIMIT $limit
            """
        else:
            data_query = f"""
            MATCH (n:{self.doctype})
            {link_match_str}
            {where_str}
            OPTIONAL MATCH (source)-[r1]->(n)
            WITH n, collect(DISTINCT {{
                direction: "incoming",
                type: type(r1),
                node: source
            }}) AS incoming_rels
            OPTIONAL MATCH (n)-[r2]->(target)
            WITH n, incoming_rels, collect(DISTINCT {{
                direction: "outgoing",
                type: type(r2),
                node: target
            }}) AS outgoing_rels
            RETURN n, incoming_rels + outgoing_rels AS relationships
            {order_clause}
            SKIP $skip
            LIMIT $limit
            """
        data_result = await self.session.run(data_query, **params)
        records = await data_result.data()

        items = []

        # Special handling for control_question - return canonical questions with controls
        if self.doctype == "control_question":
            for record in records:
                node = dict(record["n"])
                control_ids = record.get("control_ids", [])
                controls = record.get("controls", [])
                # Format for frontend: control_id = comma-separated string
                node["control_id"] = ", ".join(control_ids) if control_ids else ""
                items.append({
                    "node": node,
                    "relationships": []
                })
            return {
                "total": total,
                "skip": skip,
                "limit": limit,
                "items": items
            }

        for record in records:
            node = dict(record["n"])
            relationships = record["relationships"]

            if self.doctype == "control":
                control_id = node.get("control_id")
                control_assesment_flag = False

                # Extract framework from incoming relationships (framework node has framework_name)
                fw_val = node.get("framework")
                print(f"[MARKER] BEFORE: control_id={control_id}, fw_val={fw_val}")
                if not fw_val:
                    for rel in relationships:
                        if rel.get("direction") == "incoming":
                            rel_node = rel.get("node", {})
                            # Handle both dict and Neo4j Node
                            if isinstance(rel_node, dict):
                                fn = rel_node.get("framework_name")
                            else:
                                # Neo4j Node - convert to dict
                                fn = dict(rel_node).get("framework_name") if hasattr(rel_node, '__iter__') else None
                            if fn:
                                fw_val = fn
                                print(f"[MARKER] Found in rel: {fw_val}")
                                break
                node["framework"] = fw_val if fw_val else "MISSING"
                print(f"[MARKER] AFTER: node.framework={node.get('framework')}")

                if org_id and control_id:
                    assessment_query = """
                        MATCH (a:control_assessment {control_id: $control_id, organization_id: $org_id})
                        RETURN a
                    """
                    result = await self.session.run(assessment_query, control_id=control_id, org_id=org_id)
                    assessment_record = await result.single()
                    if assessment_record and assessment_record.get("a"):
                        assessment = assessment_record["a"]
                        node["rating"] = assessment.get("control_rating", "Low")
                        node["compliance_status"] = assessment.get("control_compliance", "Non Compliant")
                        node["attached_files"] = _file_objects(assessment.get("attached_files", []))
                        control_assesment_flag = True

            for field in self.schema["fields"]:
                fieldname = field.get("fieldname")
                fieldtype = field.get("fieldtype")
                target_doctype = field.get("link_to")
                target_field = field.get("target_field", "name")
                rel_type = field.get("relationship_type", "").upper()
                direction = field.get("relationship_direction", field.get("relation_direction", "outgoing"))

                if fieldtype not in ["Link", "MultiLink"]:
                    continue
                # Skip framework field - we already populated it manually from owns relationship
                if fieldname == "framework":
                    continue

                matched_nodes = []
                for rel in relationships:
                    if rel["type"] != rel_type:
                        continue
                    if direction == "incoming" and rel["direction"] != "incoming":
                        continue
                    if direction == "outgoing" and rel["direction"] != "outgoing":
                        continue
                    if rel["node"].get("id", "").startswith(target_doctype):
                        matched_nodes.append(rel["node"])

                if fieldtype == "Link":
                    node[fieldname] = matched_nodes[0].get(target_field) if matched_nodes else None
                elif fieldtype == "MultiLink":
                    # For vulnerability's relevant_control_ids, filter out detached controls for this org
                    if self.doctype == "vulnerability" and fieldname == "relevant_control_ids" and org_id:
                        detached_controls = node.get("detached_controls_by_org", []) or []
                        # Filter out controls that are detached for this org
                        filtered_nodes = []
                        for n in matched_nodes:
                            control_id_val = n.get(target_field)
                            detach_key = f"{control_id_val}:{org_id}"
                            if detach_key not in detached_controls:
                                filtered_nodes.append(n)
                        node[fieldname] = ", ".join([str(n.get(target_field)) for n in filtered_nodes if target_field in n])
                    else:
                        node[fieldname] = ", ".join([str(n.get(target_field)) for n in matched_nodes if target_field in n])

                if self.doctype == "control":
                    if not control_assesment_flag:
                        node["rating"] = "Low"
                        node["compliance_status"] = "Non Compliant"
                        node["attached_files"] = []

            print(f"[DEBUG] Appending item: node.framework={node.get('framework')}, control_id={node.get('control_id')}")
            items.append({
                "node": node,
                "relationships": relationships
            })

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": items
        }

    async def get_by_id(self, item_id: str):
        # Special handling for control_question - return canonical question with controls
        if self.doctype == "control_question":
            # Check if it's a canonical question (question-xxx format)
            if item_id.startswith("question-"):
                query = """
                MATCH (n:question {id: $item_id})
                OPTIONAL MATCH (c:control)-[:HAS_QUESTION]->(n)
                WITH n, collect(DISTINCT c.control_id) AS control_ids, collect(DISTINCT c) AS controls
                RETURN n, control_ids, controls
                """
                result = await self.session.run(query, item_id=item_id)
                record = await result.single()
                if record:
                    node = dict(record["n"])
                    control_ids = record.get("control_ids", [])
                    controls = record.get("controls", [])
                    # Format for frontend: control_id = comma-separated string
                    node["control_id"] = ", ".join(control_ids) if control_ids else ""
                    
                    # Build relationships array with control database IDs
                    relationships = []
                    for control in controls:
                        relationships.append({
                            "direction": "incoming",
                            "type": "HAS_QUESTION",
                            "node": dict(control)
                        })
                    
                    return {
                        "node": node,
                        "relationships": relationships
                    }
                return None
        
        # Default handling for other doctypes
        query = f"""
        MATCH (n:{self.doctype} {{id: $item_id}})
        OPTIONAL MATCH (source)-[r1]->(n)
        WITH n, collect(DISTINCT {{
            direction: "incoming",
            type: type(r1),
            node: source
        }}) AS incoming_rels
        OPTIONAL MATCH (n)-[r2]->(target)
        WITH n, incoming_rels, collect(DISTINCT {{
            direction: "outgoing",
            type: type(r2),
            node: target
        }}) AS outgoing_rels
        RETURN n, incoming_rels + outgoing_rels AS relationships
        """
        
        result = await self.session.run(query, item_id=item_id)
        record = await result.single()
        if record:
            return {
                "node": record["n"],
                "relationships": record["relationships"]
            }
        return None

    async def delete(self, item_id: str):
        # Special handling for vulnerability - HARD DELETE
        if self.doctype == "vulnerability":
            # First, get the vulnerability's organization_id
            get_vuln_query = """
            MATCH (v:vulnerability {id: $item_id})
            RETURN v.organization_id AS org_id
            """
            vuln_result = await self.session.run(get_vuln_query, item_id=item_id)
            vuln_record = await vuln_result.single()

            if vuln_record:
                org_id = vuln_record.get("org_id")

                # Hard delete all risks associated with this vulnerability
                delete_risks_query = """
                MATCH (r:risks)
                WHERE $vuln_id IN r.related_vulnerabilities
                DETACH DELETE r
                RETURN count(r) AS deleted_count
                """
                await self.session.run(delete_risks_query, vuln_id=item_id)

                # Remove CAUSES_THREAT relationships and update threat nodes
                remove_threat_rel_query = """
                MATCH (v:vulnerability {id: $item_id})-[r:CAUSES_THREAT]->(t:threat)
                WHERE t.vulnerabilities IS NOT NULL AND $item_id IN t.vulnerabilities
                SET t.vulnerabilities = [vuln IN t.vulnerabilities WHERE vuln <> $item_id]
                DELETE r
                RETURN count(r) AS deleted_relationships
                """
                await self.session.run(remove_threat_rel_query, item_id=item_id)

            # Hard delete the vulnerability
            query = """
            MATCH (n:vulnerability {id: $item_id})
            DETACH DELETE n
            RETURN COUNT(n) AS deleted_count
            """
            result = await self.session.run(query, item_id=item_id)
            deleted = await result.single()
            return deleted["deleted_count"] if deleted else 1

        # Soft-delete for risks
        if self.doctype == "risks":
            query = f"""
            MATCH (n:{self.doctype} {{id: $item_id}})
            SET n.is_deleted = 'true'
            RETURN COUNT(n) AS deleted_count
            """
            result = await self.session.run(query, item_id=item_id)
            deleted = await result.single()
            return deleted["deleted_count"]

        # Hard delete for other doctypes
        query = f"""
        MATCH (n:{self.doctype} {{id: $item_id}})
        DETACH DELETE n
        RETURN COUNT(n) AS deleted_count
        """
        result = await self.session.run(query, item_id=item_id)
        deleted = await result.single()
        return deleted["deleted_count"]


    async def delete_all(self):
        """
        Delete all nodes of this doctype.
        Returns number of deleted items.
        """
        # Hard delete for vulnerability - also delete associated risks
        if self.doctype == "vulnerability":
            # First delete all risks
            await self.session.run("MATCH (r:risks) DETACH DELETE r")

            # Then delete all vulnerabilities
            query = """
            MATCH (n:vulnerability)
            WITH n, count(n) AS cnt
            DETACH DELETE n
            RETURN cnt
            """
            result = await self.session.run(query)
            record = await result.single()
            return record["cnt"] if record else 0

        # Soft-delete for risks
        if self.doctype == "risks":
            query = f"""
            MATCH (n:{self.doctype})
            WITH n, count(n) AS cnt
            SET n.is_deleted = 'true'
            RETURN cnt
            """
            result = await self.session.run(query)
            record = await result.single()
            return record["cnt"] if record else 0

        # Hard delete for other doctypes
        query = f"""
        MATCH (n:{self.doctype})
        WITH n, count(n) AS cnt
        DETACH DELETE n
        RETURN cnt
        """
        result = await self.session.run(query)
        record = await result.single()

        return record["cnt"] if record else 0

    async def delete_bulk(self, item_ids: list):
        """
        Delete multiple nodes by their IDs without checking relationships.
        Used for risks deletion where relationships should be forcefully removed.
        Returns the count of deleted items.
        """
        if not item_ids:
            return 0

        # Special handling for vulnerability bulk delete - HARD DELETE
        if self.doctype == "vulnerability":
            # For each vulnerability, hard-delete associated risks and remove CAUSES_THREAT relationships
            for vuln_id in item_ids:
                # Hard delete all risks associated with this vulnerability
                delete_risks_query = """
                MATCH (r:risks)
                WHERE $vuln_id IN r.related_vulnerabilities
                DETACH DELETE r
                """
                await self.session.run(delete_risks_query, vuln_id=vuln_id)

                # Remove CAUSES_THREAT relationships and update threat nodes
                remove_threat_rel_query = """
                MATCH (v:vulnerability {id: $vuln_id})-[r:CAUSES_THREAT]->(t:threat)
                WHERE t.vulnerabilities IS NOT NULL AND $vuln_id IN t.vulnerabilities
                SET t.vulnerabilities = [vuln IN t.vulnerabilities WHERE vuln <> $vuln_id]
                DELETE r
                """
                await self.session.run(remove_threat_rel_query, vuln_id=vuln_id)

            # Hard delete the vulnerabilities
            query = """
            MATCH (n:vulnerability)
            WHERE n.id IN $item_ids
            WITH n, count(n) AS cnt
            DETACH DELETE n
            RETURN cnt
            """
            result = await self.session.run(query, item_ids=item_ids)
            record = await result.single()
            return record["cnt"] if record else len(item_ids)

        # Soft-delete for risks
        if self.doctype == "risks":
            query = f"""
            MATCH (n:{self.doctype})
            WHERE n.id IN $item_ids
            WITH n, count(n) AS cnt
            SET n.is_deleted = 'true'
            RETURN cnt
            """
            result = await self.session.run(query, item_ids=item_ids)
            record = await result.single()
            return record["cnt"] if record else 0

        # Hard delete for other doctypes
        query = f"""
        MATCH (n:{self.doctype})
        WHERE n.id IN $item_ids
        WITH n, count(n) AS cnt
        DETACH DELETE n
        RETURN cnt
        """
        result = await self.session.run(query, item_ids=item_ids)
        record = await result.single()
        return record["cnt"] if record else 0

    async def update(self, item_id: str, data: dict):
        now = datetime.utcnow().isoformat()
        data["updated_at"] = now

        # Ensure attached_files is always stored as list[str] (URLs/paths), not list[map].
        if "attached_files" in data:
            data["attached_files"] = _normalize_file_list(data.get("attached_files"))

        # ✅ Special handling for control_question
        if self.doctype == "control_question":
            # Handle canonical question update (control_id as list or string)
            control_id = data.get("control_id")
            if (isinstance(control_id, (str, list))) and data.get("text"):
                return await self._update_canonical_question(item_id, data, now)

            # Legacy control_question update (single control_id + question list)
            if isinstance(data.get("question"), list):
                control_id = data.get("control_id")
                description = data.get("description")
                question_list = data.get("question", [])

                if not control_id or not question_list:
                    raise ValueError("Missing 'control' or 'question' list in update")

            # Transform into flat structure for storage
            update_data = {
                "control_id": control_id,
                "description": description,
                "questions_text": [q["question"] for q in question_list],
                "weights": [q.get("wheightage", 1) for q in question_list],
                "updated_at": now
            }

            # Update the node
            query = f"""
            MATCH (n:{self.doctype} {{id: $item_id}})
            SET n += $data
            RETURN n
            """
            result = await self.session.run(query, item_id=item_id, data=update_data)
            record = await result.single()
            if not record:
                return None
            node = record["n"]

            # Delete existing HAS_QUESTION relationships (legacy control_question node)
            delete_rel_query = f"""
            MATCH (n:{self.doctype} {{id: $item_id}})<-[r:HAS_QUESTION]-(:control)
            DELETE r
            """
            await self.session.run(delete_rel_query, item_id=item_id)

            # Re-create legacy relationship to control
            relation_query = f"""
            MATCH (target:control {{control_id: $control_id}})
            MATCH (source:{self.doctype} {{id: $item_id}})
            MERGE (target)-[:HAS_QUESTION]->(source)
            """
            await self.session.run(relation_query, control_id=control_id, item_id=item_id)

            # Refresh canonical question nodes for each question in the updated list
            ctrl_node_result = await self.session.run(
                "MATCH (c:control {control_id: $cid}) RETURN c.id AS node_id LIMIT 1",
                cid=control_id,
            )
            ctrl_rec = await ctrl_node_result.single()
            ctrl_node_id = ctrl_rec["node_id"] if ctrl_rec else None

            for q_item in question_list:
                q_text = str(q_item.get("question", "")).strip()
                q_weight = float(q_item.get("wheightage", 1) or 1)
                if not q_text or not ctrl_node_id:
                    continue
                dkey = f"{control_id}||{_normalize_question_text(q_text)}"
                await self.session.run(
                    """
                    MERGE (q:question {dedup_key: $key})
                    ON CREATE SET
                        q.id             = 'question-' + toString(id(q)),
                        q.text           = $text,
                        q.weight         = $weight,
                        q.iso_control_id = $iso_control_id,
                        q.created_at     = $now,
                        q.updated_at     = $now
                    ON MATCH SET
                        q.weight         = $weight,
                        q.updated_at     = $now
                    WITH q
                    MATCH (c:control {id: $ctrl_node_id})
                    MERGE (c)-[:HAS_QUESTION]->(q)
                    """,
                    key=dkey, text=q_text, weight=q_weight,
                    iso_control_id=control_id, ctrl_node_id=ctrl_node_id,
                    now=now,
                )

            return {"n": node, "id": item_id}

        # ✅ Special handling for control (control_assessment list)
        if self.doctype == "control" and isinstance(data.get("control_assessment"), list):
            data["control_assessment"] = json.dumps(data["control_assessment"])  # Serialize
            data["updated_at"] = now  # Already set, but reinforces clarity
            print("++++++++++++++++++ current user id is",self.current_user.get("org_id"))
   
            assessment_data = {
                "control_id": data["control_id"],
                "organization_id": self.current_user.get("org_id"),
                "control_assessment": data["control_assessment"],
                "control_rating": data["rating"],
                "control_compliance": data["compliance_status"],
                "remarks": data.get("remarks", ""),
                "observation": data.get("observation", ""),
                "reference_evidence" : data.get("reference_evidence", ""),
                "attached_files": _normalize_file_list(data.get("attached_files", [])),
                "next_review_date": data.get("next_review_date", ""),
                "control_applicable": data.get("control_applicable", "Yes"),
                "created_at": now,
                "updated_at": now
            }
            
        # INSERT_YOUR_CODE
        # Check if a control_assessment node exists for this control and organization
            check_query = """
            MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
            RETURN a
            """
            result = await self.session.run(
                check_query,
                control_id=assessment_data["control_id"],
                organization_id=assessment_data["organization_id"]
                
            )
            record = await result.single()
            
            

            if not record:
                # Create new control_assessment node
                create_query = """
                CREATE (a:control_assessment $assessment_data)
                RETURN a
                """
                await self.session.run(create_query, assessment_data=assessment_data)
            else:
                # Update existing control_assessment node
                update_query = """
                MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
                SET a += $assessment_data
                RETURN a
                """
                await self.session.run(
                    update_query,
                    control_id=assessment_data["control_id"],
                    organization_id=assessment_data["organization_id"],
                    assessment_data=assessment_data
                )

            # Upsert per-question responses and propagate compliance across all
            # controls that share these questions (cross-framework sync).
            try:
                raw_answers = json.loads(data["control_assessment"]) if isinstance(data["control_assessment"], str) else data.get("control_assessment", [])
                if isinstance(raw_answers, list) and raw_answers:
                    await self._upsert_question_responses(
                        control_id_value=data["control_id"],
                        organization_id=assessment_data["organization_id"],
                        answer_list=raw_answers,
                    )
                else:
                    # No question answers in payload — still fire triggers for the
                    # manually set compliance status.
                    if data.get("compliance_status") == "Compliant":
                        await self._handle_compliant_control_trigger(item_id, assessment_data["organization_id"])
                    else:
                        await self._handle_non_compliant_control_trigger(item_id, assessment_data["organization_id"])
            except Exception as _qe:
                print(f"[QSYNC] Error upserting question responses: {_qe}")
                if data.get("compliance_status") == "Compliant":
                    await self._handle_compliant_control_trigger(item_id, assessment_data["organization_id"])
                else:
                    await self._handle_non_compliant_control_trigger(item_id, assessment_data["organization_id"])

        if self.doctype == "control" and data.get("control_assessment") is None:
           # Serialize
            data["updated_at"] = now  # Already set, but reinforces clarity
            print("++++++++++++++++++ current user id is",self.current_user.get("org_id"))

            assessment_data = {
                "control_id": data["control_id"],
                "control_assessment":data["control_assessment"],
                "organization_id": self.current_user.get("org_id"),
                "remarks": data.get("remarks", ""),
                "observation": data.get("observation", ""),
                "reference_evidence" : data.get("reference_evidence", ""),
                "attached_files": _normalize_file_list(data.get("attached_files", [])),
                "next_review_date": data.get("next_review_date", ""),
                "control_applicable": data.get("control_applicable", "Yes"),
                "control_rating": data["rating"],
                "control_compliance":"Non Compliant",
                "created_at": now,
                "updated_at": now
            }
            
        # INSERT_YOUR_CODE
        # Check if a control_assessment node exists for this control and organization
            check_query = """
            MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
            RETURN a
            """
            result = await self.session.run(
                check_query,
                control_id=assessment_data["control_id"],
                organization_id=assessment_data["organization_id"]
            )
            record = await result.single()
            
            

            if not record:
                # Create new control_assessment node
                create_query = """
                CREATE (a:control_assessment $assessment_data)
                RETURN a
                """
                await self.session.run(create_query, assessment_data=assessment_data)
            else:
                # Update existing control_assessment node
                update_query = """
                MATCH (a:control_assessment {control_id: $control_id, organization_id: $organization_id})
                SET a += $assessment_data
                RETURN a
                """
                await self.session.run(
                    update_query,
                    control_id=assessment_data["control_id"],
                    organization_id=assessment_data["organization_id"],
                    assessment_data=assessment_data
                )

            # No answers provided, default Non Compliant — fire trigger only.
            await self._handle_non_compliant_control_trigger(item_id, assessment_data["organization_id"])

        # ✅ Special handling for vulnerability soft-delete via update
        if self.doctype == "vulnerability" and data.get("is_deleted") == "true":
            # Get the vulnerability's organization_id
            get_vuln_query = """
            MATCH (v:vulnerability {id: $item_id})
            RETURN v.organization_id AS org_id
            """
            vuln_result = await self.session.run(get_vuln_query, item_id=item_id)
            vuln_record = await vuln_result.single()

            if vuln_record:
                org_id = vuln_record.get("org_id")

                # Soft-delete all risks associated with this vulnerability
                soft_delete_risks_query = """
                MATCH (r:risks)
                WHERE $vuln_id IN r.related_vulnerabilities AND r.organization_id = $org_id
                SET r.is_deleted = 'true'
                RETURN count(r) AS deleted_count
                """
                await self.session.run(soft_delete_risks_query, vuln_id=item_id, org_id=org_id)

                # Remove CAUSES_THREAT relationships and update threat nodes
                remove_threat_rel_query = """
                MATCH (v:vulnerability {id: $item_id})-[r:CAUSES_THREAT]->(t:threat)
                WHERE t.vulnerabilities IS NOT NULL AND $item_id IN t.vulnerabilities
                SET t.vulnerabilities = [vuln IN t.vulnerabilities WHERE vuln <> $item_id]
                DELETE r
                RETURN count(r) AS deleted_relationships
                """
                await self.session.run(remove_threat_rel_query, item_id=item_id)

        # ✅ Generic update logic for all doctypes
        query = f"""
        MATCH (n:{self.doctype} {{id: $item_id}})
        SET n += $data
        RETURN n
        """
        result = await self.session.run(query, item_id=item_id, data=data)
        record = await result.single()
        if not record:
            return None

        node = record["n"]

        # Delete old relationships for updated fields
        for field in self.schema["fields"]:
            if field.get("fieldtype") not in ["Link", "MultiLink"]:
                continue

            fieldname = field.get("fieldname")
            if fieldname not in data:
                continue

            target_doctype = field["link_to"]
            relationship_type = field.get("relationship_type", "RELATED_TO").upper()
            direction = field.get("relationship_direction", "outgoing")

            if direction == "incoming":
                delete_query = f"""
                MATCH (target:{target_doctype})-[r:{relationship_type}]->(n:{self.doctype} {{id: $item_id}})
                DELETE r
                """
            else:
                delete_query = f"""
                MATCH (n:{self.doctype} {{id: $item_id}})-[r:{relationship_type}]->(target:{target_doctype})
                DELETE r
                """
            await self.session.run(delete_query, item_id=item_id)

        # Recreate updated relationships
        for field in self.schema["fields"]:
            if field.get("fieldtype") not in ["Link", "MultiLink"]:
                continue

            fieldname = field.get("fieldname")
            target_value = data.get(fieldname)
            if not target_value:
                continue

            target_doctype = field["link_to"]
            relationship_type = field.get("relationship_type", "RELATED_TO").upper()
            direction = field.get("relationship_direction", "outgoing")
            target_field = "id"
            values = target_value if isinstance(target_value, list) else [target_value]

            for val in values:
                if direction == "incoming":
                    relation_query = f"""
                    MATCH (target:{target_doctype} {{{target_field}: $val}})
                    MATCH (source:{self.doctype} {{id: $source_id}})
                    MERGE (target)-[r:{relationship_type}]->(source)
                    """
                else:
                    relation_query = f"""
                    MATCH (target:{target_doctype} {{{target_field}: $val}})
                    MATCH (source:{self.doctype} {{id: $source_id}})
                    MERGE (source)-[r:{relationship_type}]->(target)
                    """
                await self.session.run(relation_query, val=val, source_id=item_id)
                
                
        if self.doctype == "assets":
            delete_risks_query = """
            MATCH (r:risks {associated_assets: $asset_id})
            DETACH DELETE r
            """
            await self.session.run(delete_risks_query, asset_id=item_id)

            # Fetch the full asset node to ensure we have all required fields
            asset_query = """
            MATCH (a:assets {id: $asset_id})
            RETURN a
            """
            asset_result = await self.session.run(asset_query, asset_id=item_id)
            asset_record = await asset_result.single()
            if asset_record and asset_record.get("a"):
                asset_data = dict(asset_record["a"])
                await self.create_risks_for_asset(asset_data)

        # If a related entity changed (control, threat, vulnerability), recompute risks for affected assets
        if self.doctype in ["control", "threat", "vulnerability"]:
            affected_assets = []

            if self.doctype == "control":
                # assets reference controls by the control node's id (e.g. 'control-13') in their security_controls list
                control_node_id = item_id or node.get("id")
                if control_node_id:
                    query = """
                    MATCH (a:assets)
                    WHERE $control_node_id IN a.security_controls
                    RETURN a
                    """
                    result = await self.session.run(query, control_node_id=control_node_id)
                    records = await result.data()
                    affected_assets = [dict(r["a"]) for r in records if r.get("a")]

            elif self.doctype == "threat":
                threat_id = item_id or node.get("id")
                
                # Method 1: Find assets through risks that reference this threat
                if threat_id:
                    risk_assets_query = """
                    MATCH (r:risks {associated_threats: $threat_id})
                    MATCH (a:assets {id: r.associated_assets})
                    RETURN DISTINCT a
                    """
                    result = await self.session.run(risk_assets_query, threat_id=threat_id)
                    records = await result.data()
                    affected_assets = [dict(r["a"]) for r in records if r.get("a")]
                
                # Method 2: Find assets through relationship chain: control -[MITIGATES]-> vulnerability -[CAUSES_THREAT]-> threat
                if threat_id:
                    chain_query = """
                    MATCH (c:control)-[:MITIGATES]->(v:vulnerability)-[:CAUSES_THREAT]->(t:threat {id: $threat_id})
                    MATCH (a:assets)
                    WHERE c.id IN a.security_controls
                    RETURN DISTINCT a
                    """
                    chain_result = await self.session.run(chain_query, threat_id=threat_id)
                    chain_records = await chain_result.data()
                    for r in chain_records:
                        if r.get("a"):
                            affected_assets.append(dict(r["a"]))

            elif self.doctype == "vulnerability":
                vuln_id = item_id or data.get("id") or node.get("id")
                print(f"[DEBUG] Vulnerability update - vuln_id: {vuln_id}")
                
                # Method 1: Find assets through risks that reference this vulnerability
                if vuln_id:
                    risk_assets_query = """
                    MATCH (r:risks)
                    WHERE $vuln_id IN r.related_vulnerabilities
                    MATCH (a:assets {id: r.associated_assets})
                    RETURN DISTINCT a
                    """
                    result = await self.session.run(risk_assets_query, vuln_id=vuln_id)
                    records = await result.data()
                    affected_assets = [dict(r["a"]) for r in records if r.get("a")]
                    print(f"[DEBUG] Method 1 (risks query): Found {len(affected_assets)} assets")
                
                # Method 2: Find assets through relationship chain: control -[MITIGATES]-> vulnerability
                if vuln_id:
                    chain_query = """
                    MATCH (c:control)-[:MITIGATES]->(v:vulnerability {id: $vuln_id})
                    MATCH (a:assets)
                    WHERE c.id IN a.security_controls
                    RETURN DISTINCT a
                    """
                    chain_result = await self.session.run(chain_query, vuln_id=vuln_id)
                    chain_records = await chain_result.data()
                    print(f"[DEBUG] Method 2 (chain query): Found {len(chain_records)} assets")
                    for r in chain_records:
                        if r.get("a"):
                            affected_assets.append(dict(r["a"]))

            # Deduplicate assets by id and recompute risks
            seen = set()
            print(f"[DEBUG] Updating {self.doctype}, found {len(affected_assets)} affected assets")
            for asset in affected_assets:
                asset_id = asset.get("id")
                if not asset_id or asset_id in seen:
                    continue
                seen.add(asset_id)
                print(f"[DEBUG] Recomputing risks for asset: {asset_id}")

                # delete existing risks for this asset
                delete_risks_query = """
                MATCH (r:risks {associated_assets: $asset_id})
                DETACH DELETE r
                """
                await self.session.run(delete_risks_query, asset_id=asset_id)

                # ensure asset has id in dict and pass to risk generator
                asset_data = {**asset, "id": asset_id}
                try:
                    await self.create_risks_for_asset(asset_data)
                    print(f"[DEBUG] Successfully recreated risks for asset: {asset_id}")
                except Exception as e:
                    print(f"[ERROR] Failed to recreate risks for asset {asset_id}: {e}")

        # Check if vulnerability has all compliant controls after update
        if self.doctype == "vulnerability":
            org_id = data.get("organization_id") or node.get("organization_id") or self.current_user.get("org_id")
            if org_id:
                await self._check_vulnerability_controls_compliance(item_id, org_id)

        return {"n": node, "id": item_id}

    async def _update_canonical_question(self, item_id: str, data: dict, now: str):
        """
        Update canonical question node with new control relationships.
        control_id is list of control_ids to attach.
        """
        text = data.get("text", "").strip()
        weight = float(data.get("weight", 1) or 1)
        description = data.get("description", "").strip()
        
        # Parse control_id (now expecting database IDs like "control-123")
        control_id_raw = data.get("control_id", [])
        if isinstance(control_id_raw, list):
            control_db_ids = [c.strip() for c in control_id_raw if c and str(c).strip()]
        elif isinstance(control_id_raw, str):
            control_db_ids = [c.strip() for c in control_id_raw.split(",") if c.strip()]
        else:
            control_db_ids = []
        
        # Convert database IDs to control_id values for relationships
        new_control_ids = []
        for db_id in control_db_ids:
            result = await self.session.run(
                "MATCH (c:control {id: $db_id}) RETURN c.control_id AS cid",
                db_id=db_id
            )
            record = await result.single()
            if record and record["cid"]:
                new_control_ids.append(record["cid"])
        
        # Update question properties
        await self.session.run(
            """
            MATCH (q:question {id: $item_id})
            SET q.text = $text, q.weight = $weight, q.description = $desc, q.updated_at = $now
            """,
            item_id=item_id, text=text, weight=weight, desc=description, now=now
        )
        
        # Delete existing HAS_QUESTION relationships
        await self.session.run(
            """
            MATCH (c:control)-[r:HAS_QUESTION]->(q:question {id: $item_id})
            DELETE r
            """,
            item_id=item_id
        )
        
        # Create new HAS_QUESTION relationships
        for cid in new_control_ids:
            await self.session.run(
                """
                MATCH (q:question {id: $item_id})
                MATCH (c:control {control_id: $cid})
                MERGE (c)-[:HAS_QUESTION]->(q)
                """,
                item_id=item_id, cid=cid
            )
        
        # Return updated node
        result = await self.session.run(
            "MATCH (q:question {id: $item_id}) RETURN q",
            item_id=item_id
        )
        record = await result.single()
        return {"n": record["q"] if record else {}, "id": item_id}

    async def _create_canonical_question(self, data: dict):
        """
        Create new canonical question node with control relationships.
        data: {text: str, control_id: list|str, weight: number, description: str}
        """
        text = data.get("text", "").strip()
        weight = float(data.get("weight", 1) or 1)
        description = data.get("description", "").strip()
        
        # Parse control_id (now expecting database IDs like "control-123")
        control_id_raw = data.get("control_id", [])
        if isinstance(control_id_raw, list):
            control_db_ids = [c.strip() for c in control_id_raw if c and str(c).strip()]
        elif isinstance(control_id_raw, str):
            control_db_ids = [c.strip() for c in control_id_raw.split(",") if c.strip()]
        else:
            control_db_ids = []
        
        # Convert database IDs to control_id values for relationships
        control_ids = []
        for db_id in control_db_ids:
            result = await self.session.run(
                "MATCH (c:control {id: $db_id}) RETURN c.control_id AS cid",
                db_id=db_id
            )
            record = await result.single()
            if record and record["cid"]:
                control_ids.append(record["cid"])
        
        if not text:
            raise ValueError("Question text is required")
        
        now = datetime.utcnow().isoformat()
        
        # Generate question ID
        id_query = """
        MERGE (c:Counter {doctype: 'question'})
        ON CREATE SET c.current = 1
        ON MATCH SET c.current = c.current + 1
        RETURN c.current AS new_id
        """
        result = await self.session.run(id_query)
        record = await result.single()
        new_id = record["new_id"]
        question_id = f"question-{new_id}"
        
        # Create canonical question node
        iso_control_id = control_ids[0] if control_ids else ""
        dedup_key = f"{iso_control_id}||{_normalize_question_text(text)}"
        
        await self.session.run(
            """
            CREATE (q:question {
                id: $qid,
                text: $text,
                weight: $weight,
                description: $desc,
                iso_control_id: $iso_cid,
                dedup_key: $dkey,
                created_at: $now,
                updated_at: $now
            })
            """,
            qid=question_id, text=text, weight=weight, desc=description,
            iso_cid=iso_control_id, dkey=dedup_key, now=now
        )
        
        # Create HAS_QUESTION relationships to controls
        for cid in control_ids:
            await self.session.run(
                """
                MATCH (q:question {id: $qid})
                MATCH (c:control {control_id: $cid})
                MERGE (c)-[:HAS_QUESTION]->(q)
                """,
                qid=question_id, cid=cid
            )
        
        # Return created node formatted for control_question API
        result = await self.session.run(
            "MATCH (q:question {id: $qid}) RETURN q",
            qid=question_id
        )
        record = await result.single()
        node = dict(record["q"]) if record else {}
        node["control_id"] = control_ids
        
        return {"n": node, "id": question_id}

    async def _upsert_question_responses(self, control_id_value: str, organization_id: str, answer_list: list):
        """
        Persist per-question answers as question_response nodes.
        answer_list is the deserialized control_assessment JSON: [{question, answer, weight?, ...}]

        For each item we:
          1. Resolve the canonical question node by (iso_control_id, normalized text).
          2. MERGE a question_response node keyed by (question_id, organization_id).
          3. Re-link question -[:HAS_RESPONSE]-> question_response.

        After saving responses we propagate compliance to every control that shares
        a question with this one (cross-framework synchronisation).
        """
        if not answer_list or not organization_id:
            return

        now = datetime.utcnow().isoformat()
        changed_question_ids: list[str] = []

        for item in answer_list:
            if not isinstance(item, dict):
                continue
            q_text = str(item.get("question", "")).strip()
            answer_val = bool(item.get("answer", False))
            evidence = str(item.get("evidence", ""))
            remarks = str(item.get("remarks", ""))
            if not q_text:
                continue

            # Use question_id from frontend if available, else fall back to dedup_key lookup
            q_id = item.get("question_id")
            if not q_id:
                dkey = f"{control_id_value}||{_normalize_question_text(q_text)}"
                q_result = await self.session.run(
                    "MATCH (q:question {dedup_key: $key}) RETURN q.id AS qid",
                    key=dkey,
                )
                q_rec = await q_result.single()
                if not q_rec:
                    print(f"[QSAVE] Warning: Question not found for dedup_key={dkey}, text={q_text[:50]}")
                    continue
                q_id = q_rec["qid"]
            changed_question_ids.append(q_id)
            print(f"[QSAVE] Saving answer for q_id={q_id}, answer={answer_val}")

            # Upsert question_response with evidence and remarks
            await self.session.run(
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
                qid=q_id,
                org_id=organization_id,
                answer=answer_val,
                evidence=evidence,
                remarks=remarks,
                now=now,
            )

        if not changed_question_ids:
            return

        # Find all controls that share any of these questions
        affected_result = await self.session.run(
            """
            MATCH (c:control)-[:HAS_QUESTION]->(q:question)
            WHERE q.id IN $qids
            RETURN DISTINCT c.id AS ctrl_node_id, c.control_id AS ctrl_id_val
            """,
            qids=changed_question_ids,
        )
        affected_controls = await affected_result.data()

        for ctrl in affected_controls:
            ctrl_node_id = ctrl["ctrl_node_id"]
            ctrl_id_val = ctrl["ctrl_id_val"]
            if not ctrl_id_val or not ctrl_node_id:
                continue

            new_compliance = await self.recompute_control_compliance(ctrl_id_val, organization_id)
            print(f"[QSYNC] Control {ctrl_id_val} recomputed => {new_compliance} for org {organization_id}")

            # Fire vulnerability/risk triggers for this control
            if new_compliance == "Compliant":
                await self._handle_compliant_control_trigger(ctrl_node_id, organization_id)
            else:
                await self._handle_non_compliant_control_trigger(ctrl_node_id, organization_id)

    async def recompute_control_compliance(self, control_id_value: str, organization_id: str) -> str:
        """
        Recompute and persist compliance status for (control_id_value, organization_id)
        from the question_response nodes linked to that control's questions.
        Returns the new compliance string.
        """
        result = await self.session.run(
            """
            MATCH (c:control {control_id: $cid})-[:HAS_QUESTION]->(q:question)
            OPTIONAL MATCH (q)-[:HAS_RESPONSE]->(r:question_response {organization_id: $org_id})
            RETURN q.weight AS weight, r.answer AS answer
            """,
            cid=control_id_value,
            org_id=organization_id,
        )
        rows = await result.data()

        if not rows:
            return "Non Compliant"

        total = len(rows)
        answered = sum(1 for r in rows if r.get("answer") is True)
        weighted_total = sum(float(r.get("weight") or 1.0) for r in rows)
        weighted_yes = sum(
            float(r.get("weight") or 1.0) for r in rows if r.get("answer") is True
        )
        new_compliance = _derive_compliance(answered, total, weighted_yes, weighted_total)

        # Upsert the derived compliance into control_assessment
        now = datetime.utcnow().isoformat()
        await self.session.run(
            """
            MERGE (a:control_assessment {control_id: $cid, organization_id: $org_id})
            ON CREATE SET a.control_compliance = $compliance,
                          a.control_rating     = 'Low',
                          a.created_at         = $now,
                          a.updated_at         = $now
            ON MATCH SET  a.control_compliance = $compliance,
                          a.updated_at         = $now
            """,
            cid=control_id_value,
            org_id=organization_id,
            compliance=new_compliance,
            now=now,
        )
        return new_compliance

    async def _handle_compliant_control_trigger(self, control_id: str, organization_id: str):
        """
        When a control assessment becomes Compliant:
        1. Find all vulnerabilities connected to this control for this organization
        2. For each vulnerability, check if ALL connected controls are compliant for this org
        3. If all controls are compliant, soft-delete the vulnerability and its associated risks
        """
        print(f"[TRIGGER] Control {control_id} is Compliant. Checking connected vulnerabilities for org {organization_id}...")

        # Get the control_id value (like "5.1") for this control
        get_control_id_query = """
        MATCH (c:control {id: $control_id})
        RETURN c.control_id AS control_id_value
        """
        control_result = await self.session.run(get_control_id_query, control_id=control_id)
        control_record = await control_result.single()
        control_id_value = control_record["control_id_value"] if control_record else None

        # Find all vulnerabilities connected to this control via MITIGATES relationship
        # Include BOTH org-specific vulnerabilities AND global vulnerabilities (no org_id)
        # For org-specific: check is_deleted
        # For global: check if org is NOT in deleted_for_orgs
        vuln_query = """
        MATCH (c:control {id: $control_id})-[:MITIGATES]->(v:vulnerability)
        WHERE (
            (v.organization_id = $org_id AND (v.is_deleted IS NULL OR v.is_deleted = 'false'))
            OR
            (v.organization_id IS NULL AND (v.deleted_for_orgs IS NULL OR NOT $org_id IN v.deleted_for_orgs))
        )
        RETURN v.id AS vulnerability_id, v.organization_id AS vuln_org_id
        """
        result = await self.session.run(vuln_query, control_id=control_id, org_id=organization_id)
        vulnerabilities = await result.data()

        if not vulnerabilities:
            print(f"[TRIGGER] No vulnerabilities connected to control {control_id} for org {organization_id}")
            return

        for vuln in vulnerabilities:
            vuln_id = vuln["vulnerability_id"]
            vuln_org_id = vuln.get("vuln_org_id")  # None for global vulnerabilities
            is_global_vuln = vuln_org_id is None
            print(f"[TRIGGER] Checking vulnerability {vuln_id} (global={is_global_vuln})...")

            # STEP 1: Detach this control from the vulnerability for this organization
            # Add "control_id_value:org_id" to detached_controls_by_org array
            if control_id_value:
                detach_key = f"{control_id_value}:{organization_id}"
                detach_control_query = """
                MATCH (v:vulnerability {id: $vuln_id})
                SET v.detached_controls_by_org = CASE
                    WHEN v.detached_controls_by_org IS NULL THEN [$detach_key]
                    WHEN NOT $detach_key IN v.detached_controls_by_org THEN v.detached_controls_by_org + $detach_key
                    ELSE v.detached_controls_by_org
                END
                RETURN count(v) AS updated
                """
                await self.session.run(detach_control_query, vuln_id=vuln_id, detach_key=detach_key)
                print(f"[TRIGGER] Detached control {control_id_value} from vulnerability {vuln_id} for org {organization_id}")

                # Soft-delete risks specifically for this control and vulnerability
                soft_delete_control_risks_query = """
                MATCH (r:risks)
                WHERE $vuln_id IN r.related_vulnerabilities
                AND r.organization_id = $org_id
                AND $control_id IN r.control_ids
                SET r.is_deleted = 'true'
                RETURN count(r) AS deleted_count
                """
                await self.session.run(soft_delete_control_risks_query, vuln_id=vuln_id, org_id=organization_id, control_id=control_id)
                print(f"[TRIGGER] Soft-deleted risks for control {control_id_value} and vulnerability {vuln_id}")

            # Get all controls connected to this vulnerability
            controls_query = """
            MATCH (c:control)-[:MITIGATES]->(v:vulnerability {id: $vuln_id})
            RETURN c.id AS control_id, c.control_id AS control_id_value
            """
            controls_result = await self.session.run(controls_query, vuln_id=vuln_id)
            connected_controls = await controls_result.data()

            if not connected_controls:
                continue

            # Check if ALL connected controls are compliant for this organization
            all_compliant = True
            for ctrl in connected_controls:
                ctrl_id_value = ctrl["control_id_value"]
                
                # Check control_assessment for this control and organization
                assessment_query = """
                MATCH (a:control_assessment {control_id: $control_id, organization_id: $org_id})
                RETURN a.control_compliance AS compliance_status
                """
                assessment_result = await self.session.run(
                    assessment_query, 
                    control_id=ctrl_id_value, 
                    org_id=organization_id
                )
                assessment_record = await assessment_result.single()

                if not assessment_record or assessment_record.get("compliance_status") != "Compliant":
                    all_compliant = False
                    print(f"[TRIGGER] Control {ctrl_id_value} is not compliant. Skipping vulnerability deletion.")
                    break

            if all_compliant:
                print(f"[TRIGGER] All controls for vulnerability {vuln_id} are compliant. Soft-deleting vulnerability and associated risks for organization {organization_id}...")

                # First, store the connected threat IDs in the vulnerability node before removing relationships
                store_threat_ids_query = """
                MATCH (v:vulnerability {id: $vuln_id})-[:CAUSES_THREAT]->(t:threat)
                WITH v, collect(t.id) AS threat_ids
                SET v.archived_threat_ids = threat_ids
                RETURN threat_ids
                """
                store_result = await self.session.run(store_threat_ids_query, vuln_id=vuln_id)
                store_record = await store_result.single()
                archived_threats = store_record["threat_ids"] if store_record else []
                print(f"[TRIGGER] Stored {len(archived_threats)} threat IDs in vulnerability {vuln_id} for future restoration")

                # Soft-delete all risks associated with this vulnerability for this organization
                soft_delete_risks_query = """
                MATCH (r:risks)
                WHERE $vuln_id IN r.related_vulnerabilities AND r.organization_id = $org_id
                SET r.is_deleted = 'true'
                RETURN count(r) AS deleted_count
                """
                risks_result = await self.session.run(soft_delete_risks_query, vuln_id=vuln_id, org_id=organization_id)
                risks_record = await risks_result.single()
                deleted_risks = risks_record["deleted_count"] if risks_record else 0
                print(f"[TRIGGER] Soft-deleted {deleted_risks} risks associated with vulnerability {vuln_id}")

                # Remove CAUSES_THREAT relationships and update threat nodes
                remove_threat_rel_query = """
                MATCH (v:vulnerability {id: $vuln_id})-[r:CAUSES_THREAT]->(t:threat)
                WHERE t.vulnerabilities IS NOT NULL AND $vuln_id IN t.vulnerabilities
                SET t.vulnerabilities = [vuln IN t.vulnerabilities WHERE vuln <> $vuln_id]
                DELETE r
                RETURN count(r) AS deleted_relationships
                """
                rel_result = await self.session.run(remove_threat_rel_query, vuln_id=vuln_id)
                rel_record = await rel_result.single()
                deleted_rels = rel_record["deleted_relationships"] if rel_record else 0
                print(f"[TRIGGER] Removed {deleted_rels} CAUSES_THREAT relationships from vulnerability {vuln_id}")

                # Soft-delete the vulnerability - different logic for global vs org-specific
                if is_global_vuln:
                    # Global vulnerability: Add org_id to deleted_for_orgs array
                    soft_delete_vuln_query = """
                    MATCH (v:vulnerability {id: $vuln_id})
                    WHERE v.organization_id IS NULL
                    SET v.deleted_for_orgs = CASE
                        WHEN v.deleted_for_orgs IS NULL THEN [$org_id]
                        WHEN NOT $org_id IN v.deleted_for_orgs THEN v.deleted_for_orgs + $org_id
                        ELSE v.deleted_for_orgs
                    END,
                    v.deleted_by_compliance_orgs = CASE
                        WHEN v.deleted_by_compliance_orgs IS NULL THEN [$org_id]
                        WHEN NOT $org_id IN v.deleted_by_compliance_orgs THEN v.deleted_by_compliance_orgs + $org_id
                        ELSE v.deleted_by_compliance_orgs
                    END
                    RETURN count(v) AS deleted_count
                    """
                    await self.session.run(soft_delete_vuln_query, vuln_id=vuln_id, org_id=organization_id)
                    print(f"[TRIGGER] Added org {organization_id} to deleted_for_orgs for global vulnerability {vuln_id}")
                else:
                    # Org-specific vulnerability: Set is_deleted flag
                    soft_delete_vuln_query = """
                    MATCH (v:vulnerability {id: $vuln_id, organization_id: $org_id})
                    SET v.is_deleted = 'true', v.deleted_by_compliance = 'true'
                    RETURN count(v) AS deleted_count
                    """
                    await self.session.run(soft_delete_vuln_query, vuln_id=vuln_id, org_id=organization_id)
                    print(f"[TRIGGER] Soft-deleted vulnerability {vuln_id} for organization {organization_id}")

                # Soft-delete associated threats that have no other active vulnerabilities
                # Handle both org-specific and global threats
                for threat_id in archived_threats:
                    # Check if threat is global or org-specific
                    check_threat_query = """
                    MATCH (t:threat {id: $threat_id})
                    RETURN t.organization_id AS threat_org_id
                    """
                    threat_check_result = await self.session.run(check_threat_query, threat_id=threat_id)
                    threat_check_record = await threat_check_result.single()
                    threat_org_id = threat_check_record.get("threat_org_id") if threat_check_record else None
                    is_global_threat = threat_org_id is None

                    # Check if there are other active vulnerabilities connected to this threat
                    check_other_vulns_query = """
                    MATCH (v:vulnerability)-[:CAUSES_THREAT]->(t:threat {id: $threat_id})
                    WHERE (
                        (v.organization_id = $org_id AND (v.is_deleted IS NULL OR v.is_deleted = 'false'))
                        OR
                        (v.organization_id IS NULL AND (v.deleted_for_orgs IS NULL OR NOT $org_id IN v.deleted_for_orgs))
                    )
                    RETURN count(v) AS active_vuln_count
                    """
                    check_result = await self.session.run(check_other_vulns_query, threat_id=threat_id, org_id=organization_id)
                    check_record = await check_result.single()
                    active_vuln_count = check_record["active_vuln_count"] if check_record else 0

                    if active_vuln_count == 0:
                        if is_global_threat:
                            # Global threat: Add org_id to deleted_for_orgs array
                            soft_delete_threat_query = """
                            MATCH (t:threat {id: $threat_id})
                            WHERE t.organization_id IS NULL
                            SET t.deleted_for_orgs = CASE
                                WHEN t.deleted_for_orgs IS NULL THEN [$org_id]
                                WHEN NOT $org_id IN t.deleted_for_orgs THEN t.deleted_for_orgs + $org_id
                                ELSE t.deleted_for_orgs
                            END,
                            t.deleted_by_compliance_orgs = CASE
                                WHEN t.deleted_by_compliance_orgs IS NULL THEN [$org_id]
                                WHEN NOT $org_id IN t.deleted_by_compliance_orgs THEN t.deleted_by_compliance_orgs + $org_id
                                ELSE t.deleted_by_compliance_orgs
                            END
                            RETURN count(t) AS deleted_count
                            """
                            await self.session.run(soft_delete_threat_query, threat_id=threat_id, org_id=organization_id)
                            print(f"[TRIGGER] Added org {organization_id} to deleted_for_orgs for global threat {threat_id}")
                        else:
                            # Org-specific threat: Set is_deleted flag
                            soft_delete_threat_query = """
                            MATCH (t:threat {id: $threat_id, organization_id: $org_id})
                            SET t.is_deleted = 'true', t.deleted_by_compliance = 'true'
                            RETURN count(t) AS deleted_count
                            """
                            threat_delete_result = await self.session.run(soft_delete_threat_query, threat_id=threat_id, org_id=organization_id)
                            threat_delete_record = await threat_delete_result.single()
                            deleted_threats = threat_delete_record["deleted_count"] if threat_delete_record else 0
                            if deleted_threats > 0:
                                print(f"[TRIGGER] Soft-deleted threat {threat_id} (no other active vulnerabilities)")
                    else:
                        print(f"[TRIGGER] Threat {threat_id} has {active_vuln_count} other active vulnerabilities, not deleting")

    async def _handle_non_compliant_control_trigger(self, control_id: str, organization_id: str):
        """
        When a control assessment becomes Non-Compliant (reverse of compliant trigger):
        1. Find all soft-deleted vulnerabilities connected to this control for this organization
        2. For each vulnerability, check if ANY connected control is non-compliant for this org
        3. If any control is non-compliant, restore the vulnerability, threat relationships, and associated risks
        """
        print(f"[TRIGGER] Control {control_id} is Non-Compliant. Checking soft-deleted vulnerabilities for org {organization_id}...")

        # Get the control_id value (like "5.1") for this control
        get_control_id_query = """
        MATCH (c:control {id: $control_id})
        RETURN c.control_id AS control_id_value
        """
        control_result = await self.session.run(get_control_id_query, control_id=control_id)
        control_record = await control_result.single()
        control_id_value = control_record["control_id_value"] if control_record else None

        # STEP 1: Reattach this control to all connected vulnerabilities for this organization
        # Find ALL vulnerabilities connected to this control (not just soft-deleted ones)
        all_vuln_query = """
        MATCH (c:control {id: $control_id})-[:MITIGATES]->(v:vulnerability)
        RETURN v.id AS vulnerability_id, v.organization_id AS vuln_org_id
        """
        all_vuln_result = await self.session.run(all_vuln_query, control_id=control_id)
        all_vulnerabilities = await all_vuln_result.data()

        if control_id_value:
            detach_key = f"{control_id_value}:{organization_id}"
            for vuln in all_vulnerabilities:
                vuln_id = vuln["vulnerability_id"]
                # Remove control from detached_controls_by_org
                reattach_control_query = """
                MATCH (v:vulnerability {id: $vuln_id})
                WHERE v.detached_controls_by_org IS NOT NULL AND $detach_key IN v.detached_controls_by_org
                SET v.detached_controls_by_org = [x IN v.detached_controls_by_org WHERE x <> $detach_key]
                RETURN count(v) AS updated
                """
                await self.session.run(reattach_control_query, vuln_id=vuln_id, detach_key=detach_key)
                print(f"[TRIGGER] Reattached control {control_id_value} to vulnerability {vuln_id} for org {organization_id}")

                # Restore risks specifically for this control and vulnerability
                restore_control_risks_query = """
                MATCH (r:risks)
                WHERE $vuln_id IN r.related_vulnerabilities
                AND r.organization_id = $org_id
                AND $control_id IN r.control_ids
                AND r.is_deleted = 'true'
                SET r.is_deleted = 'false'
                RETURN count(r) AS restored_count
                """
                await self.session.run(restore_control_risks_query, vuln_id=vuln_id, org_id=organization_id, control_id=control_id)

        # Find all soft-deleted vulnerabilities connected to this control via MITIGATES relationship
        # Include BOTH org-specific (deleted_by_compliance) AND global (org in deleted_by_compliance_orgs)
        vuln_query = """
        MATCH (c:control {id: $control_id})-[:MITIGATES]->(v:vulnerability)
        WHERE (
            (v.organization_id = $org_id AND v.is_deleted = 'true' AND v.deleted_by_compliance = 'true')
            OR
            (v.organization_id IS NULL AND $org_id IN v.deleted_by_compliance_orgs)
        )
        RETURN v.id AS vulnerability_id, v.organization_id AS vuln_org_id
        """
        result = await self.session.run(vuln_query, control_id=control_id, org_id=organization_id)
        vulnerabilities = await result.data()

        if not vulnerabilities:
            print(f"[TRIGGER] No compliance-deleted vulnerabilities connected to control {control_id} for org {organization_id}")
            return

        for vuln in vulnerabilities:
            vuln_id = vuln["vulnerability_id"]
            vuln_org_id = vuln.get("vuln_org_id")
            is_global_vuln = vuln_org_id is None
            print(f"[TRIGGER] Checking soft-deleted vulnerability {vuln_id} (global={is_global_vuln})...")

            # Get all controls connected to this vulnerability
            controls_query = """
            MATCH (c:control)-[:MITIGATES]->(v:vulnerability {id: $vuln_id})
            RETURN c.id AS control_id, c.control_id AS control_id_value
            """
            controls_result = await self.session.run(controls_query, vuln_id=vuln_id)
            connected_controls = await controls_result.data()

            if not connected_controls:
                continue

            # Check if ANY connected control is non-compliant for this organization
            any_non_compliant = False
            for ctrl in connected_controls:
                ctrl_id_value = ctrl["control_id_value"]

                # Check control_assessment for this control and organization
                assessment_query = """
                MATCH (a:control_assessment {control_id: $control_id, organization_id: $org_id})
                RETURN a.control_compliance AS compliance_status
                """
                assessment_result = await self.session.run(
                    assessment_query,
                    control_id=ctrl_id_value,
                    org_id=organization_id
                )
                assessment_record = await assessment_result.single()

                if assessment_record and assessment_record.get("compliance_status") == "Non Compliant":
                    any_non_compliant = True
                    print(f"[TRIGGER] Control {ctrl_id_value} is non compliant. Will restore vulnerability.")
                    break

            if any_non_compliant:
                print(f"[TRIGGER] At least one control for vulnerability {vuln_id} is non-compliant. Restoring vulnerability and associated risks for organization {organization_id}...")

                # Restore the vulnerability - different logic for global vs org-specific
                if is_global_vuln:
                    # Global vulnerability: Remove org_id from deleted_for_orgs and deleted_by_compliance_orgs
                    restore_vuln_query = """
                    MATCH (v:vulnerability {id: $vuln_id})
                    WHERE v.organization_id IS NULL
                    SET v.deleted_for_orgs = [x IN coalesce(v.deleted_for_orgs, []) WHERE x <> $org_id],
                        v.deleted_by_compliance_orgs = [x IN coalesce(v.deleted_by_compliance_orgs, []) WHERE x <> $org_id]
                    RETURN count(v) AS restored_count
                    """
                    await self.session.run(restore_vuln_query, vuln_id=vuln_id, org_id=organization_id)
                    print(f"[TRIGGER] Removed org {organization_id} from deleted_for_orgs for global vulnerability {vuln_id}")
                else:
                    # Org-specific vulnerability: Set is_deleted to false
                    restore_vuln_query = """
                    MATCH (v:vulnerability {id: $vuln_id, organization_id: $org_id})
                    SET v.is_deleted = 'false'
                    REMOVE v.deleted_by_compliance
                    RETURN count(v) AS restored_count
                    """
                    await self.session.run(restore_vuln_query, vuln_id=vuln_id, org_id=organization_id)
                    print(f"[TRIGGER] Restored vulnerability {vuln_id} for organization {organization_id}")

                # Restore CAUSES_THREAT relationships using archived_threat_ids
                # First, get the archived threat IDs
                get_archived_threats_query = """
                MATCH (v:vulnerability {id: $vuln_id})
                WHERE v.archived_threat_ids IS NOT NULL
                RETURN v.archived_threat_ids AS threat_ids
                """
                archived_result = await self.session.run(get_archived_threats_query, vuln_id=vuln_id)
                archived_record = await archived_result.single()
                archived_threat_ids = archived_record["threat_ids"] if archived_record else []

                for threat_id in archived_threat_ids:
                    # Create CAUSES_THREAT relationship if it doesn't exist
                    restore_rel_query = """
                    MATCH (v:vulnerability {id: $vuln_id})
                    MATCH (t:threat {id: $threat_id})
                    MERGE (v)-[:CAUSES_THREAT]->(t)
                    WITH v, t
                    SET t.vulnerabilities = CASE
                        WHEN t.vulnerabilities IS NULL THEN [$vuln_id]
                        WHEN NOT $vuln_id IN t.vulnerabilities THEN t.vulnerabilities + $vuln_id
                        ELSE t.vulnerabilities
                    END
                    RETURN t.id AS threat_id
                    """
                    await self.session.run(restore_rel_query, vuln_id=vuln_id, threat_id=threat_id)
                    print(f"[TRIGGER] Restored CAUSES_THREAT relationship for vulnerability {vuln_id} -> threat {threat_id}")

                    # Check if threat is global or org-specific
                    check_threat_query = """
                    MATCH (t:threat {id: $threat_id})
                    RETURN t.organization_id AS threat_org_id
                    """
                    threat_check_result = await self.session.run(check_threat_query, threat_id=threat_id)
                    threat_check_record = await threat_check_result.single()
                    threat_org_id = threat_check_record.get("threat_org_id") if threat_check_record else None
                    is_global_threat = threat_org_id is None

                    # Restore the threat if it was soft-deleted by compliance
                    if is_global_threat:
                        # Global threat: Remove org_id from deleted_for_orgs and deleted_by_compliance_orgs
                        restore_threat_query = """
                        MATCH (t:threat {id: $threat_id})
                        WHERE t.organization_id IS NULL AND $org_id IN coalesce(t.deleted_by_compliance_orgs, [])
                        SET t.deleted_for_orgs = [x IN coalesce(t.deleted_for_orgs, []) WHERE x <> $org_id],
                            t.deleted_by_compliance_orgs = [x IN coalesce(t.deleted_by_compliance_orgs, []) WHERE x <> $org_id]
                        RETURN count(t) AS restored_count
                        """
                        threat_restore_result = await self.session.run(restore_threat_query, threat_id=threat_id, org_id=organization_id)
                        threat_restore_record = await threat_restore_result.single()
                        restored_threat_count = threat_restore_record["restored_count"] if threat_restore_record else 0
                        if restored_threat_count > 0:
                            print(f"[TRIGGER] Removed org {organization_id} from deleted_for_orgs for global threat {threat_id}")
                    else:
                        # Org-specific threat: Set is_deleted to false
                        restore_threat_query = """
                        MATCH (t:threat {id: $threat_id, organization_id: $org_id})
                        WHERE t.is_deleted = 'true' AND t.deleted_by_compliance = 'true'
                        SET t.is_deleted = 'false'
                        REMOVE t.deleted_by_compliance
                        RETURN count(t) AS restored_count
                        """
                        threat_restore_result = await self.session.run(restore_threat_query, threat_id=threat_id, org_id=organization_id)
                        threat_restore_record = await threat_restore_result.single()
                        restored_threat_count = threat_restore_record["restored_count"] if threat_restore_record else 0
                        if restored_threat_count > 0:
                            print(f"[TRIGGER] Restored threat {threat_id} for organization {organization_id}")

                    # Deduplicate the vulnerabilities array on the threat
                    dedupe_query = """
                    MATCH (t:threat {id: $threat_id})
                    WHERE t.vulnerabilities IS NOT NULL
                    WITH t, [x IN t.vulnerabilities WHERE x IS NOT NULL | x] AS vuln_list
                    WITH t, reduce(acc = [], x IN vuln_list | CASE WHEN x IN acc THEN acc ELSE acc + x END) AS unique_vulns
                    SET t.vulnerabilities = unique_vulns
                    RETURN count(t) AS updated
                    """
                    await self.session.run(dedupe_query, threat_id=threat_id)

                # Restore all risks associated with this vulnerability for this organization
                restore_risks_query = """
                MATCH (r:risks)
                WHERE $vuln_id IN r.related_vulnerabilities AND r.organization_id = $org_id AND r.is_deleted = 'true'
                SET r.is_deleted = 'false'
                RETURN count(r) AS restored_count
                """
                risks_result = await self.session.run(restore_risks_query, vuln_id=vuln_id, org_id=organization_id)
                risks_record = await risks_result.single()
                restored_risks = risks_record["restored_count"] if risks_record else 0
                print(f"[TRIGGER] Restored {restored_risks} risks associated with vulnerability {vuln_id}")

                # Recreate risks for affected assets to ensure they reflect current state
                # Find all assets that have controls mitigating this vulnerability
                affected_assets_query = """
                MATCH (c:control)-[:MITIGATES]->(v:vulnerability {id: $vuln_id})
                MATCH (a:assets)
                WHERE c.id IN a.security_controls AND a.organization_id = $org_id
                RETURN DISTINCT a
                """
                assets_result = await self.session.run(affected_assets_query, vuln_id=vuln_id, org_id=organization_id)
                assets_records = await assets_result.data()

                for asset_record in assets_records:
                    if asset_record.get("a"):
                        asset_data = dict(asset_record["a"])
                        asset_id = asset_data.get("id")
                        if asset_id:
                            # Delete existing risks for this asset and vulnerability
                            delete_asset_vuln_risks_query = """
                            MATCH (r:risks)
                            WHERE r.associated_assets = $asset_id
                            AND $vuln_id IN r.related_vulnerabilities
                            AND r.organization_id = $org_id
                            DETACH DELETE r
                            """
                            await self.session.run(delete_asset_vuln_risks_query, asset_id=asset_id, vuln_id=vuln_id, org_id=organization_id)

                            # Recreate risks for this asset
                            try:
                                await self.create_risks_for_asset(asset_data)
                                print(f"[TRIGGER] Recreated risks for asset {asset_id}")
                            except Exception as e:
                                print(f"[ERROR] Failed to recreate risks for asset {asset_id}: {e}")

                # Clear archived_threat_ids after successful restoration
                clear_archive_query = """
                MATCH (v:vulnerability {id: $vuln_id})
                REMOVE v.archived_threat_ids
                RETURN count(v) AS cleared_count
                """
                await self.session.run(clear_archive_query, vuln_id=vuln_id)
                print(f"[TRIGGER] Cleared archived threat IDs from vulnerability {vuln_id}")

    async def _check_vulnerability_controls_compliance(self, vuln_id: str, organization_id: str):
        """
        Check if a vulnerability has attached controls that are already compliant.
        If a control is compliant, detach it from the vulnerability for this organization.
        If ALL controls are compliant, soft-delete the vulnerability.
        Called when a vulnerability is created or updated.
        """
        print(f"[CHECK] Checking controls compliance for vulnerability {vuln_id} in org {organization_id}...")

        # Get all controls connected to this vulnerability
        controls_query = """
        MATCH (c:control)-[:MITIGATES]->(v:vulnerability {id: $vuln_id})
        RETURN c.id AS control_id, c.control_id AS control_id_value
        """
        controls_result = await self.session.run(controls_query, vuln_id=vuln_id)
        connected_controls = await controls_result.data()

        if not connected_controls:
            print(f"[CHECK] No controls connected to vulnerability {vuln_id}")
            return

        all_compliant = True
        compliant_controls = []

        for ctrl in connected_controls:
            ctrl_id = ctrl["control_id"]
            ctrl_id_value = ctrl["control_id_value"]

            # Check control_assessment for this control and organization
            assessment_query = """
            MATCH (a:control_assessment {control_id: $control_id, organization_id: $org_id})
            RETURN a.control_compliance AS compliance_status
            """
            assessment_result = await self.session.run(
                assessment_query,
                control_id=ctrl_id_value,
                org_id=organization_id
            )
            assessment_record = await assessment_result.single()

            if assessment_record and assessment_record.get("compliance_status") == "Compliant":
                compliant_controls.append((ctrl_id, ctrl_id_value))
                print(f"[CHECK] Control {ctrl_id_value} is compliant - will detach from vulnerability")
            else:
                all_compliant = False
                print(f"[CHECK] Control {ctrl_id_value} is not compliant")

        # Detach compliant controls from this vulnerability
        for ctrl_id, ctrl_id_value in compliant_controls:
            detach_key = f"{ctrl_id_value}:{organization_id}"
            detach_control_query = """
            MATCH (v:vulnerability {id: $vuln_id})
            SET v.detached_controls_by_org = CASE
                WHEN v.detached_controls_by_org IS NULL THEN [$detach_key]
                WHEN NOT $detach_key IN v.detached_controls_by_org THEN v.detached_controls_by_org + $detach_key
                ELSE v.detached_controls_by_org
            END
            RETURN count(v) AS updated
            """
            await self.session.run(detach_control_query, vuln_id=vuln_id, detach_key=detach_key)
            print(f"[CHECK] Detached compliant control {ctrl_id_value} from vulnerability {vuln_id}")

            # Soft-delete risks for this control
            soft_delete_control_risks_query = """
            MATCH (r:risks)
            WHERE $vuln_id IN r.related_vulnerabilities
            AND r.organization_id = $org_id
            AND $control_id IN r.control_ids
            SET r.is_deleted = 'true'
            RETURN count(r) AS deleted_count
            """
            await self.session.run(soft_delete_control_risks_query, vuln_id=vuln_id, org_id=organization_id, control_id=ctrl_id)

        # If all controls are compliant, soft-delete the vulnerability
        if all_compliant and compliant_controls:
            print(f"[CHECK] All controls for vulnerability {vuln_id} are compliant - soft-deleting vulnerability")

            # Check if this is a global or org-specific vulnerability
            get_vuln_query = """
            MATCH (v:vulnerability {id: $vuln_id})
            RETURN v.organization_id AS vuln_org_id
            """
            vuln_result = await self.session.run(get_vuln_query, vuln_id=vuln_id)
            vuln_record = await vuln_result.single()
            vuln_org_id = vuln_record.get("vuln_org_id") if vuln_record else None
            is_global_vuln = vuln_org_id is None

            # Store threat IDs before removing relationships
            store_threat_ids_query = """
            MATCH (v:vulnerability {id: $vuln_id})-[:CAUSES_THREAT]->(t:threat)
            WITH v, collect(t.id) AS threat_ids
            SET v.archived_threat_ids = threat_ids
            RETURN threat_ids
            """
            store_result = await self.session.run(store_threat_ids_query, vuln_id=vuln_id)
            store_record = await store_result.single()
            archived_threats = store_record["threat_ids"] if store_record else []

            # Soft-delete all risks for this vulnerability
            soft_delete_risks_query = """
            MATCH (r:risks)
            WHERE $vuln_id IN r.related_vulnerabilities AND r.organization_id = $org_id
            SET r.is_deleted = 'true'
            RETURN count(r) AS deleted_count
            """
            await self.session.run(soft_delete_risks_query, vuln_id=vuln_id, org_id=organization_id)

            # Remove CAUSES_THREAT relationships
            remove_threat_rel_query = """
            MATCH (v:vulnerability {id: $vuln_id})-[r:CAUSES_THREAT]->(t:threat)
            WHERE t.vulnerabilities IS NOT NULL AND $vuln_id IN t.vulnerabilities
            SET t.vulnerabilities = [vuln IN t.vulnerabilities WHERE vuln <> $vuln_id]
            DELETE r
            RETURN count(r) AS deleted_relationships
            """
            await self.session.run(remove_threat_rel_query, vuln_id=vuln_id)

            # Soft-delete the vulnerability
            if is_global_vuln:
                soft_delete_vuln_query = """
                MATCH (v:vulnerability {id: $vuln_id})
                WHERE v.organization_id IS NULL
                SET v.deleted_for_orgs = CASE
                    WHEN v.deleted_for_orgs IS NULL THEN [$org_id]
                    WHEN NOT $org_id IN v.deleted_for_orgs THEN v.deleted_for_orgs + $org_id
                    ELSE v.deleted_for_orgs
                END,
                v.deleted_by_compliance_orgs = CASE
                    WHEN v.deleted_by_compliance_orgs IS NULL THEN [$org_id]
                    WHEN NOT $org_id IN v.deleted_by_compliance_orgs THEN v.deleted_by_compliance_orgs + $org_id
                    ELSE v.deleted_by_compliance_orgs
                END
                RETURN count(v) AS deleted_count
                """
                await self.session.run(soft_delete_vuln_query, vuln_id=vuln_id, org_id=organization_id)
                print(f"[CHECK] Added org {organization_id} to deleted_for_orgs for global vulnerability {vuln_id}")
            else:
                soft_delete_vuln_query = """
                MATCH (v:vulnerability {id: $vuln_id, organization_id: $org_id})
                SET v.is_deleted = 'true', v.deleted_by_compliance = 'true'
                RETURN count(v) AS deleted_count
                """
                await self.session.run(soft_delete_vuln_query, vuln_id=vuln_id, org_id=organization_id)
                print(f"[CHECK] Soft-deleted vulnerability {vuln_id} (all controls compliant)")

            # Soft-delete associated threats
            for threat_id in archived_threats:
                # Check if threat has other active vulnerabilities
                check_other_vulns_query = """
                MATCH (v:vulnerability)-[:CAUSES_THREAT]->(t:threat {id: $threat_id})
                WHERE (
                    (v.organization_id = $org_id AND (v.is_deleted IS NULL OR v.is_deleted = 'false'))
                    OR
                    (v.organization_id IS NULL AND (v.deleted_for_orgs IS NULL OR NOT $org_id IN v.deleted_for_orgs))
                )
                RETURN count(v) AS active_vuln_count
                """
                check_result = await self.session.run(check_other_vulns_query, threat_id=threat_id, org_id=organization_id)
                check_record = await check_result.single()
                active_vuln_count = check_record["active_vuln_count"] if check_record else 0

                if active_vuln_count == 0:
                    # Check if threat is global
                    check_threat_query = """
                    MATCH (t:threat {id: $threat_id})
                    RETURN t.organization_id AS threat_org_id
                    """
                    threat_check_result = await self.session.run(check_threat_query, threat_id=threat_id)
                    threat_check_record = await threat_check_result.single()
                    threat_org_id = threat_check_record.get("threat_org_id") if threat_check_record else None
                    is_global_threat = threat_org_id is None

                    if is_global_threat:
                        soft_delete_threat_query = """
                        MATCH (t:threat {id: $threat_id})
                        WHERE t.organization_id IS NULL
                        SET t.deleted_for_orgs = CASE
                            WHEN t.deleted_for_orgs IS NULL THEN [$org_id]
                            WHEN NOT $org_id IN t.deleted_for_orgs THEN t.deleted_for_orgs + $org_id
                            ELSE t.deleted_for_orgs
                        END,
                        t.deleted_by_compliance_orgs = CASE
                            WHEN t.deleted_by_compliance_orgs IS NULL THEN [$org_id]
                            WHEN NOT $org_id IN t.deleted_by_compliance_orgs THEN t.deleted_by_compliance_orgs + $org_id
                            ELSE t.deleted_by_compliance_orgs
                        END
                        RETURN count(t) AS deleted_count
                        """
                        await self.session.run(soft_delete_threat_query, threat_id=threat_id, org_id=organization_id)
                    else:
                        soft_delete_threat_query = """
                        MATCH (t:threat {id: $threat_id, organization_id: $org_id})
                        SET t.is_deleted = 'true', t.deleted_by_compliance = 'true'
                        RETURN count(t) AS deleted_count
                        """
                        await self.session.run(soft_delete_threat_query, threat_id=threat_id, org_id=organization_id)