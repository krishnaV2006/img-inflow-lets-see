import os
import io
import base64
import asyncio
from google import genai
from google.genai import types
from PIL import Image
from backend.services import s3_service
from backend.services.gemini_keys import call_with_retry

IMAGE_MODEL   = "gemini-3.1-flash-image-preview"
IMAGE_CONFIG  = types.GenerateContentConfig(
    response_modalities=["TEXT", "IMAGE"],
    image_config=types.ImageConfig(aspect_ratio="16:9"),
)


def _save_image_from_response(response, output_path: str) -> str:
    """Extract image bytes from a Gemini response and write to disk."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            image_data = part.inline_data.data
            if isinstance(image_data, str):
                image_bytes = base64.b64decode(image_data)
            elif isinstance(image_data, bytes):
                try:
                    if image_data[:4] != b'\x89PNG' and image_data[:2] != b'\xff\xd8':
                        image_bytes = base64.b64decode(image_data)
                    else:
                        image_bytes = image_data
                except Exception:
                    image_bytes = image_data
            print(f"Image: {len(image_bytes)} bytes, mime: {part.inline_data.mime_type}")
            with open(output_path, "wb") as f:
                f.write(image_bytes)
            print(f"Saved: {output_path}")
            return output_path
    raise Exception("No image returned by Gemini API")


async def _upload_to_s3_and_clean(local_path: str):
    if s3_service.is_configured():
        filename = os.path.basename(local_path)
        uploaded = s3_service.upload_image(local_path, filename)
        if uploaded:
            try:
                os.remove(local_path)
            except Exception as e:
                print(f"[S3] Warning: could not remove {local_path}: {e}")


async def generate_image(prompt: str, output_path: str) -> str:
    """Generate a 16:9 1K image using Gemini and save it."""
    print(f"[generate_image] {prompt[:120]}…")

    def _run():
        response = call_with_retry(
            lambda c: c.models.generate_content(
                model=IMAGE_MODEL,
                contents=[prompt],
                config=IMAGE_CONFIG,
            )
        )
        return _save_image_from_response(response, output_path)

    result = await asyncio.to_thread(_run)
    await _upload_to_s3_and_clean(result)
    return result


async def edit_image(source_image_path: str, modification_prompt: str, output_path: str) -> str:
    """Edit an existing image using Gemini with a modification instruction."""
    print(f"[edit_image] {source_image_path} — {modification_prompt[:80]}…")

    def _run():
        with open(source_image_path, "rb") as f:
            source_bytes = f.read()

        mime = "image/jpeg" if source_image_path.lower().endswith((".jpg", ".jpeg")) else "image/png"

        contents = [
            types.Part.from_bytes(data=source_bytes, mime_type=mime),
            f"Apply this change only: {modification_prompt}. Keep composition and style. No text or letters.",
        ]

        response = call_with_retry(
            lambda c: c.models.generate_content(
                model=IMAGE_MODEL,
                contents=contents,
                config=IMAGE_CONFIG,
            )
        )
        return _save_image_from_response(response, output_path)

    result = await asyncio.to_thread(_run)
    await _upload_to_s3_and_clean(result)
    return result
