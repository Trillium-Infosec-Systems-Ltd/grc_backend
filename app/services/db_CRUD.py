from neo4j import AsyncSession
from services.schema_loader import load_schema
import uuid
from neo4j import AsyncDriver
import json
from datetime import datetime

class GenericCRUD:
    def __init__(self, session: AsyncSession, doctype: str):
        self.session = session
        self.schema = load_schema(doctype)
        self.doctype = doctype 
 
    async def create(self, data: dict):
        # Validate required fields from schema
        required_fields = [
            f["fieldname"] for f in self.schema["fields"]
            if f.get("required")
        ]

        for field in required_fields:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

        # Use Counter node for incremental ID
        id_query = f"""
        MERGE (c:Counter {{doctype: $doctype}})
        ON CREATE SET c.current = 1
        ON MATCH SET c.current = c.current + 1
        RETURN c.current AS new_id
        """
        result = await self.session.run(id_query, doctype=self.doctype)
        record = await result.single()
        new_id = record["new_id"]
        data["id"] = self.doctype+'-'+str(new_id)

        # Add timestamps
        now = datetime.utcnow().isoformat()
        data["created_at"] = now
        data["updated_at"] = now

        # Create node
        query = f"""
        CREATE (n:{self.doctype} $data)
        RETURN n
        """
        result = await self.session.run(query, data=data)
        return await result.single()
    
    async def get_all(self, skip: int = 0, limit: int = 10):
        # 1. Get total count
        count_query = f"MATCH (n:{self.doctype}) RETURN count(n) AS total"
        count_result = await self.session.run(count_query)
        count_record = await count_result.single()
        total = count_record["total"]

        # 2. Get paginated data
        data_query = f"""
        MATCH (n:{self.doctype})
        RETURN n
        ORDER BY n.created_at DESC
        SKIP $skip
        LIMIT $limit
        """
        data_result = await self.session.run(data_query, skip=skip, limit=limit)
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
    



# --- Service Logic for Link Options ---
