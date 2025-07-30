
from pydantic import BaseModel
from typing import Optional,List


class ComplianceQuestion(BaseModel):
    question: str
    answer: bool
    weight: int

class ComplianceCreate(BaseModel):
    organization_id: str
    control_id: str
    control_effectiveness: Optional[float] = None
    questions: List[ComplianceQuestion]

class ComplianceUpdate(ComplianceCreate):
    id: str
