import os
import re
import json
import uuid
import time
import bisect
import shutil
import subprocess
import tempfile
from collections import defaultdict
from google import genai
from pydantic import BaseModel, Field
from typing import List, cast
from backend.models import AudioSegment, SegmentVisualPlan

# Initialize the new-style Gemini client (uses key pool — see gemini_keys.py)
from backend.services.gemini_keys import get_client, call_with_retry, key_count
client = get_client()
print(f"[GeminiService] Loaded {key_count()} API key(s).")

MODEL_NAME = "gemini-3.1-flash-lite"
ASR_MODEL_NAME = "gemini-3-flash-preview"  # Better model for timestamped transcription
# ASR_MODEL_NAME = "gemini-2.5-flash"  # Better model for timestamped transcription

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv', '.m4v'}

# ASR chunking config
_CHUNK_LENGTH_MS = 120_000      # 2 minutes per chunk
_SILENCE_NOISE_DB = -30         # -30dB threshold for silence detection
_SILENCE_MIN_SEC = 0.5          # minimum silence duration to use as split point
_HALLUCINATION_BUFFER_MS = 2000  # 2s tolerance before flagging hallucination
_LEADING_DROPOUT_THRESHOLD_MS = 15_000  # if first segment starts >15s into chunk, it's a dropout
_GAP_THRESHOLD_MS = 500         # gaps > 500ms between segments trigger recovery pass
_FADE_ZONE_MS = 80              # adaptive fade zone — soft overlap tolerance in canvas assembly
_TEMPERATURE_CYCLE = [0.0, 0.2, 0.4]  # 3 attempts


