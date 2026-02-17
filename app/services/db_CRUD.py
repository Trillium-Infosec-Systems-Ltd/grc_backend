# this is a comment
from neo4j import AsyncSession
from services.schema_loader import load_schema
import uuid
from neo4j import AsyncDriver
import json
from datetime import datetime
from typing import Any, Dict
import json
from fastapi import APIRouter, Depends
from services.database import get_db
import json
from fastapi.responses import JSONResponse


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
        
        # Query to get all combinations: control -> vulnerability -> threat
        # for the given controls attached to this asset
        chain_query = """
        MATCH (c:control)-[:MITIGATES]->(v:vulnerability)-[:CAUSES_THREAT]->(t:threat)
        WHERE c.id IN $control_ids
        RETURN c.id AS control_id, 
               c.rating AS control_rating,
               v.id AS vulnerability_id,
               v.vulnerability_name AS vulnerability_name,
               v.ease_of_exploitation AS vuln_ease,
               t.id AS threat_id,
               t.likelihood AS threat_likelihood
        """
        
        result = await self.session.run(chain_query, control_ids=security_controls)
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
                "residual_risk": residual_risk
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

        # Special case for 'control_question' with multiple questions
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
                control = record["control_id_value"]  # e.g. "5.10"
            else:
                control = control_id  # Fallback to passed value
            
            # Check if control_question already exists for this control
            check_query = """
                MATCH(c:control_question {control:$control})
                RETURN c.id as control_question
            """
            result = await self.session.run(check_query, control=control)
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

            # Create relationship
            relation_query = f"""
            MATCH (target:control {{id: $control_id}})
            MATCH (source:{self.doctype} {{id: $question_id}})
            MERGE (target)-[:HAS_QUESTION]->(source)
            """
            await self.session.run(relation_query, control_id=control_id, question_id=node_id)

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

        return {"n": node, "id": data["id"]}
    
    async def get_all(self, current_user, skip: int = 0, limit: int = 10, filters: dict = None):
        filters = filters or {}
        where_clauses = []
        params = {"skip": skip, "limit": limit}
        org_id = current_user.get("org_id")

        filterable_fields = {f["fieldname"]: f for f in self.schema["fields"] if f.get("is_filter")}
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

        where_str = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        print(f"[get_all] doctype={self.doctype}, where_str={where_str}, params={params}")

        # Get total count
        count_query = f"""
        MATCH (n:{self.doctype})
        {where_str}
        RETURN count(n) AS total
        """
        count_result = await self.session.run(count_query, **params)
        total = (await count_result.single())["total"]

        # ✅ Order by control_id if doctype is 'control' - handle version-style IDs like 5.1, 5.10
        if self.doctype == "control":
            # Split control_id by '.' and sort numerically (e.g., 5.1, 5.2, ..., 5.10)
            order_clause = """ORDER BY 
                toInteger(split(toString(n.control_id), '.')[0]) ASC,
                CASE WHEN size(split(toString(n.control_id), '.')) > 1 
                     THEN toInteger(split(toString(n.control_id), '.')[1]) 
                     ELSE 0 END ASC"""
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
        data_query = f"""
        MATCH (n:{self.doctype})
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

        for record in records:
            node = dict(record["n"])
            relationships = record["relationships"]

            if self.doctype == "control":
                control_id = node.get("control_id")
                print(repr(control_id))
                control_assesment_flag = False
                if org_id and control_id:
                    assessment_query = """
                        MATCH (a:control_assessment {control_id: $control_id, organization_id: $org_id})
                        RETURN a
                    """
                    result = await self.session.run(assessment_query, control_id=control_id, org_id=org_id)
                    record = await result.single()
                    if record and record.get("a"):
                        assessment = record["a"]
                        node["rating"] = assessment.get("control_rating", "Low")
                        node["compliance_status"] = assessment.get("control_compliance", "Non Compliant")
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
                    node[fieldname] = ", ".join([str(n.get(target_field)) for n in matched_nodes if target_field in n])

                if self.doctype == "control":
                    if not control_assesment_flag:
                        node["rating"] = "Low"
                        node["compliance_status"] = "Non-Compliant"

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

        # ✅ Special handling for control_question
        if self.doctype == "control_question" and isinstance(data.get("question"), list):
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

            # Delete existing HAS_QUESTION relationships
            delete_rel_query = f"""
            MATCH (n:{self.doctype} {{id: $item_id}})<-[r:HAS_QUESTION]-(:control)
            DELETE r
            """
            await self.session.run(delete_rel_query, item_id=item_id)

            # Create updated relationship to control
            relation_query = f"""
            MATCH (target:control {{control_id: $control_id}})
            MATCH (source:{self.doctype} {{id: $item_id}})
            MERGE (target)-[:HAS_QUESTION]->(source)
            """
            await self.session.run(relation_query, control_id=control_id, item_id=item_id)

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
                "next_review_date": data.get("next_review_date", ""),
                "control_applicable": data.get("control_applicable", "Yes"),
                "control_rating": data["rating"],
                "control_compliance":"Non-Compliant",
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

        return {"n": node, "id": item_id}