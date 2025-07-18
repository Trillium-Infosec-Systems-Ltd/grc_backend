import asyncio
from uuid import uuid4
from services.database import get_db
from services.auth_service import hash_password
from neo4j import AsyncSession
from contextlib import asynccontextmanager

# Super admin credentials
SUPER_ADMIN_EMAIL = "admin@example.com"
SUPER_ADMIN_PASSWORD = "admin123"
SUPER_ADMIN_ROLE = "super_admin"

async def create_super_admin(db: AsyncSession):
    try:
        # Check if super admin already exists
        check_query = """
        MATCH (u:users {role: $role})
        RETURN count(u) AS count
        """
        result = await db.run(check_query, role=SUPER_ADMIN_ROLE)
        record = await result.single()
        if record and record["count"] > 0:
            print("✅ Super admin already exists.")
            return

        # Create super admin
        user_id = f"superadmin-{str(uuid4())[:8]}"
        hashed_pwd = hash_password(SUPER_ADMIN_PASSWORD)

        create_query = """
        CREATE (u:users {
            id: $id,
            email: $email,
            password: $password,
            role: $role,
            org_id: $org_id
        })
        RETURN u
        """
        params = {
            "id": user_id,
            "email": SUPER_ADMIN_EMAIL,
            "password": hashed_pwd,
            "role": SUPER_ADMIN_ROLE,
            "org_id": None
        }

        result = await db.run(create_query, **params)
        record = await result.single()
        print("✅ Super admin created:", record["u"])

    except Exception as e:
        print(f"❌ Error during super admin creation: {str(e)}")

async def main():
    async for db in get_db():
        await create_super_admin(db)

if __name__ == "__main__":
    asyncio.run(main())