def _get_audio_duration_ms(path: str) -> int:
    """Return audio/video duration in milliseconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Exception(f"ffprobe failed: {result.stderr[:200]}")
    return int(float(result.stdout.strip()) * 1000)


def _extract_audio_wav(source_path: str) -> str:
    """Extract/convert any audio or video file to 16kHz mono WAV.
    WAV is optimal for Gemini ASR accuracy."""
    out_path = tempfile.mktemp(suffix=".wav")
    cmd = [
        "ffmpeg", "-i", source_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"ffmpeg WAV extraction failed: {result.stderr[:200]}")
    return out_path


def _detect_silences(audio_path: str) -> list:
    """Detect silence intervals using ffmpeg silencedetect.
    Returns list of (start_sec, end_sec) tuples."""
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", f"silencedetect=noise={_SILENCE_NOISE_DB}dB:d={_SILENCE_MIN_SEC}",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    silences = []
    current_start = None
    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip().split()[0])
            except ValueError:
                pass
        elif "silence_end:" in line and current_start is not None:
            try:
                end = float(line.split("silence_end:")[1].split("|")[0].strip())
                silences.append((current_start, end))
                current_start = None
            except ValueError:
                pass
    return silences


def _split_audio_into_chunks(wav_path: str) -> list:
    """Split WAV into silence-aware chunks of ~5 minutes each.
    Returns list of dicts: {path, duration_ms, offset_ms, _temp_dir (if split)}.
    Returns single-item list if audio is shorter than one chunk."""
    total_ms = _get_audio_duration_ms(wav_path)

    if total_ms <= _CHUNK_LENGTH_MS:
        return [{"path": wav_path, "duration_ms": total_ms, "offset_ms": 0}]

    silences = _detect_silences(wav_path)
    chunk_length_sec = _CHUNK_LENGTH_MS / 1000.0
    total_sec = total_ms / 1000.0
    silence_starts = [s[0] for s in silences]

    # Build split points, snapping to nearest silence within ±30s of each target
    split_points = []
    current_time = 0.0
    while current_time + chunk_length_sec < total_sec:
        target = current_time + chunk_length_sec
        window = min(30.0, chunk_length_sec / 4)
        idx = bisect.bisect_left(silence_starts, target - window)
        candidates = [s for s in silences[idx:] if s[0] <= target + window and s[1] > target - window]
        if candidates:
            best = min(candidates, key=lambda s: abs((s[0] + s[1]) / 2 - target))
            split_time = (best[0] + best[1]) / 2
        else:
            split_time = target  # hard cut if no silence found
        split_points.append(f"{split_time:.3f}")
        current_time = split_time

    temp_dir = tempfile.mkdtemp()
    segment_pattern = os.path.join(temp_dir, "chunk_%03d.wav")
    cmd = [
        "ffmpeg", "-y", "-i", wav_path,
        "-f", "segment",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-reset_timestamps", "1",
    ]
    if split_points:
        cmd.extend(["-segment_times", ",".join(split_points)])
    else:
        cmd.extend(["-segment_time", str(chunk_length_sec)])
    cmd.append(segment_pattern)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"ffmpeg chunk split failed: {result.stderr[:200]}")

    chunk_files = sorted(
        os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith(".wav")
    )
    chunks = []
    offset_ms = 0
    for path in chunk_files:
        dur_ms = _get_audio_duration_ms(path)
        chunks.append({"path": path, "duration_ms": dur_ms, "offset_ms": offset_ms, "_temp_dir": temp_dir})
        offset_ms += dur_ms
    return chunks


def _srt_time_to_ms(time_str: str) -> int:
    """Parse timestamp to milliseconds.
    Handles: HH:MM:SS,mmm  HH:MM:SS.mmm  MM:SS,mmm  MM:SS.mmm  MM:SS:mmm (Gemini quirk)"""
    s = str(time_str).strip()
    # HH:MM:SS,mmm or HH:MM:SS.mmm  (standard SRT)
    m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)$', s)
    if m:
        h, mn, sc, ms_str = m.groups()
        ms = int(ms_str[:3].ljust(3, '0'))
        return (int(h) * 3600 + int(mn) * 60 + int(sc)) * 1000 + ms
    # MM:SS:mmm  (Gemini quirk — colon before 3-digit ms, no hours component)
    m = re.match(r'(\d+):(\d+):(\d{3})$', s)
    if m:
        mn, sc, ms = map(int, m.groups())
        return (mn * 60 + sc) * 1000 + ms
    # MM:SS,mmm or MM:SS.mmm  (no hours)
    m = re.match(r'(\d+):(\d+)[,.](\d+)$', s)
    if m:
        mn, sc, ms_str = m.groups()
        ms = int(ms_str[:3].ljust(3, '0'))
        return (int(mn) * 60 + int(sc)) * 1000 + ms
    # fallback: plain seconds
    try:
        return int(float(s.replace(',', '.')) * 1000)
    except ValueError:
        raise ValueError(f"Cannot parse timestamp: {time_str!r}")


def _parse_chunk_response(
    response_text: str,
    offset_ms: int,
    chunk_duration_ms: int,
    hallucination_buffer_ms: int | None = None,
) -> list:
    """Parse Gemini JSON, apply global offset, validate timestamps.
    Returns list of {start_s, end_s, text} dicts.
    Raises ValueError on bad/hallucinated output so caller can retry."""
    buffer = hallucination_buffer_ms if hallucination_buffer_ms is not None else _HALLUCINATION_BUFFER_MS

    # Strip markdown code fences that Gemini sometimes wraps around JSON
    text = response_text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    segments_raw = json.loads(text)  # raises JSONDecodeError if bad
    if not isinstance(segments_raw, list) or not segments_raw:
        raise ValueError("Empty segments list")

    segments = []
    for seg in segments_raw:
        start_ms = _srt_time_to_ms(seg.get("start", "00:00:00,000"))
        end_ms = _srt_time_to_ms(seg.get("end", "00:00:00,000"))
        if end_ms <= start_ms:
            continue
        segments.append({
            "start_s": (start_ms + offset_ms) / 1000.0,
            "end_s":   (end_ms   + offset_ms) / 1000.0,
            "text":    seg.get("text", "").strip()
        })

    if not segments:
        raise ValueError("No valid segments after parsing")

    # Hallucination guard: final segment must not exceed chunk end + tolerance
    last_end_ms = int(segments[-1]["end_s"] * 1000)
    limit_ms = offset_ms + chunk_duration_ms + buffer
    if last_end_ms > limit_ms:
        raise ValueError(
            f"Hallucination: last segment ends at {last_end_ms}ms, "
            f"limit is {offset_ms + chunk_duration_ms}ms (+{buffer}ms buffer)"
        )

    # Leading-dropout guard: first segment must not start too far into the chunk
    first_start_ms = int(segments[0]["start_s"] * 1000)
    chunk_relative_start_ms = first_start_ms - offset_ms
    if chunk_relative_start_ms > _LEADING_DROPOUT_THRESHOLD_MS:
        raise ValueError(
            f"Leading dropout: first segment starts {chunk_relative_start_ms}ms into chunk "
            f"(threshold {_LEADING_DROPOUT_THRESHOLD_MS}ms) — model skipped beginning"
        )

    # Mid-chunk gap guard: flag any internal gap > 20s as a dropout and retry
    _MID_GAP_THRESHOLD_MS = 20_000
    for idx in range(1, len(segments)):
        prev_end_ms = int(segments[idx - 1]["end_s"] * 1000)
        cur_start_ms = int(segments[idx]["start_s"] * 1000)
        gap_ms = cur_start_ms - prev_end_ms
        if gap_ms > _MID_GAP_THRESHOLD_MS:
            raise ValueError(
                f"Mid-chunk dropout: {gap_ms}ms gap between segment {idx - 1} "
                f"(ends {prev_end_ms}ms) and segment {idx} (starts {cur_start_ms}ms)"
            )

    return segments


# ── Sub-chunk transcription helpers ──────────────────────────────────────────

def _transcribe_chunk_attempt(audio_path: str, offset_ms: int, duration_ms: int, temperature: float) -> list | None:
    """Upload one audio slice to Gemini, transcribe it, delete it. Returns segments or None."""
    upload_client = get_client()
    myfile = None
    try:
        myfile = upload_client.files.upload(file=audio_path)
        wait_secs = 0
        while myfile.state.name == "PROCESSING" and wait_secs < 180:
            time.sleep(3)
            wait_secs += 3
            myfile = upload_client.files.get(name=myfile.name)
        if myfile.state.name != "ACTIVE":
            return None
        duration_sec = duration_ms / 1000.0
        prompt = f"""Transcribe this {duration_sec:.2f}s audio verbatim.

