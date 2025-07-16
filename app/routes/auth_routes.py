from fastapi import APIRouter, Depends, HTTPException
from neo4j import AsyncDriver
from uuid import uuid4
from neo4j import AsyncSession
from services.database import get_db
from services.dependencies import get_current_user

from schemas.user_schema import UserCreate, UserLogin
from services.auth_service import hash_password, verify_password, create_access_token

# router = APIRouter(prefix="/auth", tags=["Auth"])
router = APIRouter()

@router.post("/register")
async def register(user: UserCreate,  session: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    # Step 1: Check if user already exists
    
    creator_role = current_user.get("role")
    target_role = user.role

    if creator_role == "user":
        raise HTTPException(status_code=403, detail="Users cannot create other users")
    
    if creator_role == "partner" and target_role != "user":
        raise HTTPException(
            status_code=403,
            detail="Partners can only create users"
        )

    if creator_role == "super_admin" and target_role not in ["partner", "user"]:
        raise HTTPException(
            status_code=403,
            detail="Super Admin can only create users or partners"
        )

    
    
    check_query = "MATCH (u:User {email: $email}) RETURN u"
    result = await session.run(check_query, email=user.email)
    if await result.single():
        raise HTTPException(status_code=400, detail="User already exists")

    # Step 2: Generate user ID using Counter
    counter_query = """
    MERGE (c:Counter {doctype: 'User'})
    ON CREATE SET c.current = 1
    ON MATCH SET c.current = c.current + 1
    RETURN c.current AS new_id
    """
    counter_result = await session.run(counter_query)
    counter_record = await counter_result.single()
    new_id = counter_record["new_id"]
    user_id = f"user-{new_id}"

    # Step 3: Create User node
    create_user_query = """
    CREATE (u:User {
        id: $id,
        email: $email,
        password: $password,
        role: $role,
        org_id: $org_id
    }) RETURN u
    """
    await session.run(create_user_query, id=user_id, email=user.email,
                      password=hash_password(user.password),
                      role=user.role, org_id=user.org_id)

    # Step 4: Create relation from User → Organization
    relation_query = """
    MATCH (u:User {id: $user_id})
    MATCH (o:Organization {id: $org_id})
    MERGE (u)-[:ASSOCIATE_WITH]->(o)
    """
    await session.run(relation_query, user_id=user_id, org_id=user.org_id)

    return {"msg": "User registered successfully", "id": user_id}

@router.post("/login")
async def login(user: UserLogin, session: AsyncSession = Depends(get_db)):
    query = "MATCH (u:User {email: $email}) RETURN u"
    result = await session.run(query, email=user.email)
    record = await result.single()

    if not record:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    db_user = record["u"]
    if not verify_password(user.password, db_user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token_data = {
        "user_id": db_user["id"],
        "email": db_user["email"],
        "role": db_user["role"],
        "org_id": db_user.get("org_id")
    }

    access_token = create_access_token(token_data)
    return {"access_token": access_token, "token_type": "bearer"}