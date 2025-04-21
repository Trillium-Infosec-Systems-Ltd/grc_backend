from neo4j import AsyncSession
from services.schema_loader import load_schema

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