CRITICAL REQUIREMENTS:
1. Timestamps are relative to 00:00:00 and must not exceed {duration_sec:.2f}s
2. NO segment may exceed 10 seconds — split at sentence boundaries and natural pauses
3. DO NOT identify or label speakers under any circumstances — output text and timestamps ONLY
4. One sentence per segment, max 10s each (split longer speech at pauses)
5. Remove fillers (uh, um, hmm). Keep technical terms as spoken.
6. Every spoken word must be captured — do not skip or summarise any content
7. The text area should contain only transcript text — no timestamps inside text fields.

Timestamp format: HH:MM:SS,mmm (strict — no other format accepted)
JSON output:
[{{"start": "HH:MM:SS,mmm", "end": "HH:MM:SS,mmm", "text": "..."}}]"""
        response = upload_client.models.generate_content(
            model=ASR_MODEL_NAME,
            contents=[prompt, myfile],
            config={"response_mime_type": "application/json", "temperature": temperature, "max_output_tokens": 65536},
        )
        return _parse_chunk_response(response.text, offset_ms, duration_ms)
    finally:
        if myfile:
            try:
                upload_client.files.delete(name=myfile.name)
            except Exception:
                pass


def _transcribe_with_dropout_fallback(audio_path: str, offset_ms: int, duration_ms: int, label: str) -> list:
    """Try temperature cycling; if all fail due to dropout, split in half and retry each half."""
    segments = None
    last_err = None

    for temperature in _TEMPERATURE_CYCLE:
        try:
            segments = _transcribe_chunk_attempt(audio_path, offset_ms, duration_ms, temperature)
            if segments:
                print(f"  {label} transcribed: {len(segments)} segments (temp={temperature})")
                return segments
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_rl = "429" in err_str or "quota" in err_str or "resource_exhausted" in err_str
            print(f"  {label} attempt temp={temperature} failed: {e}")
            time.sleep(60 if is_rl else 3)

    # All retries failed — split in half if chunk is long enough
    half_ms = duration_ms // 2
    if half_ms < 10_000:
        print(f"  {label} chunk too short to split further — skipping.")
        return []

    print(f"  {label} all retries failed — splitting {duration_ms / 1000:.1f}s chunk in half...")
    mid_sec = half_ms / 1000.0
    total_sec = duration_ms / 1000.0
    import tempfile as _tf

    first_path = _tf.mktemp(suffix=".wav")
    second_path = _tf.mktemp(suffix=".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ss", "0", "-t", f"{mid_sec:.3f}",
             "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", first_path],
            capture_output=True, check=True
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ss", f"{mid_sec:.3f}", "-t", f"{mid_sec:.3f}",
             "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", second_path],
            capture_output=True, check=True
        )
        first_segs  = _transcribe_with_dropout_fallback(first_path,  offset_ms,           half_ms, f"{label}[A]")
        second_segs = _transcribe_with_dropout_fallback(second_path, offset_ms + half_ms, duration_ms - half_ms, f"{label}[B]")
        return first_segs + second_segs
    except Exception as e:
        print(f"  {label} sub-chunk split failed: {e}")
        return []
    finally:
        for p in (first_path, second_path):
            try:
                os.remove(p)
            except Exception:
                pass


# ── Gap Recovery ──────────────────────────────────────────────────────────────

def _extract_gap_audio(wav_path: str, start_s: float, end_s: float) -> str | None:
    """Slice a time window from a WAV for gap recovery. Returns temp path or None."""
    out_path = tempfile.mktemp(suffix=".wav")
    duration_s = end_s - start_s
    cmd = [
        "ffmpeg", "-y", "-i", wav_path,
        "-ss", f"{start_s:.3f}", "-t", f"{duration_s:.3f}",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(out_path):
        return None
    return out_path


def _transcribe_gap(wav_path: str, gap_start_s: float, gap_end_s: float) -> list:
    """Upload and re-transcribe a specific audio gap.
    Returns list of {start_s, end_s, text} dicts clipped to the gap bounds."""
    gap_duration_s = gap_end_s - gap_start_s
    gap_path = _extract_gap_audio(wav_path, gap_start_s, gap_end_s)
    if not gap_path:
        return []

    upload_client = None
    myfile = None
    try:
        upload_client = get_client()
        myfile = upload_client.files.upload(file=gap_path)

        waited = 0
        while myfile.state.name == "PROCESSING" and waited < 60:
            time.sleep(2)
            waited += 2
            myfile = upload_client.files.get(name=myfile.name)

        if myfile.state.name != "ACTIVE":
            return []

        prompt = f"""This {gap_duration_s:.2f}s audio segment was missed in the initial transcription pass.
Transcribe all audible speech with precise start and end times.

