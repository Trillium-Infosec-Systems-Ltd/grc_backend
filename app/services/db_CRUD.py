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
# from routes.generics import compute_threat_info
from services.risk_calculator import compute_threat_info
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
        Given an asset node data, compute threats and create associated risks.
        This can be reused by both single and bulk asset creation.
        """
        security_controls = asset_data["security_controls"]
        all_threat_ids = set()

        for cont in security_controls:
            query = """
            MATCH (t:threat)
            WHERE $cont IN t.relevant_controls
            RETURN t.id AS threat_id
            """
            result = await self.session.run(query, cont=cont)
            async for record in result:
                all_threat_ids.add(record["threat_id"])

        for threat_id in all_threat_ids:
            db_generator = get_db()
            db = await anext(db_generator)
            try:
                calculated_risk = await compute_threat_info(threat_id, asset_data["asset_value"], db)

                # Generate ID for risk node
                id_query = """
                MERGE (c:Counter {doctype: $doctype})
                ON CREATE SET c.current = 1
                ON MATCH SET c.current = c.current + 1
                RETURN c.current AS new_id
                """
                result = await self.session.run(id_query, doctype="risks")
                record = await result.single()
                new_id = record["new_id"]
                risk_id = f"risks-{new_id}"

                risk_data = {
                    "id": risk_id,
                    "risk_by_asset": "Name",
                    "associated_assets": asset_data["id"],
                    "type": asset_data["type"],
                    "asset_value": asset_data["asset_value"],
                    "associated_threats": threat_id,
                    "threat_probability": calculated_risk["likelihood"],
                    "related_vulnerabilities": calculated_risk["vulnerabilities"],
                    "control_ids": cont,
                    "ease_of_exploitation": calculated_risk["ease_of_exploitation"],
                    "residual_risk": calculated_risk["risk"]
                }

                # Create node
                create_query = """
                CREATE (n:risks $data)
                RETURN n
                """
                await self.session.run(create_query, data=risk_data)

                # Create relationships
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

            finally:
                await db_generator.aclose()


    async def create(self, data: dict):

        # Special case for 'control_question' with multiple questions
        if self.doctype == "control_question" and isinstance(data.get("question"), list):
            control_id = data.get("control_id")

            query ="""
                MATCH(c:control {id:$control_id})
                RETURN c.control_id as control
            """

            result = await self.session.run(query, control_id=control_id)
            record = await result.single()


            if record:
                control = record["control"]
            else:
                control = control_id

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
                "created_at": now,
                "updated_at": now
            }

            # ✅ Unpack manually in query
            create_query = f"""
            CREATE (n:{self.doctype} {{
                id: $id,
                control: $control,
                questions_text: $questions_text,
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


        # Special handling for control: calculate ease_of_exploitation (case-insensitive)
        if self.doctype == "control" and "rating" in data:
            ease_map = {
                "High": "Low",
                "Medium": "Medium",
                "Low": "High"
            }
            rating_val = str(data["rating"]).strip().lower()
            ease_of_exploitation = ease_map.get(rating_val, "Unknown")
            data["ease_of_exploitation"] = ease_of_exploitation

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
            await self.create_risks_for_asset(data)


        return {"n": node, "id": data["id"]}
    async def get_all(self, current_user,skip: int = 0, limit: int = 10, filters: dict = None):
        filters = filters or {}
        where_clauses = []
        params = {"skip": skip, "limit": limit}
        org_id = current_user.get("org_id")
        filterable_fields = {f["fieldname"]: f for f in self.schema["fields"] if f.get("is_filter")
    }
        for i, (key, value) in enumerate(filters.items()):
            if key not in filterable_fields:
                continue  # only allow fields marked as is_filter

            param_key = f"filter_{i}"
            field_info = filterable_fields[key]
            fieldtype = field_info.get("fieldtype", "Data")

            # --- Text fields ---
            if fieldtype in ["Data", "LongText"]:
                # if frontend sent a number (like control_id=7.1), don’t apply toLower
                try:
                    float(value)
                    # exact numeric match
                    where_clauses.append(f"n.{key} = ${param_key}")
                except ValueError:
                    # normal string → case-insensitive contains search
                    where_clauses.append(f"toLower(toString(n.{key})) CONTAINS toLower(${param_key})")

            # --- Dropdowns / Radios ---
            elif fieldtype in ["Radio", "Select"]:
                where_clauses.append(f"n.{key} = ${param_key}")

            # --- Dates ---
            elif fieldtype == "Date":
                if filters.get(f"{key}_min"):
                    where_clauses.append(f"n.{key} >= ${param_key}_min")
                    params[f"{param_key}_min"] = filters[f"{key}_min"]
                if filters.get(f"{key}_max"):
                    where_clauses.append(f"n.{key} <= ${param_key}_max")
                    params[f"{param_key}_max"] = filters[f"{key}_max"]
                continue

            # --- Default fallback (exact match) ---
            else:
                where_clauses.append(f"n.{key} = ${param_key}")

            params[param_key] = value

        where_str = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""


        # Get total count
        count_query = f"""
        MATCH (n:{self.doctype})
        {where_str}
        RETURN count(n) AS total
        """
        count_result = await self.session.run(count_query, **params)
        total = (await count_result.single())["total"]

        # Get main data with incoming/outgoing relationships
        data_query = f"""
        MATCH (n:{self.doctype})
        {where_str}
        OPTIONAL MATCH (source)-[r1]->(n)
        OPTIONAL MATCH (n)-[r2]->(target)
        RETURN n,
            collect({{
                direction: "incoming",
                type: type(r1),
                node: source
            }}) +
            collect({{
                direction: "outgoing",
                type: type(r2),
                node: target
            }}) AS relationships
        ORDER BY n.created_at DESC
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
                control_assesment_flag = False
                if org_id and control_id:
                    assessment_query = """
                        MATCH (a:control_assessment {control_id: $control_id, organization_id: $org_id})
                        RETURN a
                    """
                    result = await self.session.run(assessment_query, control_id=control_id, org_id=org_id)
                    record = await result.single()
                    if record and record.get("a"):
                        print("record is there actually")
                        assessment = record["a"]
                        node["rating"] = assessment.get("control_rating", "Low")
                        node["compliance_status"] = assessment.get("control_compliance", "Non-Compliant")
                        control_assesment_flag = True
                
            
            # Enhance node fields by resolving target_field values
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
                    # import pdb;pdb.set_trace()
                    node[fieldname] = matched_nodes[0].get(target_field) if matched_nodes else None
                elif fieldtype == "MultiLink":
                    node[fieldname] = ", ".join([n.get(target_field) for n in matched_nodes if target_field in n])

                if self.doctype == "control":
                    if  control_assesment_flag:
                        print("control_Assessed")
                    else:
                        print("control not Assessed")

                        node["rating"] =  "Low"
                        node["compliance_status"] = "Non-Compliant"
    
                    
                    

            items.append({
                "node": node,
                "relationships": relationships
            })
            
        # items = json.loads(json.dumps(items, allow_nan=False))

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
        OPTIONAL MATCH (n)-[r2]->(target)

        RETURN n,
            collect({{
                direction: "incoming",
                type: type(r1),
                node: source
            }}) + 
            collect({{
                direction: "outgoing",
                type: type(r2),
                node: target
            }}) AS relationships
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

    async def update(self, item_id: str, data: dict):
        now = datetime.utcnow().isoformat()
        data["updated_at"] = now

        # ✅ Special handling for control_question
        if self.doctype == "control_question" and isinstance(data.get("question"), list):
            control_id = data.get("control_id")
            question_list = data.get("question", [])

            if not control_id or not question_list:
                raise ValueError("Missing 'control' or 'question' list in update")

            # Transform into flat structure for storage
            update_data = {
                "control_id": control_id,
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
            ease_map = {
                "High": "Low",
                "Medium": "Medium",
                "Low": "High"
            }
            ease_of_exploitation = ease_map.get(str(data['rating']).strip(), "Unknown")

            data["ease_of_exploitation"] =ease_of_exploitation
            print("++++++++++++++++++ current user id is",self.current_user.get("org_id"))
   
            assessment_data = {
                "control_id": data["control_id"],
                "organization_id": self.current_user.get("org_id"),
                "control_assessment": data["control_assessment"],
                "control_rating": data["rating"],
                "control_compliance": data["compliance_status"],
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

            asset_data = {**data, "id": item_id}
            await self.create_risks_for_asset(asset_data)

        return {"n": node, "id": item_id}