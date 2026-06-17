from pydantic import BaseModel, ConfigDict, computed_field
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from uuid import UUID


# ── Auth ──────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str

class RegisterRequest(BaseModel):
    username: str
    password: str

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

ONLINE_THRESHOLD_MINUTES = 2

class UserResponse(BaseModel):
    id: UUID
    username: str
    role: str
    is_active: bool
    created_at: datetime
    last_active_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def is_online(self) -> bool:
        if not self.last_active_at:
            return False
        cutoff = datetime.utcnow() - timedelta(minutes=ONLINE_THRESHOLD_MINUTES)
        return self.last_active_at >= cutoff

class UserStatsResponse(UserResponse):
    course_count: int = 0
    generated_count: int = 0
    edited_count: int = 0
    download_count: int = 0
    total_active_seconds: int = 0
    total_video_duration_seconds: float = 0


# ── Courses ───────────────────────────────────────────────────────────────────

class CourseBase(BaseModel):
    title: str
    scenario_description: str
    about_course: str
    custom_restrictions: Optional[str] = None

class CourseCreate(CourseBase):
    pass

class CourseUpdate(CourseBase):
    pass

class MediaSessionResponse(BaseModel):
    id: UUID
    title: str
    upload_timestamp: datetime
    processing_status: str = "pending"
    source_type: str = "video"
    s3_key: Optional[str] = None
    duration_seconds: Optional[float] = None
    tag: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class CourseResponse(CourseBase):
    id: UUID
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class CourseDetailResponse(CourseResponse):
    media_sessions: List[MediaSessionResponse] = []
    owner_username: Optional[str] = None

    @classmethod
    def from_orm_with_owner(cls, course):
        obj = cls.model_validate(course)
        if course.owner:
            obj.owner_username = course.owner.username
        return obj


# ── Visual Plans ──────────────────────────────────────────────────────────────

class SegmentVisualPlanBase(BaseModel):
    concept: str
    category: str = "action"
    description: str
    visual_type: str
    theme: str
    tags: Optional[str] = ""
    global_context: Optional[str] = ""
    final_prompt: Optional[str] = ""
    is_disabled: bool = False

class SegmentVisualPlanResponse(SegmentVisualPlanBase):
    id: UUID
    selected_image_id: Optional[UUID] = None
    is_curated: bool = False
    model_config = ConfigDict(from_attributes=True)

class AudioSegmentResponse(BaseModel):
    id: UUID
    timestamp_start: float
    timestamp_end: float
    transcript_text: str
    alternative_plans: List[SegmentVisualPlanResponse] = []
    model_config = ConfigDict(from_attributes=True)

class VisualPlanResponse(BaseModel):
    id: UUID
    session_id: UUID
    status: str
    global_context_id: Optional[UUID] = None
    created_at: datetime
    segments: List[AudioSegmentResponse] = []
    course_scenario: Optional[str] = None
    course_about: Optional[str] = None
    course_restrictions: Optional[str] = None
    session_tag: Optional[str] = None

class VisualPlanMutationRequest(BaseModel):
    concept: str
    category: str = "action"
    description: str
    visual_type: str
    tags: str
    global_context: str
    final_prompt: str
    is_disabled: bool = False


# ── Images ────────────────────────────────────────────────────────────────────

class ImageSelectRequest(BaseModel):
    image_id: UUID

class ImageSuggestionResponse(BaseModel):
    id: UUID
    local_path: str
    visual_type: str
    theme: str
    concept_description: str
    tags: str
    is_edited: bool = False
    model_config = ConfigDict(from_attributes=True)

class ImageEditRequest(BaseModel):
    modification: str
    plan_id: Optional[UUID] = None
