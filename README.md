# VisionVoice

> A real-time AI voice agent that sees the world through your camera — and narrates it back like a story worth living.

Built for people who are blind or visually impaired. Not alt-text. Not a list of objects. A narration — the kind a great friend with eyes would give.

---

## Architecture

```
User speaks → Whisper (STT) → Camera frame capture
    ↓
Claude Vision (narrative prompt) → ElevenLabs (warm TTS voice)
    ↓
Audio plays back in phone browser
```

### Backend (`/backend`) — Python FastAPI
| Endpoint | What it does |
|---|---|
| `POST /narrate` | All-in-one: transcribe + describe + speak |
| `POST /transcribe` | Whisper STT only |
| `POST /describe` | Claude Vision narration only |
| `POST /speak` | ElevenLabs TTS only |
| `DELETE /session/{id}` | Clear conversation memory |
| `GET /health` | Health check |

### Frontend (`/frontend`) — Vanilla HTML/CSS/JS
- No framework, no build step — works directly from file system or any static host
- Mobile-first, designed for phone browser use
- WCAG-accessible (keyboard mic control, ARIA live regions, skip link)

---

## Setup

### 1. Get API keys
| Service | Where | Required? |
|---|---|---|
| Anthropic | [console.anthropic.com](https://console.anthropic.com) | ✅ |
| OpenAI | [platform.openai.com](https://platform.openai.com) | ✅ |
| ElevenLabs | [elevenlabs.io](https://elevenlabs.io) | Optional (falls back to text-only) |

### 2. Backend
```bash
cd backend
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API keys

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Frontend
```bash
# Option A — direct (no server needed for local testing)
open frontend/index.html

# Option B — local HTTP server (required for camera on some browsers)
cd frontend
python -m http.server 3000
# Then open http://localhost:3000
```

> **Phone access**: Run the backend on your laptop, then in Settings set the backend URL to `http://<your-laptop-ip>:8000`. Make sure both devices are on the same Wi-Fi.

---

## Usage

1. Allow camera + microphone permissions when prompted
2. Point camera at your surroundings
3. **Hold the mic button** and ask anything:
   - *"Where am I?"*
   - *"What does this place feel like?"*
   - *"Is there anything I need to watch out for?"*
   - *"Tell me more about what's to my left"*
4. Release the button — VisionVoice narrates back in a warm voice
5. Follow-up questions remember context from previous answers

### Languages
Switch between **English / Hindi / Kannada** using the top-right buttons. The narration will match your selection.

### Hazard alerts
Hazards (steps, traffic, wet floors, obstacles) are always mentioned **first**, highlighted in amber, before any descriptive narration.

---

## ElevenLabs Voice

The default voice is **Sarah** (`EXAVITQu4vr4xnSDxMaL`) — warm, calm, present.

To change it:
1. Find a voice you love at [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library)
2. Copy the Voice ID
3. Set `ELEVENLABS_VOICE_ID=<your-id>` in `.env`

---

## Environment Variables

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
ELEVENLABS_API_KEY=...          # optional
ELEVENLABS_VOICE_ID=EXAVITQu4vr4xnSDxMaL   # optional
```

---

## Project Structure

```
voice_agent/
├── backend/
│   ├── main.py          # FastAPI app — all routes
│   ├── prompts.py       # System prompt + narration logic
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── index.html       # UI structure (accessible, semantic)
│   ├── style.css        # Dark glassmorphism design system
│   └── app.js           # Camera, recording, API, playback
└── README.md
```

---

## The idea

> *Blindness is not the absence of experience. It is the absence of a narrator.*
> *VisionVoice is that narrator.*

Every accessibility tool built for blind people tells them what is **there**.  
VisionVoice tells them what it **feels like** to be there.

---

*Built with Anthropic Claude, OpenAI Whisper, and ElevenLabs.*
