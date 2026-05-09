import os
import uuid
import base64
import asyncio
from typing import Optional
from io import BytesIO

import httpx
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from openai import AsyncOpenAI
from google import genai
from google.genai import types
from PIL import Image

from .prompts import SYSTEM_PROMPT, build_user_message

load_dotenv()

app = FastAPI(title="VisionVoice API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Clients ──────────────────────────────────────────────────────────────────

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
GEMINI_MODEL  = "gemini-flash-latest"   # Modern alias mapping to free-tier model

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")

# ─── Session store ────────────────────────────────────────────────────────────
# Each session stores plain-text history: [{"role":"user","text":"..."}, ...]
# Converted to types.Content on each request.
sessions: dict[str, list] = {}
MAX_TURNS = 8   # keep last 8 exchanges (16 messages)


def get_session(session_id: str) -> list:
    return sessions.setdefault(session_id, [])


def trim(history: list) -> list:
    return history[-(MAX_TURNS * 2):] if len(history) > MAX_TURNS * 2 else history


def history_to_contents(history: list) -> list[types.Content]:
    """Convert stored text history to Gemini Content objects."""
    contents = []
    for msg in history:
        role  = msg["role"]                       # "user" | "model"
        parts = [types.Part(text=msg["text"])]
        contents.append(types.Content(role=role, parts=parts))
    return contents


def bytes_to_pil(image_bytes: bytes) -> Image.Image:
    return Image.open(BytesIO(image_bytes)).convert("RGB")


def pil_to_part(img: Image.Image) -> types.Part:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return types.Part(
        inline_data=types.Blob(data=buf.getvalue(), mime_type="image/jpeg")
    )


# ─── Shared Gemini call ───────────────────────────────────────────────────────

async def call_gemini(history: list, user_text: str, pil_image: Image.Image) -> str:
    """
    Build a multi-turn request with retry logic.
    Raises HTTPException on failure — never returns fake responses.
    """
    past = history_to_contents(trim(history))

    new_turn = types.Content(
        role="user",
        parts=[
            types.Part(text=user_text),
            pil_to_part(pil_image),
        ],
    )

    last_error = None
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=past + [new_turn],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=1024,
                    temperature=0.7,
                ),
            )
            return response.text.strip()
        except Exception as e:
            last_error = e
            error_str = str(e)
            print(f"Gemini attempt {attempt + 1}/3 failed: {error_str}")

            # Check for rate limit / quota (429)
            is_quota = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower()
            # Check for auth issues (403 / bad API key)
            is_auth  = "403" in error_str or "API_KEY" in error_str or "invalid" in error_str.lower() or "unauthorized" in error_str.lower()

            if is_auth:
                raise HTTPException(403, "Gemini API key is invalid or not enabled. Please check your GEMINI_API_KEY in Render Environment Variables.")

            if is_quota and attempt < 2:
                wait_time = (2 ** attempt) * 3   # 3s, then 6s
                print(f"Rate limited — waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
                continue
            elif is_quota:
                raise HTTPException(429, "Gemini is rate-limited right now. Please wait 10 seconds and try again.")
            else:
                # Unknown error — raise immediately, don't retry
                raise HTTPException(500, f"Gemini error: {error_str[:200]}")

    raise HTTPException(500, f"Gemini failed after 3 attempts: {str(last_error)[:200]}")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "alive", "service": "VisionVoice", "vision": GEMINI_MODEL, "version": "2.0.0",
            "gemini_key_set": bool(os.getenv("GEMINI_API_KEY"))}


@app.get("/test-gemini")
async def test_gemini():
    """Diagnostic: tests Gemini API with a simple text call. Open in browser to debug."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return {"success": False, "error": "GEMINI_API_KEY is not set in environment variables"}
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Say exactly: I am working correctly"
        )
        return {"success": True, "response": response.text.strip(), "model": GEMINI_MODEL}
    except Exception as e:
        return {"success": False, "error": str(e), "error_type": type(e).__name__, "model": GEMINI_MODEL}

@app.get("/test-models")
async def test_models():
    """Diagnostic: lists all available models for this API key."""
    try:
        models = []
        for m in gemini_client.models.list():
            models.append(m.name)
        return {"success": True, "models": models}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/demo")
async def demo():
    """Demo endpoint - returns a sample narration for testing."""
    return {
        "transcript": "What can you see around me?",
        "narration": "I can see you're in a well-lit indoor space with a modern aesthetic. The environment has a calm atmosphere with soft lighting and clean lines. There's a sense of order and organization around you. The space appears comfortable and tech-forward.",
        "audio_b64": None,
        "session_id": "demo"
    }


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Whisper STT — converts voice recording to text."""
    try:
        audio_bytes = await audio.read()
        buf = BytesIO(audio_bytes)
        buf.name = audio.filename or "recording.webm"
        result = await openai_client.audio.transcriptions.create(
            model="whisper-1", file=buf, response_format="text"
        )
        return {"transcript": result.strip()}
    except Exception as e:
        raise HTTPException(500, f"Transcription failed: {e}")


