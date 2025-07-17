from pydantic import BaseModel, EmailStr
from typing import Optional

class UserCreate(BaseModel):
    name:str
    email: EmailStr
    password: str
    role: str  # super_admin, partner_user, internal_user
    org_id: Optional[str] = None 
    
    
class UserUpdate(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    org_id: Optional[str] = None

class UserLogin(BaseModel):
    email: str
    password: str
    remember_me: bool = False
class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str
    org_id: Optional[str] = None
    
class RefreshTokenRequest(BaseModel):
    refresh_token: str