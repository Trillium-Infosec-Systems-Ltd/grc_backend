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

        # Create node and return it
        query = f"""
        CREATE (n:{self.doctype} $data)
        RETURN n
        """
        result = await self.session.run(query, data=data)
        record = await result.single()
        node = record["n"]  # This line caused error earlier if no record

        # Create Link and MultiLink relationships
        for field in self.schema["fields"]:
            if field.get("fieldtype") not in ["Link", "MultiLink"]:
                continue

            fieldname = field.get("fieldname")
            target_value = data.get(fieldname)
            if not target_value:
                continue

            target_doctype = field["link_to"]
            target_field = field["target_field"]
            relationship_type = field.get("relationship_type", "RELATED_TO").upper()
            direction = field.get("relationship_direction", "outgoing")

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
                await self.session.run(relation_query, val=val, source_id=data["id"])

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

        print("Filters received:", filters)
        print("Final params:", params)
        print("Where clause:", where_str)

        count_query = f"""
        MATCH (n:{self.doctype})
        {where_str}
        RETURN count(n) AS total
        """
        count_result = await self.session.run(count_query, **params)
        total = (await count_result.single())["total"]

        data_query = f"""
        MATCH (n:{self.doctype})
        {where_str}
        RETURN n
        ORDER BY n.created_at DESC
        SKIP $skip
        LIMIT $limit
        """
        data_result = await self.session.run(data_query, **params)
        records = await data_result.data()

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": [record["n"] for record in records]
        }
    async def get_by_id(self, item_id: str):
        query = f"""
        MATCH (n:{self.doctype} {{id: $item_id}})
        RETURN n
        """
        result = await self.session.run(query, item_id=item_id)
        record = await result.single()
        if record:
            return record["n"]
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

        query = f"""
        MATCH (n:{self.doctype} {{id: $item_id}})
        SET n += $data
        RETURN n
        """
        result = await self.session.run(query, item_id=item_id, data=data)
        record = await result.single()
        if record:
            return record["n"]
        return None



