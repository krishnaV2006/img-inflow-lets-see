import os
import shutil
from datetime import datetime
from fastapi import FastAPI, File, UploadFile, Depends, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session, selectinload
from uuid import UUID

from backend.database import engine, Base, get_db
from backend.models import Course, MediaSession, VisualPlan, AudioSegment, SegmentVisualPlan, GeneratedImage, GlobalContext, User, DownloadLog
from backend.schemas import (
    CourseCreate, CourseUpdate, CourseResponse, CourseDetailResponse,
    VisualPlanResponse, SegmentVisualPlanResponse, VisualPlanMutationRequest,
    ImageSelectRequest, ImageSuggestionResponse, ImageEditRequest,
    LoginRequest, RegisterRequest, TokenResponse, UserCreate, UserUpdate, UserResponse, UserStatsResponse, ChangePasswordRequest,
)
from backend.auth import hash_password, verify_password, create_access_token, get_current_user, require_admin
from backend.services.gemini_service import extract_transcript_from_audio, parse_and_chunk_transcript, parse_srt_to_transcript, curate_visual_plans
from backend.services.image_service import generate_image, edit_image
from backend.services import s3_service

# Create DB tables
Base.metadata.create_all(bind=engine)

# Migrations: add columns that may not exist in older DBs
_migrations = [
    "ALTER TABLE media_sessions ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'video'",
    "ALTER TABLE media_sessions ADD COLUMN IF NOT EXISTS s3_key TEXT",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS user_id CHAR(32) REFERENCES users(id)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP",
    "ALTER TABLE segment_visual_plans ADD COLUMN IF NOT EXISTS is_curated BOOLEAN DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_active_seconds INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMP",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS custom_restrictions TEXT",
    "ALTER TABLE media_sessions ADD COLUMN IF NOT EXISTS duration_seconds FLOAT",
    "ALTER TABLE media_sessions ADD COLUMN IF NOT EXISTS tag TEXT",
]
try:
    from sqlalchemy import text as sa_text
    for _sql in _migrations:
        # Each migration runs in its own connection+transaction so a failure
        # (e.g. "column already exists") in one doesn't abort the others.
        try:
            with engine.connect() as _conn:
                _conn.execute(sa_text(_sql))
                _conn.commit()
        except Exception:
            pass  # Column already exists — safe to ignore
except Exception:
    pass


def _seed_admin():
    """Create the admin user from env vars if it doesn't exist yet."""
    from backend.database import SessionLocal
    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == admin_username).first():
            admin = User(
                username=admin_username,
                password_hash=hash_password(admin_password),
                role="admin",
                is_active=True,
            )
            db.add(admin)
            db.commit()
            print(f"[startup] Admin user '{admin_username}' created.")
        else:
            print(f"[startup] Admin user '{admin_username}' already exists.")
    finally:
        db.close()


_seed_admin()