@app.post("/describe")
async def describe(
    image:      UploadFile = File(...),
    question:   str  = Form(default="Where am I? What's around me? What does this place feel like?"),
    language:   str  = Form(default="english"),
    session_id: str  = Form(default=""),
):
    """Gemini Vision — narrates one camera frame."""
    try:
        if not session_id:
            session_id = str(uuid.uuid4())

        pil_image = bytes_to_pil(await image.read())
        history   = get_session(session_id)
        user_text = build_user_message(question, language)

        narration = await call_gemini(history, user_text, pil_image)

        history.append({"role": "user",  "text": question})
        history.append({"role": "model", "text": narration})
        sessions[session_id] = trim(history)

        return {"narration": narration, "session_id": session_id}
    except Exception as e:
        raise HTTPException(500, f"Description failed: {e}")


@app.post("/speak")
async def speak(text: str = Form(...)):
    """ElevenLabs TTS — returns streamed mp3 audio with retry logic."""
    if not ELEVENLABS_API_KEY:
        raise HTTPException(503, "ElevenLabs key not set")
    try:
        url     = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
        headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.55, "similarity_boost": 0.80,
                               "style": 0.30, "use_speaker_boost": True},
        }
        
        # Retry logic for rate limiting
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.post(url, json=payload, headers=headers)
                    if r.status_code == 200:
                        return StreamingResponse(BytesIO(r.content), media_type="audio/mpeg")
                    elif r.status_code == 429:  # Rate limit
                        if attempt < 2:
                            wait_time = (2 ** attempt) * 2  # exponential backoff: 2s, 4s
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            raise HTTPException(429, "ElevenLabs rate limit exceeded. Please try again later.")
                    else:
                        raise HTTPException(502, f"ElevenLabs error: {r.status_code} - {r.text}")
            except HTTPException:
                raise
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                raise HTTPException(500, f"TTS failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"TTS failed: {str(e)}")



@app.post("/narrate")
async def narrate_full(
    audio:      Optional[UploadFile] = File(default=None),
    image:      UploadFile           = File(...),
    question:   str  = Form(default=""),
    language:   str  = Form(default="english"),
    session_id: str  = Form(default=""),
):
    """
    All-in-one pipeline:
      1. Whisper STT (if audio provided)
      2. Gemini Vision narration
      3. ElevenLabs TTS (if key configured)
    Returns { transcript, narration, audio_b64, session_id }
    """
    try:
        # 1 ── Transcribe (fail-safe: if Whisper/OpenAI fails, fall back gracefully)
        if audio and audio.filename:
            try:
                buf = BytesIO(await audio.read())
                buf.name = audio.filename or "recording.webm"
                tx = await openai_client.audio.transcriptions.create(
                    model="whisper-1", file=buf, response_format="text"
                )
                final_question = tx.strip() or question or "What's around me?"
            except Exception as whisper_err:
                print(f"Whisper transcription failed (using fallback): {whisper_err}")
                final_question = question or "Where am I? What's around me? What does this place feel like?"
        else:
            final_question = question or "Where am I? What's around me? What does this place feel like?"

        # 2 ── Gemini Vision
        if not session_id:
            session_id = str(uuid.uuid4())

        pil_image = bytes_to_pil(await image.read())
        history   = get_session(session_id)
        user_text = build_user_message(final_question, language)

        narration = await call_gemini(history, user_text, pil_image)

        history.append({"role": "user",  "text": final_question})
        history.append({"role": "model", "text": narration})
        sessions[session_id] = trim(history)

        # 3 ── Audio: use browser Web Speech API (free, unlimited)
        # audio_b64 is null — frontend uses Web Speech API to speak narration
        return JSONResponse({
            "transcript":  final_question,
            "narration":   narration,
            "audio_b64":   None,
            "speak_text":  narration,   # frontend uses this to trigger speech
            "session_id":  session_id,
        })

    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/session/{session_id}")
async def clear_session(session_id: str):
    sessions.pop(session_id, None)
    return {"cleared": session_id}

# ─── Serve Frontend ───────────────────────────────────────────────────────────
frontend_path = os.path.join(os.path.dirname(__file__), "../frontend")
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")

