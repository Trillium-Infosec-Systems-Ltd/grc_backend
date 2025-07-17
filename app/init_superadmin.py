import asyncio
from uuid import uuid4
from services.database import get_db  # your existing DB connection logic
from services.auth_service import  hash_password # your existing hashing logic
from neo4j import AsyncSession

# Super admin credentials
SUPER_ADMIN_EMAIL = "admin@example.com"
SUPER_ADMIN_PASSWORD = "admin123"
SUPER_ADMIN_ROLE = "super_admin"

async def create_super_admin(db: AsyncSession):
    user_id = f"superadmin-{str(uuid4())[:8]}"
    hashed_pwd = hash_password(SUPER_ADMIN_PASSWORD)

    query = """
    CREATE (u:User {
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

    try:
        result = await db.run(query, **params)
        record = await result.single()
        print("✅ Super admin created successfully:")
        print(record["u"])
    except Exception as e:
        print(f"❌ Failed to create super admin: {str(e)}")

if __name__ == "__main__":
    async def main():
        db = await anext(get_db())
        await create_super_admin(db)
        await db.close()

    asyncio.run(main())