app = FastAPI(title="Visual Generation AI API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("/app/backend/local_storage/images", exist_ok=True)


@app.get("/api/v1/debug/s3-status")
def s3_status():
    """Quick diagnostic — shows whether S3 is configured and reachable."""
    import os
    configured = s3_service.is_configured()
    result = {
        "s3_configured": configured,
        "bucket": os.getenv("S3_BUCKET_NAME") or "(not set)",
        "region": os.getenv("AWS_REGION") or "(not set)",
        "key_present": bool(os.getenv("AWS_ACCESS_KEY_ID")),
        "secret_present": bool(os.getenv("AWS_SECRET_ACCESS_KEY")),
    }
    if configured:
        # Try a lightweight API call to confirm credentials work
        try:
            import boto3
            client = boto3.client(
                "s3",
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION", "ap-south-1"),
            )
            client.head_bucket(Bucket=os.getenv("S3_BUCKET_NAME"))
            result["bucket_reachable"] = True
        except Exception as e:
            result["bucket_reachable"] = False
            result["error"] = str(e)
    return result


@app.get("/images/{filename}")
async def serve_image(filename: str):
    """
    Serve a generated image.
    - If S3 is configured: redirect the client to a presigned S3 URL (1-hour TTL).
    - Otherwise: serve the file directly from local storage.
    """
    # Sanitise to prevent path traversal
    filename = os.path.basename(filename)

    if s3_service.is_configured():
        url = s3_service.get_presigned_url(filename, expiration=3600)
        if url:
            return RedirectResponse(url, status_code=302)

    # Local fallback (no S3 or presign failed)
    local_path = f"/app/backend/local_storage/images/{filename}"
    if os.path.exists(local_path):
        return FileResponse(local_path, media_type="image/png")

    raise HTTPException(status_code=404, detail="Image not found")


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.post("/api/v1/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    token = create_access_token(str(user.id), user.username, user.role)
    return TokenResponse(access_token=token, role=user.role, username=user.username)


@app.post("/api/v1/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(status_code=409, detail="Username already taken")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        role="user",
        is_active=False,
    )
    db.add(user)
    db.commit()
    return {"detail": "Account created. Please wait for an admin to activate it before signing in."}


@app.get("/api/v1/auth/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@app.post("/api/v1/users/me/change-password")
def change_password(req: ChangePasswordRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not verify_password(req.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current_user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"detail": "Password updated successfully"}


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.post("/api/v1/heartbeat")
def heartbeat(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Called by the frontend every 30s to track total active time per user."""
    now = datetime.utcnow()
    if current_user.last_heartbeat_at:
        delta = (now - current_user.last_heartbeat_at).total_seconds()
        # Only accumulate if last heartbeat was within 90s (3× interval) — avoids
        # counting long idle gaps (e.g. laptop sleep, overnight tab left open)
        if 0 < delta <= 90:
            current_user.total_active_seconds = (current_user.total_active_seconds or 0) + int(delta)
    current_user.last_heartbeat_at = now
    db.commit()
    return {"ok": True}


@app.get("/api/v1/admin/daily-generations")
def admin_daily_generations(days: int = 30, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Returns per-day image generation counts for the last N days."""
    from sqlalchemy import func
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            func.date(GeneratedImage.creation_timestamp).label("day"),
            func.count(GeneratedImage.id).label("count"),
        )
        .filter(GeneratedImage.creation_timestamp >= cutoff)
        .group_by(func.date(GeneratedImage.creation_timestamp))
        .order_by("day")
        .all()
    )
    return [{"date": str(r.day), "count": r.count} for r in rows]


@app.get("/api/v1/admin/online-count")
def admin_online_count(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    from datetime import timedelta
    from backend.schemas import ONLINE_THRESHOLD_MINUTES
    cutoff = datetime.utcnow() - timedelta(minutes=ONLINE_THRESHOLD_MINUTES)
    count = db.query(User).filter(
        User.last_active_at >= cutoff,
        User.is_active == True
    ).count()
    total = db.query(User).filter(User.is_active == True).count()
    pending = db.query(User).filter(User.is_active == False).count()
    return {"online": count, "total": total, "pending": pending}


@app.get("/api/v1/admin/users", response_model=list[UserStatsResponse])
def admin_list_users(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    users = db.query(User).order_by(User.created_at).all()
    result = []
    for u in users:
        # Fetch course IDs as hex strings to avoid GUID JOIN comparison issues
        user_course_ids = [
            "%.32x" % row[0].int if isinstance(row[0], __import__('uuid').UUID) else row[0]
            for row in db.query(Course.id).filter(Course.user_id == u.id).all()
        ]
        course_count = len(user_course_ids)

        if user_course_ids:
            generated_count = db.query(GeneratedImage).filter(
                GeneratedImage.course_id.in_(user_course_ids),
                GeneratedImage.is_edited == False
            ).count()
            edited_count = db.query(GeneratedImage).filter(
                GeneratedImage.course_id.in_(user_course_ids),
                GeneratedImage.is_edited == True
            ).count()
        else:
            generated_count = 0
            edited_count = 0

        download_count = db.query(DownloadLog).filter(DownloadLog.user_id == u.id).count()

        # Calculate total video duration for this user - get courses first to avoid UUID comparison issues
        total_duration = 0
        user_courses = db.query(Course).filter(Course.user_id == u.id).all()
        if user_courses:
            course_ids = [c.id for c in user_courses]
            duration_rows = db.query(MediaSession).filter(
                MediaSession.course_id.in_(course_ids)
            ).all()
            total_duration = sum(ms.duration_seconds for ms in duration_rows if ms.duration_seconds)

        result.append(UserStatsResponse(
            id=u.id,
            username=u.username,
            role=u.role,
            is_active=u.is_active,
            created_at=u.created_at,
            last_active_at=u.last_active_at,
            course_count=course_count,
            generated_count=generated_count,
            edited_count=edited_count,
            download_count=download_count,
            total_active_seconds=u.total_active_seconds or 0,
            total_video_duration_seconds=total_duration,
        ))
    return result


@app.get("/api/v1/admin/users/{user_id}/courses", response_model=list[CourseDetailResponse])
def admin_get_user_courses(user_id: UUID, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return db.query(Course).filter(Course.user_id == user_id).order_by(Course.created_at.desc()).all()


@app.get("/api/v1/admin/users/{user_id}/durations")
def admin_get_user_durations(user_id: UUID, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Get detailed duration breakdown for a user: all courses with their media sessions and durations."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    courses = db.query(Course).filter(Course.user_id == user_id).order_by(Course.created_at.desc()).all()

    result = {
        "user_id": str(user.id),
        "username": user.username,
        "total_duration_seconds": 0,
        "courses": []
    }

    for course in courses:
        sessions = db.query(MediaSession).filter(
            MediaSession.course_id == course.id
        ).order_by(MediaSession.upload_timestamp.desc()).all()

        course_duration = 0
        sessions_data = []

        for session in sessions:
            duration = session.duration_seconds or 0
            course_duration += duration
            sessions_data.append({
                "session_id": str(session.id),
                "title": session.title,
                "duration_seconds": duration,
                "upload_timestamp": session.upload_timestamp.isoformat(),
                "processing_status": session.processing_status,
                "source_type": session.source_type,
            })

        result["courses"].append({
            "course_id": str(course.id),
            "course_title": course.title,
            "course_duration_seconds": course_duration,
            "media_sessions": sessions_data
        })

        result["total_duration_seconds"] += course_duration

    return result


@app.post("/api/v1/admin/users", response_model=UserResponse)
def admin_create_user(req: UserCreate, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        role=req.role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.put("/api/v1/admin/users/{user_id}", response_model=UserResponse)
def admin_update_user(user_id: UUID, req: UserUpdate, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if req.password is not None:
        user.password_hash = hash_password(req.password)
    if req.role is not None:
        user.role = req.role
    if req.is_active is not None:
        user.is_active = req.is_active
    db.commit()
    db.refresh(user)
    return user


@app.delete("/api/v1/admin/users/{user_id}")
def admin_delete_user(user_id: UUID, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete admin users")
    db.delete(user)
    db.commit()
    return {"status": "deleted"}


# ── Download log ──────────────────────────────────────────────────────────────

@app.get("/api/v1/images/{image_id}/download")
def download_image_file(image_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    image = db.query(GeneratedImage).filter(GeneratedImage.id == image_id).first()
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    # Log the download
    log = DownloadLog(user_id=current_user.id, image_id=image_id)
    db.add(log)
    db.commit()

    import io
    from PIL import Image as PilImage

    def _to_jpeg(image_bytes: bytes) -> bytes:
        buf = io.BytesIO(image_bytes)
        img = PilImage.open(buf).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=92)
        return out.getvalue()

    base_filename = os.path.splitext(os.path.basename(image.local_path))[0] if image.local_path else str(image_id)
    download_filename = f"{base_filename}.jpeg"
    print(f"[DOWNLOAD] Image local_path: {image.local_path}")
    print(f"[DOWNLOAD] base_filename: {base_filename}")
    print(f"[DOWNLOAD] download_filename: {download_filename}")
    headers = {"Content-Disposition": f'attachment; filename="{download_filename}"'}

    # Serve from local disk if present
    if image.local_path and os.path.exists(image.local_path):
        with open(image.local_path, "rb") as f:
            jpeg_bytes = _to_jpeg(f.read())
        return StreamingResponse(io.BytesIO(jpeg_bytes), media_type="image/jpeg", headers=headers)

    # Otherwise fetch from S3 and convert
    if s3_service.is_configured():
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION", "ap-south-1"),
        )
        s3_filename = os.path.basename(image.local_path) if image.local_path else f"{image_id}.png"
        try:
            obj = s3.get_object(Bucket=os.getenv("S3_BUCKET_NAME"), Key=s3_service.s3_key_for(s3_filename))
            jpeg_bytes = _to_jpeg(obj["Body"].read())
            return StreamingResponse(io.BytesIO(jpeg_bytes), media_type="image/jpeg", headers=headers)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not retrieve image from S3: {e}")

    raise HTTPException(status_code=404, detail="Image file not found")


@app.post("/api/v1/images/{image_id}/log-download")
def log_download(image_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    image = db.query(GeneratedImage).filter(GeneratedImage.id == image_id).first()
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    log = DownloadLog(user_id=current_user.id, image_id=image_id)
    db.add(log)
    db.commit()
    return {"status": "logged"}


# ── Courses ───────────────────────────────────────────────────────────────────

@app.post("/api/v1/courses", response_model=CourseResponse)
def create_course(course: CourseCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    new_course = Course(**course.model_dump(), user_id=current_user.id)
    db.add(new_course)
    db.commit()
    db.refresh(new_course)
    return new_course


@app.get("/api/v1/courses", response_model=list[CourseDetailResponse])
def get_courses(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from backend.schemas import CourseDetailResponse as CDR
    query = db.query(Course).options(selectinload(Course.owner))
    if current_user.role != "admin":
        query = query.filter(Course.user_id == current_user.id)
    courses = query.order_by(Course.created_at.desc()).all()
    return [CDR.from_orm_with_owner(c) for c in courses]


@app.get("/api/v1/courses/{course_id}", response_model=CourseDetailResponse)
def get_course(course_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    if current_user.role != "admin" and str(course.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    return course


@app.put("/api/v1/courses/{course_id}", response_model=CourseResponse)
def update_course(course_id: UUID, course_update: CourseUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    if current_user.role != "admin" and str(course.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    course.title = course_update.title
    course.scenario_description = course_update.scenario_description
    course.about_course = course_update.about_course
    course.tag = course_update.tag
    course.custom_restrictions = course_update.custom_restrictions
    db.commit()
    db.refresh(course)
    return course


@app.delete("/api/v1/courses/{course_id}/prompts")
def clear_course_prompts(course_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """Clear all stored final_prompt values for every visual plan in every session of a course.
    Called automatically when the user saves new course restrictions."""
    sessions = db.query(MediaSession).filter(MediaSession.course_id == course_id).all()
    cleared = 0
    for sess in sessions:
        segments = db.query(AudioSegment).filter(AudioSegment.session_id == sess.id).all()
        for seg in segments:
            for plan in seg.alternative_plans:
                if plan.final_prompt:
                    plan.final_prompt = None
                    cleared += 1
    db.commit()
    return {"cleared": True, "count": cleared}


import asyncio
import threading

def _run_background_processing(session_id_str: str, file_path: str, is_transcript: bool, course_id_str: str):
    """Background worker: runs ASR + chunking with retry. Uses its own DB session."""
    from backend.database import SessionLocal
    db = SessionLocal()
    MAX_RETRIES = 3

    try:
        session = db.query(MediaSession).filter(MediaSession.id == session_id_str).first()
        if not session:
            print(f"[BG] Session {session_id_str} not found")
            return

        course = db.query(Course).filter(Course.id == course_id_str).first()
        if not course:
            session.processing_status = "failed"
            session.error_message = "Course not found"
            db.commit()
            return

        session.processing_status = "processing"
        db.commit()

        transcript_text = ""
        if file_path.lower().endswith('.srt'):
            with open(file_path, "r", encoding="utf-8") as f:
                transcript_text = parse_srt_to_transcript(f.read())
        elif is_transcript:
            with open(file_path, "r", encoding="utf-8") as f:
                transcript_text = f.read()
        else:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                transcript_text = loop.run_until_complete(extract_transcript_from_audio(file_path))
            finally:
                loop.close()

        # Guard: if transcription returned an error string, mark as failed and keep the file
        if transcript_text.startswith("Failed to extract transcript"):
            session.processing_status = "failed"
            session.error_message = transcript_text
            db.commit()
            print(f"[BG] Transcription failed for session {session_id_str}: {transcript_text}")
            return

        session.transcript_text = transcript_text
        db.commit()

        # Only delete the local file after a confirmed successful transcript
        if os.path.exists(file_path):
            os.remove(file_path)

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"[BG] Chunking attempt {attempt}/{MAX_RETRIES} for session {session_id_str}")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(
                        parse_and_chunk_transcript(transcript_text, session_id_str, db, course)
                    )
                finally:
                    loop.close()

                vplan = VisualPlan(session_id=session.id)
                db.add(vplan)
                db.commit()

                # Calculate duration from audio segments (max timestamp_end)
                audio_segments = db.query(AudioSegment).filter(AudioSegment.session_id == session.id).all()
                if audio_segments:
                    max_timestamp = max(seg.timestamp_end for seg in audio_segments)
                    session.duration_seconds = max_timestamp
                else:
                    # Fallback: if no audio segments, check visual plan's segments
                    vplan_segments = db.query(AudioSegment).filter(
                        AudioSegment.session_id == session.id
                    ).order_by(AudioSegment.timestamp_end.desc()).first()

                    if vplan_segments:
                        session.duration_seconds = vplan_segments.timestamp_end
                    else:
                        session.duration_seconds = 0

                session.processing_status = "completed"
                session.error_message = None
                db.commit()
                print(f"[BG] Session {session_id_str} processing COMPLETED (duration: {session.duration_seconds}s)")
                return
            except Exception as e:
                last_error = str(e)
                print(f"[BG] Chunking attempt {attempt} failed: {last_error}")
                if attempt < MAX_RETRIES:
                    import time
                    time.sleep(2)

        session.processing_status = "failed"
        session.error_message = f"Chunking failed after {MAX_RETRIES} attempts: {last_error}"
        db.commit()
        print(f"[BG] Session {session_id_str} processing FAILED")

    except Exception as e:
        print(f"[BG] Fatal error processing session {session_id_str}: {e}")
        try:
            session = db.query(MediaSession).filter(MediaSession.id == session_id_str).first()
            if session:
                session.processing_status = "failed"
                session.error_message = str(e)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@app.post("/api/v1/sessions/upload")
async def upload_session(
    course_id: UUID = Form(...),
    file: UploadFile = File(None),
    transcript: UploadFile = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not file and not transcript:
        raise HTTPException(status_code=400, detail="Must provide either media file or transcript.")

    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    if current_user.role != "admin" and str(course.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    is_transcript = transcript is not None
    if is_transcript and transcript.filename.lower().endswith('.txt'):
        raise HTTPException(status_code=400, detail=".txt files are not accepted. Please upload an .srt or .xml file.")
    upload_file = transcript if is_transcript else file
    temp_path = f"/tmp/{upload_file.filename}"

    if is_transcript:
        content = await transcript.read()
        with open(temp_path, "wb") as f:
            f.write(content)
    else:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

    session = MediaSession(
        course_id=course_id,
        title=upload_file.filename,
        transcript_text="",
        processing_status="processing",
        source_type="transcript" if is_transcript else "video"
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # Upload original file to S3 so it can be re-processed later
    if s3_service.is_configured():
        s3_key = s3_service.upload_media(temp_path, str(session.id), upload_file.filename)
        if s3_key:
            session.s3_key = s3_key
            db.commit()

    thread = threading.Thread(
        target=_run_background_processing,
        args=(str(session.id), temp_path, is_transcript, str(course_id)),
        daemon=True
    )
    thread.start()

    return {"session_id": str(session.id), "status": "processing", "message": "Upload received. Processing in background."}


@app.get("/api/v1/sessions/{session_id}/status")
def get_session_status(session_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    session = db.query(MediaSession).filter(MediaSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": str(session.id),
        "processing_status": session.processing_status,
        "error_message": session.error_message
    }


@app.patch("/api/v1/sessions/{session_id}/tag")
def update_session_tag(session_id: UUID, body: dict, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    session = db.query(MediaSession).filter(MediaSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    course = db.query(Course).filter(Course.id == session.course_id).first()
    if current_user.role != "admin" and (not course or str(course.user_id) != str(current_user.id)):
        raise HTTPException(status_code=403, detail="Access denied")
    session.tag = body.get("tag") or None
    db.commit()
    return {"session_id": str(session.id), "tag": session.tag}


@app.get("/api/v1/sessions/{session_id}/download-source")
def download_source_video(session_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    session = db.query(MediaSession).filter(MediaSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.s3_key:
        raise HTTPException(status_code=404, detail="No source file stored for this session.")
    url = s3_service.get_presigned_media_url(session.s3_key, expiration=3600)
    if not url:
        raise HTTPException(status_code=500, detail="Could not generate download link.")
    return {"url": url, "filename": session.title}


@app.post("/api/v1/sessions/{session_id}/regenerate")
def regenerate_session(session_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    session = db.query(MediaSession).filter(MediaSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not session.s3_key:
        raise HTTPException(status_code=400, detail="Source file is not stored in cloud — cannot regenerate.")

    # Download from S3 to /tmp
    temp_path = f"/tmp/{session.title}"
    if not s3_service.download_media(session.s3_key, temp_path):
        raise HTTPException(status_code=500, detail="Failed to download source file from cloud storage.")

    # Clear all previous results so we start fresh
    # Must delete SegmentVisualPlan first — FK to AudioSegment has no DB-level CASCADE
    segment_ids = db.query(AudioSegment.id).filter(AudioSegment.session_id == session.id).subquery()
    db.query(SegmentVisualPlan).filter(SegmentVisualPlan.segment_id.in_(segment_ids)).delete(synchronize_session=False)
    db.query(AudioSegment).filter(AudioSegment.session_id == session.id).delete(synchronize_session=False)
    db.query(VisualPlan).filter(VisualPlan.session_id == session.id).delete(synchronize_session=False)
    session.transcript_text = ""
    session.processing_status = "processing"
    session.error_message = None
    db.commit()

    thread = threading.Thread(
        target=_run_background_processing,
        args=(str(session.id), temp_path, session.source_type == "transcript", str(session.course_id)),
        daemon=True
    )
    thread.start()

    return {"status": "processing", "message": "Regeneration started."}


@app.delete("/api/v1/sessions/{session_id}")
def delete_session(session_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    session = db.query(MediaSession).filter(MediaSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()
    return {"detail": "Session deleted"}


@app.get("/api/v1/plans/{session_id}", response_model=VisualPlanResponse)
def get_visual_plan(session_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    plan = db.query(VisualPlan).filter(VisualPlan.session_id == session_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Visual plan not found")

    session = (
        db.query(MediaSession)
        .options(
            selectinload(MediaSession.audio_segments)
            .selectinload(AudioSegment.alternative_plans),
            selectinload(MediaSession.course)
        )
        .filter(MediaSession.id == session_id)
        .first()
    )

    segments_data = [
        {
            "id": seg.id,
            "timestamp_start": seg.timestamp_start,
            "timestamp_end": seg.timestamp_end,
            "transcript_text": seg.transcript_text,
            "alternative_plans": seg.alternative_plans
        }
        for seg in sorted(session.audio_segments, key=lambda s: s.timestamp_start)
    ]

    return dict(
        id=plan.id,
        session_id=plan.session_id,
        status=plan.status,
        global_context_id=plan.global_context_id,
        created_at=plan.created_at,
        segments=segments_data,
        course_scenario=session.course.scenario_description,
        course_about=session.course.about_course,
        course_restrictions=session.course.custom_restrictions,
        session_tag=session.tag
    )


@app.post("/api/v1/sessions/{session_id}/curate")
def curate_session_plans(session_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    curated_ids = curate_visual_plans(str(session_id), db)
    return {"curated_ids": curated_ids, "count": len(curated_ids)}

@app.delete("/api/v1/sessions/{session_id}/curate")
def clear_session_curation(session_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    segments = db.query(AudioSegment).filter(AudioSegment.session_id == session_id).all()
    for seg in segments:
        for plan in seg.alternative_plans:
            plan.is_curated = False
    db.commit()
    return {"cleared": True}


@app.delete("/api/v1/sessions/{session_id}/prompts")
def clear_session_prompts(session_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """Clear all stored final_prompt values for every visual plan in a session.
    The frontend will then regenerate them fresh using the updated buildPromptForCard logic."""
    segments = db.query(AudioSegment).filter(AudioSegment.session_id == session_id).all()
    cleared = 0
    for seg in segments:
        for plan in seg.alternative_plans:
            if plan.final_prompt:
                plan.final_prompt = None
                cleared += 1
    db.commit()
    return {"cleared": True, "count": cleared}


@app.delete("/api/v1/admin/reset-all-prompts")
def admin_reset_all_prompts(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Admin-only: wipe ALL stored final_prompt values across every session and every user.
    After this, the frontend will auto-regenerate prompts fresh for every card that loads."""
    cleared = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.final_prompt.isnot(None)).update(
        {SegmentVisualPlan.final_prompt: None}, synchronize_session=False
    )
    db.commit()
    return {"cleared": True, "count": cleared}


@app.post("/api/v1/admin/backfill-durations")
def admin_backfill_durations(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Admin-only: calculate and backfill duration_seconds for all existing media sessions.
    Uses audio segments from the session, or falls back to segments in visual plans."""
    sessions = db.query(MediaSession).all()
    updated_count = 0

    for session in sessions:
        if session.duration_seconds is not None:
            continue  # Already has duration, skip

        # First try: get duration from direct audio segments
        audio_segments = db.query(AudioSegment).filter(AudioSegment.session_id == session.id).all()

        if audio_segments:
            # Duration is the max timestamp_end
            max_timestamp = max(seg.timestamp_end for seg in audio_segments)
            session.duration_seconds = max_timestamp
            updated_count += 1
            continue

        # Fallback: if no audio segments, try to get from visual plan's segments
        visual_plans = db.query(VisualPlan).filter(VisualPlan.session_id == session.id).all()
        max_timestamp = None

        for vplan in visual_plans:
            # Get audio segments associated with this visual plan (through segments)
            plan_segments = db.query(AudioSegment).filter(AudioSegment.session_id == session.id).all()
            if plan_segments:
                vplan_max = max(seg.timestamp_end for seg in plan_segments)
                if max_timestamp is None or vplan_max > max_timestamp:
                    max_timestamp = vplan_max

        if max_timestamp is not None:
            session.duration_seconds = max_timestamp
            updated_count += 1
        else:
            # No segments found anywhere
            session.duration_seconds = 0
            updated_count += 1

    db.commit()
    return {"success": True, "updated": updated_count, "message": f"Updated durations for {updated_count} media sessions"}


@app.post("/api/v1/visual-plans/{plan_id}/reject-curated")
def reject_curated_plan(plan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    plan = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    seg = db.query(AudioSegment).filter(AudioSegment.id == plan.segment_id).first()
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    minute = int(seg.timestamp_start // 60)

    # Find all segments in the same minute of the same session
    segs_in_minute = db.query(AudioSegment).filter(
        AudioSegment.session_id == seg.session_id,
        AudioSegment.timestamp_start >= minute * 60,
        AudioSegment.timestamp_start < (minute + 1) * 60,
    ).all()

    # Collect non-curated, non-disabled candidates from those segments
    candidates = []
    for s in segs_in_minute:
        for p in s.alternative_plans:
            if not p.is_curated and not p.is_disabled and str(p.id) != str(plan_id):
                candidates.append((p, s.timestamp_start))

    replacement_id = None
    if candidates:
        # Pick the one closest in time to the rejected plan
        replacement_plan, _ = min(candidates, key=lambda x: abs(x[1] - seg.timestamp_start))
        replacement_plan.is_curated = True
        replacement_id = str(replacement_plan.id)

    plan.is_curated = False
    db.commit()

    return {"replacement_id": replacement_id}


@app.put("/api/v1/visual-plans/{plan_id}")
def update_specific_plan(plan_id: UUID, req: VisualPlanMutationRequest, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    plan = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Alternative Visual Plan not found")
    plan.concept = req.concept
    plan.description = req.description
    plan.visual_type = req.visual_type
    plan.tags = req.tags
    plan.global_context = req.global_context
    plan.final_prompt = req.final_prompt
    plan.is_disabled = req.is_disabled
    db.commit()
    return {"status": "plan_updated"}


@app.post("/api/v1/visual-plans/{plan_id}/generate")
async def generate_specific_plan(plan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    plan = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Alternative Visual Plan not found")
    if plan.is_disabled:
        raise HTTPException(status_code=400, detail="Cannot generate for a disabled plan.")

    prompt = plan.final_prompt
    if not prompt:
        prompt = f"{plan.visual_type}: {plan.concept}. {plan.description}. Context: {plan.global_context}"

    import uuid
    new_image_id = uuid.uuid4()

    # Resolve course_id and tag via explicit queries to avoid lazy-load chain failures
    course_id = None
    course_tag = None
    if plan.segment_id:
        seg = db.query(AudioSegment).filter(AudioSegment.id == plan.segment_id).first()
        if seg and seg.session_id:
            msession = db.query(MediaSession).filter(MediaSession.id == seg.session_id).first()
            if msession:
                course_id = msession.course_id
                course_tag = msession.tag
                print(f"[IMG] Session {msession.id} has tag: '{course_tag}'")

    # Generate filename with tag if available, otherwise just timestamp
    timestamp = int(datetime.utcnow().timestamp())
    if course_tag and course_tag.strip():
        output_filename = f"{course_tag}_{timestamp}.png"
        print(f"[IMG] Generated filename with tag: {output_filename}")
    else:
        output_filename = f"{timestamp}.png"
        print(f"[IMG] Generated filename without tag: {output_filename}")

    output_path = f"/app/backend/local_storage/images/{output_filename}"

    actual_path = await generate_image(prompt, output_path)

    new_image = GeneratedImage(
        id=new_image_id,
        course_id=course_id,
        plan_id=plan_id,
        local_path=actual_path,
        visual_type=plan.visual_type,
        theme=plan.theme,
        concept_description=plan.description,
        tags=plan.tags,
        generation_prompt=prompt,
    )
    db.add(new_image)
    db.commit()
    db.refresh(new_image)

    plan.selected_image_id = new_image.id
    db.commit()

    public_url = f"/images/{output_filename}"
    return {"asset_id": str(new_image.id), "image_path": public_url, "status": "success"}


@app.put("/api/v1/plans/{plan_id}/select_image")
def select_plan_image(plan_id: UUID, req: ImageSelectRequest, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    plan = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Alternative Visual Plan not found")
    img = db.query(GeneratedImage).filter(GeneratedImage.id == req.image_id).first()
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")
    plan.selected_image_id = img.id
    db.commit()
    return {"status": "image_linked", "asset_id": str(img.id)}


@app.get("/api/v1/plans/{plan_id}/images", response_model=list[ImageSuggestionResponse])
def get_plan_image_history(plan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    images = db.query(GeneratedImage).filter(
        GeneratedImage.plan_id == plan_id
    ).order_by(GeneratedImage.creation_timestamp.desc()).all()
    return images


@app.get("/api/v1/images/suggest", response_model=list[ImageSuggestionResponse])
def suggest_images(visual_type: str, course_id: UUID, tags: str = "", limit: int = 5, except_image_id: UUID = None, exclude_plan_id: UUID = None, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    query = db.query(GeneratedImage).filter(
        GeneratedImage.course_id == course_id,
        GeneratedImage.visual_type == visual_type
    )
    if except_image_id:
        query = query.filter(GeneratedImage.id != except_image_id)
    if exclude_plan_id:
        query = query.filter(GeneratedImage.plan_id != exclude_plan_id)
    if tags and tags.strip():
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            primary_tag = tag_list[0]
            query = query.filter(GeneratedImage.tags.ilike(f"%{primary_tag}%"))
    results = query.order_by(GeneratedImage.id.desc()).limit(limit).all()
    if len(results) < limit:
        current_ids = [r.id for r in results]
        if except_image_id and except_image_id not in current_ids:
            current_ids.append(except_image_id)
        fallback_query = db.query(GeneratedImage).filter(
            GeneratedImage.course_id == course_id,
            GeneratedImage.visual_type == visual_type,
            ~GeneratedImage.id.in_(current_ids if current_ids else [None])
        )
        if exclude_plan_id:
            fallback_query = fallback_query.filter(GeneratedImage.plan_id != exclude_plan_id)
        fallback_query = fallback_query.order_by(GeneratedImage.id.desc()).limit(limit - len(results))
        results.extend(fallback_query.all())
    return results


@app.post("/api/v1/images/{image_id}/edit")
async def edit_existing_image(image_id: UUID, req: ImageEditRequest, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    source_img = db.query(GeneratedImage).filter(GeneratedImage.id == image_id).first()
    if not source_img:
        raise HTTPException(status_code=404, detail="Source image not found")

    import uuid
    new_image_id = uuid.uuid4()

    # Get tag from the source image's session (via plan → segment → session)
    course_tag = None
    if source_img.plan_id:
        orig_plan = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.id == source_img.plan_id).first()
        if orig_plan and orig_plan.segment_id:
            seg = db.query(AudioSegment).filter(AudioSegment.id == orig_plan.segment_id).first()
            if seg:
                msession = db.query(MediaSession).filter(MediaSession.id == seg.session_id).first()
                if msession:
                    course_tag = msession.tag
                    print(f"[IMG] Edit: Session {msession.id} has tag: '{course_tag}'")

    # Generate filename with tag if available, otherwise just timestamp
    timestamp = int(datetime.utcnow().timestamp())
    if course_tag and course_tag.strip():
        output_filename = f"{course_tag}_{timestamp}.png"
        print(f"[IMG] Edit: Generated filename with tag: {output_filename}")
    else:
        output_filename = f"{timestamp}.png"
        print(f"[IMG] Edit: Generated filename without tag: {output_filename}")

    output_path = f"/app/backend/local_storage/images/{output_filename}"

    # Ensure source image is available locally (may need to pull from S3)
    source_local = source_img.local_path
    source_was_downloaded = False
    if source_local and not os.path.exists(source_local) and s3_service.is_configured():
        source_filename = os.path.basename(source_local)
        downloaded = s3_service.download_image(source_filename, source_local)
        if not downloaded:
            raise HTTPException(status_code=500, detail="Could not retrieve source image from S3")
        source_was_downloaded = True

    actual_path = await edit_image(source_img.local_path, req.modification, output_path)

    # Clean up the source image if we downloaded it from S3 just for this edit
    if source_was_downloaded and source_local and os.path.exists(source_local):
        try:
            os.remove(source_local)
        except Exception as e:
            print(f"[S3] Warning: could not remove temp source {source_local}: {e}")

    new_image = GeneratedImage(
        id=new_image_id,
        course_id=source_img.course_id,
        plan_id=req.plan_id or source_img.plan_id,
        local_path=actual_path,
        visual_type=source_img.visual_type,
        theme=source_img.theme,
        concept_description=f"{source_img.concept_description} [Edited: {req.modification}]",
        tags=source_img.tags,
        generation_prompt=f"Edit of {image_id}: {req.modification}",
        is_edited=True,
    )
    db.add(new_image)
    db.commit()
    db.refresh(new_image)

    if req.plan_id:
        plan = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.id == req.plan_id).first()
        if plan:
            plan.selected_image_id = new_image.id
            db.commit()

    public_url = f"/images/{output_filename}"
    return {"asset_id": str(new_image.id), "image_path": public_url, "status": "success"}


@app.delete("/api/v1/images/{image_id}")
def delete_image(image_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    image = db.query(GeneratedImage).filter(GeneratedImage.id == image_id).first()
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    plans = db.query(SegmentVisualPlan).filter(SegmentVisualPlan.selected_image_id == image_id).all()
    for plan in plans:
        plan.selected_image_id = None
    if image.local_path:
        # Delete from S3
        if s3_service.is_configured():
            s3_service.delete_image(os.path.basename(image.local_path))
        # Delete local copy if it still exists
        if os.path.exists(image.local_path):
            try:
                os.remove(image.local_path)
            except Exception as e:
                print(f"Warning: Could not delete local file {image.local_path}: {e}")
    db.delete(image)
    db.commit()
    return {"status": "deleted", "id": str(image_id)}


@app.get("/api/v1/sessions/{session_id}/timeline-assets")
def get_final_timeline(session_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    session = db.query(MediaSession).filter(MediaSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    timeline_assets = []
    for seg in session.audio_segments:
        alts = db.query(SegmentVisualPlan).filter(
            SegmentVisualPlan.segment_id == seg.id,
            SegmentVisualPlan.is_disabled == False,
            SegmentVisualPlan.selected_image_id != None
        ).all()
        for alt in alts:
            img = db.query(GeneratedImage).filter(GeneratedImage.id == alt.selected_image_id).first()
            if img:
                filename = os.path.basename(img.local_path)
                timeline_assets.append({
                    "timestamp_start": seg.timestamp_start,
                    "timestamp_end": seg.timestamp_end,
                    "transcript_text": seg.transcript_text or "",
                    "concept": alt.concept or "",
                    "description": alt.description or "",
                    "visual_type": img.visual_type or "",
                    "theme": img.theme or "",
                    "tags": img.tags or "",
                    "asset_path": f"/images/{filename}",
                    "type": img.visual_type
                })

    return {"status": "completed", "timeline_assets": timeline_assets}


app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
