from pydantic import BaseModel, EmailStr
from typing import Optional

class UserCreate(BaseModel):
    name:str
    email: EmailStr
    password: str
    role: str  # super_admin, partner_user, internal_user
    org_id: Optional[str] = None  # required for partner/internal

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str
    org_id: Optional[str] = None