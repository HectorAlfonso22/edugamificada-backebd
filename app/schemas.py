from pydantic import BaseModel, Field
from typing import Optional, Any, Literal
from uuid import UUID

Subject = Literal["castellano", "matematica"]

class AttemptIn(BaseModel):
    student_id: UUID
    item_id: UUID
    subject: Subject              # <- IMPORTANTE: este campo debe existir
    is_correct: bool
    response: Optional[Any] = None
    time_ms: Optional[int] = None

class ProgressOut(BaseModel):
    student_id: UUID
    subject: Subject
    total_attempts: int
    correct_attempts: int
    accuracy: float
    xp: int
    level: int
    current_streak: int
    best_streak: int
    last_answer_at: Optional[str] = None

class AttemptOut(BaseModel):
    attempt_id: int = Field(..., alias="id")
    saved: bool
    progress: ProgressOut