STRICT RULES:
- Timestamps are relative to 00:00:00 and must not exceed {gap_duration_s:.2f}s
- DO NOT identify or label speakers under any circumstances
- Output transcribed text and timestamps ONLY — no speaker names, no diarization
- One sentence per segment, max 8 seconds each
- Remove fillers (uh, um, hmm)
- Do not skip the transcripts because their quality is not goos. Generate Transcripts.

Format: HH:MM:SS,mmm (strict)
JSON output:
[{{"start": "HH:MM:SS,mmm", "end": "HH:MM:SS,mmm", "text": "..."}}]"""

        response = upload_client.models.generate_content(
            model=ASR_MODEL_NAME,
            contents=[prompt, myfile],
            config={
                "response_mime_type": "application/json",
                "temperature": 0.0,
                "max_output_tokens": 8192,
            },
        )

        gap_offset_ms = int(gap_start_s * 1000)
        gap_duration_ms = int(gap_duration_s * 1000)
        # Use a 1000ms buffer for short clips — Gemini can be slightly imprecise on brief slices
        raw = _parse_chunk_response(response.text, gap_offset_ms, gap_duration_ms, hallucination_buffer_ms=1000)

        # Clip recovered segments strictly within the gap bounds to prevent overlaps
        clipped = []
        for seg in raw:
            s = max(seg["start_s"], gap_start_s)
            e = min(seg["end_s"], gap_end_s)
            if e > s:
                clipped.append({"start_s": s, "end_s": e, "text": seg["text"]})
        return clipped

    except Exception as e:
        print(f"  [GapRecovery] {gap_start_s:.1f}s–{gap_end_s:.1f}s failed: {e}")
        return []
    finally:
        if gap_path and os.path.exists(gap_path):
            os.remove(gap_path)
        if myfile is not None and upload_client is not None:
            try:
                upload_client.files.delete(name=myfile.name)
            except Exception:
                pass


def _run_gap_recovery(wav_path: str, segments: list, total_duration_s: float) -> list:
    """Scan assembled timeline for gaps > 500ms and re-transcribe those slices.
    Covers: leading gap (before first segment), inter-segment gaps, trailing gap."""
    if not segments:
        return segments

    threshold_s = _GAP_THRESHOLD_MS / 1000.0
    sorted_segs = sorted(segments, key=lambda s: s["start_s"])

    gaps = []
    if sorted_segs[0]["start_s"] > threshold_s:
        gaps.append((0.0, sorted_segs[0]["start_s"]))
    for i in range(len(sorted_segs) - 1):
        gap_start = sorted_segs[i]["end_s"]
        gap_end = sorted_segs[i + 1]["start_s"]
        if gap_end - gap_start > threshold_s:
            gaps.append((gap_start, gap_end))
    if total_duration_s - sorted_segs[-1]["end_s"] > threshold_s:
        gaps.append((sorted_segs[-1]["end_s"], total_duration_s))

    if not gaps:
        return segments

    print(f"  [GapRecovery] {len(gaps)} gap(s) > {_GAP_THRESHOLD_MS}ms found — running recovery...")
    recovered = list(segments)
    for gap_start, gap_end in gaps:
        print(f"  [GapRecovery] Recovering {gap_start:.1f}s–{gap_end:.1f}s ({gap_end - gap_start:.1f}s)")
        new_segs = _transcribe_gap(wav_path, gap_start, gap_end)
        if new_segs:
            print(f"  [GapRecovery] Inserted {len(new_segs)} segment(s)")
            recovered.extend(new_segs)
        else:
            print(f"  [GapRecovery] No speech in {gap_start:.1f}s–{gap_end:.1f}s (silence or failed)")
        time.sleep(5)

    return recovered


# ── Canvas Assembly ───────────────────────────────────────────────────────────

def _apply_canvas_assembly(segments: list, total_duration_s: float) -> list:
    """Place each segment at its exact intended timestamp (canvas approach).
    Overflow beyond the adaptive fade zone is hard-clipped; the next segment
    is flagged stale to signal that its timing context was recalculated."""
    if not segments:
        return segments

    fade_zone_s = _FADE_ZONE_MS / 1000.0
    ordered = sorted(segments, key=lambda s: s["start_s"])
    result = []

    for i, seg in enumerate(ordered):
        seg = dict(seg)
        next_start = ordered[i + 1]["start_s"] if i + 1 < len(ordered) else total_duration_s
        overflow = seg["end_s"] - next_start

        if overflow > fade_zone_s:
            seg["end_s"] = next_start
            if i + 1 < len(ordered):
                ordered[i + 1] = dict(ordered[i + 1])
                ordered[i + 1]["_stale"] = True
                print(
                    f"  [Canvas] Segment @{seg['start_s']:.2f}s overflowed by {overflow*1000:.0f}ms — "
                    f"clipped; segment @{next_start:.2f}s marked stale"
                )

        seg["end_s"] = min(seg["end_s"], total_duration_s)
        if seg["start_s"] < total_duration_s and seg["end_s"] > seg["start_s"]:
            result.append(seg)

    return result


def parse_srt_to_transcript(srt_content: str) -> str:
    """Parse SRT subtitle file content into the [start_s - end_s] text format
    used by parse_and_chunk_transcript."""
    import re
    lines = []

    # SRT blocks are separated by blank lines
    blocks: list[str] = cast(list[str], re.split(r'\n\s*\n', srt_content.strip()))

    for block in blocks:
        block_lines: list[str] = block.strip().split('\n')

        # Find the --> timestamp line
        timestamp_line: str = ""
        text_start = 0
        for i, line in enumerate(block_lines):
            if '-->' in line:
                timestamp_line = line
                text_start = i + 1
                break

        if not timestamp_line:
            continue

        text_lines: list[str] = block_lines[text_start:]
        if not text_lines:
            continue

        # Parse HH:MM:SS,mmm --> HH:MM:SS,mmm (also accepts . as separator)
        match = re.match(
            r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})',
            timestamp_line.strip()
        )
        if not match:
            continue

        h1, m1, s1, ms1 = int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
        h2, m2, s2, ms2 = int(match.group(5)), int(match.group(6)), int(match.group(7)), int(match.group(8))

        start_s: float = h1 * 3600.0 + m1 * 60.0 + s1 + ms1 / 1000.0
        end_s: float   = h2 * 3600.0 + m2 * 60.0 + s2 + ms2 / 1000.0

        # Join multi-line subtitles; strip HTML tags like <i>, <b>
        text = ' '.join(text_lines).strip()
        text = re.sub(r'<[^>]+>', '', text).strip()

        if text:
            lines.append(f"[{start_s} - {end_s}] {text}")

    return '\n'.join(lines)


async def extract_transcript_from_audio(audio_path: str) -> str:
    """Transcribe audio/video with silence-aware chunking for accurate timestamps.

    Pipeline:
      1. Extract to 16kHz mono WAV
      2. Split into ~5-min chunks at natural silence points
      3. Upload ALL chunks to Gemini first (sequential uploads)
      4. Wait for ALL chunks to reach ACTIVE state
      5. Transcribe each chunk with 10-attempt temperature cycling
      6. Sort and assemble final transcript
    Returns transcript in "[start_s - end_s] text" format."""
    temp_wav = None
    chunk_temp_dirs: set = set()

    try:
        print(f"Extracting WAV from: {audio_path}")
        temp_wav = _extract_audio_wav(audio_path)

        chunks = _split_audio_into_chunks(temp_wav)
        for c in chunks:
            if "_temp_dir" in c:
                chunk_temp_dirs.add(c["_temp_dir"])

        total = len(chunks)
        print(f"Split into {total} chunk(s). Processing one at a time...")

        # ── Sequential: upload → wait ACTIVE → transcribe → delete, one chunk at a time ──
        # This avoids hammering the API with back-to-back calls and keeps the
        # number of live Gemini file uploads to exactly one at any moment.
        all_segments = []

        for i, chunk in enumerate(chunks):
            chunk_duration_ms = chunk["duration_ms"]
            offset_ms         = chunk["offset_ms"]
            print(f"  [{i + 1}/{total}] chunk offset={offset_ms / 1000:.1f}s duration={chunk_duration_ms / 1000:.1f}s")

            chunk_segments = _transcribe_with_dropout_fallback(
                chunk["path"], offset_ms, chunk_duration_ms, f"[{i + 1}/{total}]"
            )
            if chunk_segments:
                all_segments.extend(chunk_segments)
            else:
                print(f"  [{i + 1}/{total}] Warning: no segments after all fallbacks.")

            if i < total - 1:
                print(f"  [{i + 1}/{total}] Waiting 3s before next chunk...")
                time.sleep(3)

        if not all_segments:
            return ("Failed to extract transcript. All audio chunks failed to process "
                    "(likely a rate limit or upload error — check server logs).")

        print(f"Initial transcription: {len(all_segments)} raw segment(s).")

        # ── Canvas Assembly (temporal sync + overlap resolution) ─────────────
        total_duration_s = _get_audio_duration_ms(temp_wav) / 1000.0
        all_segments = _apply_canvas_assembly(all_segments, total_duration_s)
        print(f"After canvas assembly: {len(all_segments)} segment(s).")

        all_segments.sort(key=lambda x: x["start_s"])
        return "\n".join(
            f"[{seg['start_s']} - {seg['end_s']}] {seg['text']}"
            for seg in all_segments
        )

    except Exception as e:
        print(f"Error extracting transcript: {e}")
        return f"Failed to extract transcript: {e}"
    finally:
        if temp_wav and os.path.exists(temp_wav):
            os.remove(temp_wav)
        for temp_dir in chunk_temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)


class PromptAlternative(BaseModel):
    concept: str
    category: str
    description: str
    visual_type: str
    theme: str
    tags: str

class TranscriptChunk(BaseModel):
    start_line_index: int
    end_line_index: int
    transcript_text: str
    alternatives: List[PromptAlternative]

class VisualPlanSchema(BaseModel):
    chunks: List[TranscriptChunk]

async def parse_and_chunk_transcript(transcript: str, session_id: str, db, course) -> None:
    """Takes a raw transcript, chunks it into conceptual blocks, and generates
    multiple alternative visual plan ideas for each chunk using Gemini."""
    # Split transcript into batches of lines to preserve timestamp formatting
    # Keep batches small (10 lines): 1 chunk per line × 3 alternatives = big JSON per batch
    LINES_PER_BATCH = 10
    lines = transcript.strip().split("\n")
    batches = []
    
    for i in range(0, len(lines), LINES_PER_BATCH):
        batch = "\n".join(lines[i:i+LINES_PER_BATCH])
        batches.append(batch)

    for batch_idx, batch_text in enumerate(batches):
        print(f"Processing transcript batch {batch_idx + 1} of {len(batches)}")

        # Parse all real timestamps out of this batch so we can reference them later
        parsed_lines = []
        batch_lines = batch_text.split('\n')
        batch_offset = batch_idx * LINES_PER_BATCH  # Global line offset for this batch
        print(f"  Batch {batch_idx} has {len(batch_lines)} lines, global offset={batch_offset}")

        for idx, line in enumerate(batch_lines):
            match = re.search(r'\[([\d\.]+) - ([\d\.]+)\]', line)
            if match:
                parsed_lines.append({
                    "start": float(match.group(1)),
                    "end": float(match.group(2))
                })
            else:
                # Fallback if unparseable
                parsed_lines.append({"start": 0.0, "end": 0.0})
                
        # Give Gemini explicit numbered lines
        numbered_batch_text = "\n".join([f"Line {i}: {line}" for i, line in enumerate(batch_lines)])
        print(f"  Batch {batch_idx} first 3 lines:")
        for i in range(min(3, len(numbered_batch_text.split('\n')))):
            print(f"    {numbered_batch_text.split(chr(10))[i][:100]}")
        
        prompt = f"""
