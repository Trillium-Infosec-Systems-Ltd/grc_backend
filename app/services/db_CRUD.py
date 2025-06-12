# this is a comment
from neo4j import AsyncSession
from services.schema_loader import load_schema
import uuid
from neo4j import AsyncDriver
import json
from datetime import datetime
from typing import Any, Dict




class GenericCRUD:
    def __init__(self, session: AsyncSession, doctype: str):
        self.session = session
        self.schema = load_schema(doctype)
        self.doctype = doctype 
 
    async def create(self, data: dict):
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
        data["id"] = f"{self.doctype}-{new_id}"

        # Add timestamps
        now = datetime.utcnow().isoformat()
        data["created_at"] = now
        data["updated_at"] = now

        # Create the main node
        query = f"""
        CREATE (n:{self.doctype} $data)
        RETURN n
        """
        result = await self.session.run(query, data=data)
        record = await result.single()
        node = record["n"]

        # RELATIONSHIP CREATION WITH DUPLICATE PREVENTION
        created_relationships = set()
# SIMPLIFIED RELATIONSHIP CREATION
        for field in self.schema["fields"]:
            fieldtype = field.get("fieldtype")
            if fieldtype not in ["Link", "MultiLink"]:
                continue

            fieldname = field.get("fieldname")
            target_value = data.get(fieldname)
            if not target_value:
                continue

            target_doctype = field["link_to"]
            relationship_type = field.get("relationship_type", "RELATED_TO").upper()
            direction = field.get("relationship_direction", "outgoing")

            # For MultiLink, ensure list and remove duplicates
            if fieldtype == "MultiLink":
                values = list(set(target_value)) if isinstance(target_value, list) else [target_value]
            else:
                values = [target_value]

            for val in values:
                if direction == "incoming":
                    relation_query = f"""
                    MATCH (target:{target_doctype} {{id: $val}})
                    MATCH (source:{self.doctype} {{id: $source_id}})
                    MERGE (target)-[r:{relationship_type}]->(source)
                    RETURN r
                    """
                else:
                    relation_query = f"""
                    MATCH (target:{target_doctype} {{id: $val}})
                    MATCH (source:{self.doctype} {{id: $source_id}})
                    MERGE (source)-[r:{relationship_type}]->(target)
                    RETURN r
                    """

                result = await self.session.run(
                    relation_query,
                    val=val,
                    source_id=data["id"]
                )
                await result.consume()

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
        # Add updated timestamp
        now = datetime.utcnow().isoformat()
        data["updated_at"] = now

        # Update node properties
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

        # Remove existing Link/MultiLink relationships only for fields being updated
        for field in self.schema["fields"]:
            if field.get("fieldtype") not in ["Link", "MultiLink"]:
                continue

            fieldname = field.get("fieldname")
            if fieldname not in data:
                continue  # Only update if field is provided in the update request

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

        # Re-create Link/MultiLink relationships
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

            # Always match using 'id' as target_field
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