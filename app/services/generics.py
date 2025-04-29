from neo4j import AsyncSession
from services.schema_loader import load_schema
import uuid
from neo4j import AsyncDriver
import json



async def get_link_options_service(
    driver: AsyncDriver,
    document_type: str,
    field: str,
    search_term: str = None,
    filters: str = None,
    limit: int = 10,
    offset: int = 0,
):

    try:
        filter_conditions = []
        params = {
            "limit": limit,
            "offset": offset,
        }

        if search_term:
            filter_conditions.append(f"toLower(n.{field}) CONTAINS toLower($search_term)")
            params["search_term"] = search_term

        if filters:
            filters_dict = json.loads(filters)
            for idx, (key, val) in enumerate(filters_dict.items()):
                filter_key = f"filter_{idx}"
                filter_conditions.append(f"n.{key} = ${filter_key}")
                params[filter_key] = val

        where_clause = " AND ".join(filter_conditions)
        where_query = f"WHERE {where_clause}" if where_clause else ""

        cypher = f"""
        MATCH (n:{document_type})
        {where_query}
        RETURN n.id AS value, n.{field} AS label
        SKIP $offset
        LIMIT $limit
        """

        # Using the session directly for running the query
        result = await driver.run(cypher,params)
        return await result.data()

    except Exception as e:
        raise Exception(f"Error in get_link_options_service: {str(e)}")