You are an expert educational video planner tasked with creating visual plans for an educational video.
The transcript is provided as a numbered list where EACH LINE is already a separate sentence with its own timestamp.

CRITICAL RULE: You MUST create exactly ONE chunk for EACH line. Do NOT merge or group lines together.
- Each chunk's `start_line_index` and `end_line_index` MUST be the SAME number.
- If a batch has N lines (Line 0 to Line N-1), you MUST output exactly N chunks.

Example for 3 lines:
- Chunk 1: start_line_index=0, end_line_index=0
- Chunk 2: start_line_index=1, end_line_index=1
- Chunk 3: start_line_index=2, end_line_index=2

This course's general scenario is: {course.scenario_description}
About this course: {course.about_course}

For each chunk, output exactly 3 alternative visual concepts that best represent that specific sentence.

--- DESCRIPTION FIELD RULES (most important) ---
The `description` field must be 2–3 detailed sentences that:
1. Are firmly grounded in the course scenario and context above — use the same setting, characters, and cultural context described there.
2. Show narrative continuity — look at the surrounding lines in the transcript and make this visual feel like a natural progression of the story so far (e.g. if earlier lines introduced a character or situation, continue that thread).
3. Describe a concrete, specific scene: who is present, where they are, what they are doing, what objects or elements are visible — not just the abstract concept.
4. Connect the sentence's key idea to the real-world scenario from the course (e.g. if the concept is "proactive approach" and the scenario is debt recovery in rural India, describe a specific agent or character taking a proactive step in that exact setting).
BAD example: "An illustration of an eagle representing the proactive approach and early detection."
GOOD example: "A loan recovery officer in a small-town office reviews a list of early-warning accounts on his desk, circling names before they become overdue. His expression is focused and deliberate, with a wall calendar and phone visible nearby. The scene conveys anticipation and preparedness rather than reaction."
--- END DESCRIPTION RULES ---

