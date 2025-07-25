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


class GenericCRUD:
    def __init__(self, session: AsyncSession, doctype: str):
        self.session = session
        self.schema = load_schema(doctype)
        self.doctype = doctype

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
    async def get_all(self, skip: int = 0, limit: int = 10, filters: dict = None):
        filters = filters or {}
        where_clauses = []
        params = {"skip": skip, "limit": limit}

        for i, (key, value) in enumerate(filters.items()):
            param_key = f"filter_{i}"
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
        ORDER BY n.created_at ASC
        SKIP $skip
        LIMIT $limit
        """
        data_result = await self.session.run(data_query, **params)
        records = await data_result.data()

        items = []

        for record in records:
            node = dict(record["n"])
            relationships = record["relationships"]

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

        return {"n": node, "id": item_id}