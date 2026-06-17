import uuid
from sqlalchemy import Column, String, Float, ForeignKey, Boolean, DateTime, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.database import Base

# Helper to use UUID type seamlessly (fallback to String for SQLite)
from sqlalchemy.types import TypeDecorator, CHAR
class GUID(TypeDecorator):
    impl = CHAR
    cache_ok = True
    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(CHAR(32))
    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        elif dialect.name == 'postgresql':
            return str(value)
        else:
            if not isinstance(value, uuid.UUID):
                return "%.32x" % uuid.UUID(value).int
            else:
                return "%.32x" % value.int
    def process_result_value(self, value, dialect):
        if value is None:
            return value
        else:
            if not isinstance(value, uuid.UUID):
                return uuid.UUID(value)
            else:
                return value


class User(Base):
    __tablename__ = "users"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")  # "user" or "admin"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, nullable=True)

    total_active_seconds = Column(Integer, default=0)
    last_heartbeat_at = Column(DateTime, nullable=True)

    courses = relationship("Course", back_populates="owner")
    download_logs = relationship("DownloadLog", back_populates="user")


class Course(Base):
    __tablename__ = "courses"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    title = Column(String, nullable=False)
    scenario_description = Column(Text, nullable=False)
    about_course = Column(Text, nullable=False)
    custom_restrictions = Column(Text, nullable=True)  # per-course prompt restrictions override
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="courses")
    media_sessions = relationship("MediaSession", back_populates="course", cascade="all, delete-orphan")


class MediaSession(Base):
    __tablename__ = "media_sessions"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    course_id = Column(GUID(), ForeignKey("courses.id"), nullable=False)
    title = Column(String, nullable=False)
    transcript_text = Column(Text, nullable=False, default="")
    upload_timestamp = Column(DateTime, default=datetime.utcnow)
    processing_status = Column(String, default="pending")  # pending, processing, completed, failed
    error_message = Column(Text, nullable=True)
    source_type = Column(String, default="video")  # video, transcript
    s3_key = Column(String, nullable=True)  # S3 key for the original uploaded file
    duration_seconds = Column(Float, nullable=True)  # Duration in seconds, calculated from audio segments
    tag = Column(String, nullable=True)  # tag for image filenames (e.g., "cooking" → cooking_timestamp.jpeg)

    course = relationship("Course", back_populates="media_sessions")
    visual_plans = relationship("VisualPlan", back_populates="session", cascade="all, delete-orphan")
    audio_segments = relationship("AudioSegment", back_populates="session", cascade="all, delete-orphan")


class GlobalContext(Base):
    __tablename__ = "global_contexts"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    project_id = Column(GUID(), nullable=True)
    style_preference = Column(String)
    color_palette = Column(String)
    tone = Column(String)
    environmental_context = Column(String)
    demographic_preferences = Column(String)


class GeneratedImage(Base):
    __tablename__ = "generated_images"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    course_id = Column(GUID(), nullable=True)
    plan_id = Column(GUID(), nullable=True)
    local_path = Column(String, nullable=False)
    visual_type = Column(String)
    theme = Column(String)
    concept_description = Column(Text)
    tags = Column(String)
    generation_prompt = Column(Text)
    is_edited = Column(Boolean, default=False)
    embedding_id = Column(String)
    creation_timestamp = Column(DateTime, default=datetime.utcnow)

    download_logs = relationship("DownloadLog", back_populates="image")


class DownloadLog(Base):
    __tablename__ = "download_logs"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    image_id = Column(GUID(), ForeignKey("generated_images.id"), nullable=True)
    downloaded_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="download_logs")
    image = relationship("GeneratedImage", back_populates="download_logs")


class VisualPlan(Base):
    __tablename__ = "visual_plans"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    session_id = Column(GUID(), ForeignKey("media_sessions.id"))
    status = Column(String, default="draft")
    global_context_id = Column(GUID(), ForeignKey("global_contexts.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("MediaSession", back_populates="visual_plans")
    global_context = relationship("GlobalContext")


class AudioSegment(Base):
    __tablename__ = "audio_segments"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    session_id = Column(GUID(), ForeignKey("media_sessions.id"))
    timestamp_start = Column(Float, nullable=False)
    timestamp_end = Column(Float, nullable=False)
    transcript_text = Column(Text)

    session = relationship("MediaSession", back_populates="audio_segments")
    alternative_plans = relationship("SegmentVisualPlan", back_populates="segment", cascade="all, delete-orphan")


class SegmentVisualPlan(Base):
    __tablename__ = "segment_visual_plans"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    segment_id = Column(GUID(), ForeignKey("audio_segments.id"))
    concept = Column(Text)
    category = Column(String, default="action")
    description = Column(Text)
    visual_type = Column(String)
    theme = Column(String)
    tags = Column(String)
    global_context = Column(Text, nullable=True)
    final_prompt = Column(Text, nullable=True)
    selected_image_id = Column(GUID(), ForeignKey("generated_images.id"), nullable=True)
    is_disabled = Column(Boolean, default=False)
    is_curated = Column(Boolean, default=False)

    segment = relationship("AudioSegment", back_populates="alternative_plans")
    selected_image = relationship("GeneratedImage")