Rules:
visual_type must be exactly one of: "unicolour_icon", "tricolour_icon", "unicolour_illustration", "multicolour_illustration", "image"
theme is a shorthand describing the art style. You must pick one of:
- "Theme A" (Single colour abstract vector icon)
- "Theme B" (Flat 3-colour vector icon)
- "unicolour" (Minimal flat digital illustration)
- "multicolour" (Colourful digital scene illustration)
- "realistic" (Documentary style photograph)

Transcript to process:
{numbered_batch_text}
"""

        try:
            response = call_with_retry(
                lambda c: c.models.generate_content(
                    model=MODEL_NAME,
                    contents=[prompt],
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": VisualPlanSchema
                    }
                )
            )
            
            # The structured output returns a parsed dict when response_schema is provided,
            # or we can parse response.text. genai client often parses it cleanly.
            try:
                # If parsed is available in response:
                if hasattr(response, 'parsed') and response.parsed is not None:
                    # It's an instance of VisualPlanSchema
                    data = response.parsed.model_dump()
                else:
                    data = json.loads(response.text)
            except Exception as parse_err:
                print(f"Parse error: {parse_err}. Raw text: {response.text}")
                continue

            for chunk_num, chunk in enumerate(data.get("chunks", [])):
                try:
                    start_idx = chunk.get("start_line_index", 0)
                    end_idx = chunk.get("end_line_index", 0)

                    # Log raw indices for debugging
                    print(f"  Batch {batch_idx} chunk {chunk_num}: raw indices start={start_idx}, end={end_idx}, parsed_lines_count={len(parsed_lines)}, batch_offset={batch_offset}")

                    # If indices appear to be global (greater than or equal to batch_offset + len(batch_lines)),
                    # convert them to batch-relative by subtracting the batch offset
                    if start_idx >= batch_offset and start_idx < batch_offset + len(parsed_lines):
                        start_idx = start_idx - batch_offset
                        print(f"  Batch {batch_idx} chunk {chunk_num}: converted global start index to batch-relative")
                    if end_idx >= batch_offset and end_idx < batch_offset + len(parsed_lines):
                        end_idx = end_idx - batch_offset
                        print(f"  Batch {batch_idx} chunk {chunk_num}: converted global end index to batch-relative")

                    # Clamp indices to valid ranges just in case
                    start_idx = max(0, min(start_idx, len(parsed_lines) - 1))
                    end_idx = max(start_idx, min(end_idx, len(parsed_lines) - 1))

                    print(f"  Batch {batch_idx} chunk {chunk_num}: final indices start={start_idx}, end={end_idx}")

                    real_start_s = parsed_lines[start_idx]["start"]
                    real_end_s = parsed_lines[end_idx]["end"]

                    print(f"  Batch {batch_idx} chunk {chunk_num}: timestamps {real_start_s}s - {real_end_s}s")
                    
                    # Extract clean text directly from the source line —
                    # Gemini often returns the raw "[0.0 - 2.9] text" prefix or
                    # concatenates multiple lines, so we strip it ourselves.
                    raw_line = batch_lines[start_idx] if start_idx < len(batch_lines) else ""
                    clean_text = re.sub(r'^\[\d+\.?\d* - \d+\.?\d*\]\s*', '', raw_line).strip()
                    if not clean_text:
                        clean_text = chunk.get("transcript_text", "")

                    segment = AudioSegment(
                        session_id=uuid.UUID(session_id),
                        timestamp_start=real_start_s,
                        timestamp_end=real_end_s,
                        transcript_text=clean_text
                    )
                    db.add(segment)
                    db.commit()
                    db.refresh(segment)

                    for alt in chunk.get("alternatives", []):
                        v_type = alt.get("visual_type", "unicolour_icon")
                        category = alt.get("category", "action")
                        concept = alt.get("concept", "Unknown")
                        desc = alt.get("description", "")
                        tags = alt.get("tags", "")
                        
                        # --- NEW AUTOMATIC STYLE SELECTION ENGINE ---
                        visual_style = ""
                        generated_type = v_type

                        if v_type == 'unicolour_icon':
                            generated_type = "minimal unicolour vector icon"
                            visual_style = "Simple symbolic icon, single colour, simple geometric shapes, bold lines, centered composition, flat vector design.\n\nThe icon should be minimal, abstract, and clearly communicate the concept of " + concept + ".\n\nRestrictions:\nNo text\nNo logos\nNo branding\nNo background scene"
                        elif v_type == 'tricolour_icon':
                            generated_type = "flat 3-colour vector icon"
                            visual_style = "Simple recognizable icon, maximum 3 colours, minimal details, centered composition.\n\nThe icon should be simple, recognizable, and clearly communicate the concept of " + concept + ".\n\nRestrictions:\nNo text\nNo logos\nNo branding\nNo complicated background scene"
                        elif v_type == 'unicolour_illustration':
                            generated_type = "minimal unicolour flat digital illustration"
                            visual_style = "Flat digital illustration style, NOT a photograph. Single colour palette with tints and shades. Clean vector-like shapes, minimal detail, bold outlines, simple storytelling composition. Think editorial infographic art or textbook diagram illustration. No photographic textures, no gradients that look like lighting, no realistic shadows."
                        elif v_type == 'multicolour_illustration':
                            generated_type = "multicolour flat digital illustration"
                            visual_style = "Colourful flat digital illustration style, NOT a photograph. Think modern explainer video art or editorial infographic — clean vector shapes, bold colour fills, soft cel-shading at most. Human figures should be stylised, not realistic. Contextual scene with narrative clarity. No photographic textures, no realistic lighting, no cinematic depth-of-field."
                        else:
                            generated_type = "image"
                            visual_style = "Documentary style image, natural lighting, cinematic composition, realistic environments, educational B-roll quality."

                        init_prompt = f"Create a {generated_type}."
                        if desc and desc.strip():
                            init_prompt += f"\n\n{desc}"
                        if course.scenario_description and course.scenario_description.strip():
                            init_prompt += f"\n\nVisual Scene Setup\n{course.scenario_description}"
                        if course.about_course and course.about_course.strip():
                            init_prompt += f"\n\nAbout This Course\n{course.about_course}"
                        init_prompt += f"\n\nVisual Style\n{visual_style}"
                        init_prompt += f"\n\nQuality Requirements\nHigh resolution\nClear composition\nSuitable for educational course videos\n16:9 aspect ratio\n\nRestrictions"
                        
                        if course.custom_restrictions and course.custom_restrictions.strip():
                            init_prompt += f"\n{course.custom_restrictions}"
                        else:
                            init_prompt += f"\nNo logos or branding\nNo watermarks\nNo foreign subjects\nNo foreign currency\nUse Indian currency (₹) when money is shown\nABSOLUTELY NO TEXT, letters, numbers, or words anywhere in the image\nNo political or sensitive content"
                        
                        svp = SegmentVisualPlan(
                            segment_id=segment.id,
                            concept=concept,
                            category=category,
                            description=desc,
                            visual_type=v_type,
                            theme=alt.get("theme", "Theme A"),
                            tags=tags,
                            global_context="",
                            final_prompt=init_prompt
                        )
                        db.add(svp)
                    db.commit()
                except Exception as item_err:
                    print(f"Error persisting chunk: {item_err}")
                    db.rollback()
        except Exception as e:
            print(f"Error processing transcript batch {batch_idx + 1}: {e}")

        # Pace between batches — prevents all keys exhausting back-to-back
        if batch_idx < len(batches) - 1:
            time.sleep(8)

    # Fallback if absolutely nothing was generated
    # (Check if the session has any segments)
    existing_segments = db.query(AudioSegment).filter(AudioSegment.session_id == session_id).first()
    if not existing_segments:
        print("All parsing failed. Adding fallback segment.")
        fallback_segment = AudioSegment(
            session_id=uuid.UUID(session_id),
            timestamp_start=0.0, timestamp_end=15.0, # Just a dummy timestamp
            transcript_text=transcript[:200]
        )
        db.add(fallback_segment)
        db.commit()
        db.refresh(fallback_segment)

        fallback_plan = SegmentVisualPlan(
            segment_id=fallback_segment.id,
            concept="Fallback Concept",
            category="action",
            description="Placeholder due to JSON parsing failure",
            visual_type="icon",
            theme="Theme A",
            tags="",
            global_context="",
            final_prompt=f"""Create a minimal unicolour vector icon representing "Fallback Concept".

