from pydantic import BaseModel, EmailStr
from typing import Optional


class UserCreate(BaseModel):
    name: str
    username: str
    email: EmailStr
    password: str
    role: str  # super_admin, partner_user, internal_user
    org_id: Optional[str] = None
    date_of_birth: Optional[str] = None
    present_address: Optional[str] = None
    permanent_address: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    # password: Optional[str] = None
    role: Optional[str] = None
    org_id: Optional[str] = None
    date_of_birth: Optional[str] = None
    present_address: Optional[str] = None
    permanent_address: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


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