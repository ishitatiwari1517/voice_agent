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

from prompts import SYSTEM_PROMPT, build_user_message

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
GEMINI_MODEL  = "gemini-1.5-flash"   # free tier, vision-capable

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
        parts = [types.Part.from_text(msg["text"])]
        contents.append(types.Content(role=role, parts=parts))
    return contents


def bytes_to_pil(image_bytes: bytes) -> Image.Image:
    return Image.open(BytesIO(image_bytes)).convert("RGB")


def pil_to_part(img: Image.Image) -> types.Part:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


# ─── Shared Gemini call ───────────────────────────────────────────────────────

def call_gemini(history: list, user_text: str, pil_image: Image.Image) -> str:
    """
    Build a multi-turn request:
      - previous history (text only)
      - new user turn = [text + image]
    Returns the model's narration string.
    """
    past = history_to_contents(trim(history))

    new_turn = types.Content(
        role="user",
        parts=[
            types.Part.from_text(user_text),
            pil_to_part(pil_image),
        ],
    )

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=past + [new_turn],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=1024,
            temperature=0.85,
        ),
    )
    return response.text.strip()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "alive", "service": "VisionVoice", "vision": GEMINI_MODEL}


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

        narration = call_gemini(history, user_text, pil_image)

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
        # 1 ── Transcribe
        if audio and audio.filename:
            buf = BytesIO(await audio.read())
            buf.name = audio.filename or "recording.webm"
            tx = await openai_client.audio.transcriptions.create(
                model="whisper-1", file=buf, response_format="text"
            )
            final_question = tx.strip()
        else:
            final_question = question or "Where am I? What's around me? What does this place feel like?"

        # 2 ── Gemini Vision
        if not session_id:
            session_id = str(uuid.uuid4())

        pil_image = bytes_to_pil(await image.read())
        history   = get_session(session_id)
        user_text = build_user_message(final_question, language)

        narration = call_gemini(history, user_text, pil_image)

        history.append({"role": "user",  "text": final_question})
        history.append({"role": "model", "text": narration})
        sessions[session_id] = trim(history)

        # 3 ── Browser Speech Synthesis (skip ElevenLabs entirely)
        # We'll use browser's Web Speech API instead - free and unlimited
        audio_b64 = None

        return JSONResponse({
            "transcript": final_question,
            "narration":  narration,
            "audio_b64":  audio_b64,
            "session_id": session_id,
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