Scene Description
{course.scenario_description}
Placeholder

Course Context
{course.about_course}

Visual Style
Clean symbolic icon, single colour, simple geometric shapes, bold lines, centered composition, flat vector design.

Quality Requirements
High resolution
Suitable for educational course videos
16:9 aspect ratio

Restrictions
No logos or branding
No watermarks
No foreign subjects
No foreign currency
Avoid random text"""
        )
        db.add(fallback_plan)
        db.commit()


def curate_visual_plans(session_id: str, db) -> list[str]:
    """
    Selects ~10 visual plans per minute using deterministic even-distribution.
    No LLM call — guaranteed correct count every time.
    Within each minute, picks one plan per segment spread evenly across the timeline,
    preferring variety of visual_type across the selection.
    """
    from backend.models import AudioSegment, SegmentVisualPlan

    segments = (
        db.query(AudioSegment)
        .filter(AudioSegment.session_id == session_id)
        .order_by(AudioSegment.timestamp_start)
        .all()
    )

    all_plans = []
    for seg in segments:
        plans = [p for p in seg.alternative_plans if not p.is_disabled]
        for plan in plans:
            all_plans.append({
                "id":              str(plan.id),
                "timestamp_start": seg.timestamp_start,
                "visual_type":     plan.visual_type or "",
            })

    if not all_plans:
        return []

    TARGET_PER_MINUTE = 10

    # Group by minute
    buckets = defaultdict(list)
    for plan in all_plans:
        minute = int(plan["timestamp_start"] // 60)
        buckets[minute].append(plan)

    # Clear all curated flags
    all_ids = [p["id"] for p in all_plans]
    db.query(SegmentVisualPlan).filter(
        SegmentVisualPlan.id.in_(all_ids)
    ).update({"is_curated": False}, synchronize_session=False)
    db.commit()

    final_ids = []

    for minute in sorted(buckets.keys()):
        plans = sorted(buckets[minute], key=lambda p: p["timestamp_start"])
        n = len(plans)
        pick = min(TARGET_PER_MINUTE, n)

        if pick == 0:
            continue

        # Evenly-spaced indices across the bucket
        indices = [round(i * (n - 1) / (pick - 1)) if pick > 1 else 0 for i in range(pick)]
        # Deduplicate while preserving order
        seen = set()
        chosen_indices = []
        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                chosen_indices.append(idx)

        # If dedup reduced count, fill from remaining
        if len(chosen_indices) < pick:
            all_indices = set(range(n))
            extras = [i for i in all_indices if i not in seen]
            chosen_indices.extend(extras[:pick - len(chosen_indices)])

        chosen = [plans[i]["id"] for i in sorted(chosen_indices)]
        final_ids.extend(chosen)
        print(f"  [Curation] Minute {minute + 1}: selected {len(chosen)}/{n} plans")

    if final_ids:
        db.query(SegmentVisualPlan).filter(
            SegmentVisualPlan.id.in_(final_ids)
        ).update({"is_curated": True}, synchronize_session=False)
        db.commit()

    print(f"  [Curation] Done — {len(final_ids)} plans marked across {len(buckets)} minute(s)")
    return final_ids
