from neo4j import AsyncSession
from services.schema_loader import load_schema
import uuid

class GenericCRUD:
    def __init__(self, session: AsyncSession, doctype: str):
        self.session = session
        self.schema = load_schema(doctype)
        self.doctype = doctype 
 
    async def create(self, data: dict):
        # Validate against schema fields
        required_fields = [
            f["fieldname"] for f in self.schema["fields"]
            if f.get("required")
        ]

        for field in required_fields:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

        # Add a unique ID if not present
        if "id" not in data:
            data["id"] = str(uuid.uuid4())

        query = f"""
        CREATE (n:{self.doctype} $data)
        RETURN n
        """
        result = await self.session.run(query, data=data)
        return await result.single()
    
    async def get_all(self):
        query = f"""
        MATCH (n:{self.doctype})
        RETURN n
        """
        result = await self.session.run(query)
        return await result.data()

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

    async def update(self, item_id: str, data: dict):
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

    async def delete(self, item_id: str):
        query = f"""
        MATCH (n:{self.doctype} {{id: $item_id}})
        DETACH DELETE n
        RETURN COUNT(n) AS deleted_count
        """
        result = await self.session.run(query, item_id=item_id)
        deleted = await result.single()
        return deleted["deleted_count"]