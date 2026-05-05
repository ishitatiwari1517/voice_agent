/* ─── VisionVoice — app.js ─────────────────────────────────────────
   Flow:
   1. Camera starts automatically on load
   2. User holds mic button → records audio (MediaRecorder)
   3. On release: grab a camera frame + audio → POST /narrate
   4. Show narration text + autoplay ElevenLabs audio
   5. All interactions are accessible via keyboard (Space/Enter for mic)
────────────────────────────────────────────────────────────────── */

'use strict';

// ── Config ─────────────────────────────────────────────────────
const DEFAULT_API_URL = `${window.location.protocol}//${window.location.host}`;
const SETTINGS_KEY    = 'vv_settings';

// ── State ───────────────────────────────────────────────────────
let state = {
  apiUrl:       localStorage.getItem('vv_api_url') || DEFAULT_API_URL,
  language:     'english',
  sessionId:    localStorage.getItem('vv_session_id') || '',
  voiceEnabled: localStorage.getItem('vv_voice') !== 'false',
  cameraOn:     false,
  recording:    false,
  processing:   false,
  lastNarration: null,   // { question, narration, audioB64 }
  mediaRecorder: null,
  audioChunks:  [],
  stream:       null,
};

// ── DOM refs ────────────────────────────────────────────────────
const video          = document.getElementById('video-feed');
const canvas         = document.getElementById('capture-canvas');
const cameraOff      = document.getElementById('camera-off');
const cameraToggle   = document.getElementById('camera-toggle');
const enableCameraBtn= document.getElementById('enable-camera-btn');
const scanRing       = document.getElementById('scan-ring');
const processingOverlay = document.getElementById('processing-overlay');

const narrationIdle   = document.getElementById('narration-idle');
const narrationContent= document.getElementById('narration-content');
const narrationCard   = document.getElementById('narration-card');
const narrationQuestion = document.getElementById('narration-question');
const narrationText   = document.getElementById('narration-text');
const narrationAudio  = document.getElementById('narration-audio');
const replayBtn       = document.getElementById('replay-btn');
const copyBtn         = document.getElementById('copy-btn');

const micBtn          = document.getElementById('mic-btn');
const micLabel        = document.getElementById('mic-label');
const micIconDefault  = document.getElementById('mic-icon-default');
const micIconRecording= document.getElementById('mic-icon-recording');
const micOuterRing    = document.getElementById('mic-outer-ring');

const statusBar       = document.getElementById('status-bar');

const langBtns        = document.querySelectorAll('.lang-btn');
const settingsFab     = document.getElementById('settings-fab');
const settingsDrawer  = document.getElementById('settings-drawer');
const closeSettings   = document.getElementById('close-settings');
const apiUrlInput     = document.getElementById('api-url');
const voiceToggle     = document.getElementById('voice-toggle');
const clearMemoryBtn  = document.getElementById('clear-memory-btn');

// ── Utilities ───────────────────────────────────────────────────
function showStatus(msg, type = 'info', duration = 3000) {
  statusBar.textContent = msg;
  statusBar.className = `status-bar visible ${type === 'error' ? 'error' : ''}`;
  if (duration > 0) {
    setTimeout(() => { statusBar.className = 'status-bar'; }, duration);
  }
}

function clearStatus() { statusBar.className = 'status-bar'; }

function setProcessing(on) {
  state.processing = on;
  processingOverlay.hidden = !on;
  processingOverlay.setAttribute('aria-hidden', String(!on));
  scanRing.classList.toggle('active', on);
  micBtn.disabled = on;
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result.split(',')[1]);
    reader.onerror  = reject;
    reader.readAsDataURL(blob);
  });
}

function captureFrame() {
  if (!state.cameraOn || !video.videoWidth) return null;
  canvas.width  = video.videoWidth;
  canvas.height = video.videoHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(video, 0, 0);
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.85));
}

// ── Camera ─────────────────────────────────────────────────────
async function startCamera() {
  try {
    const constraints = {
      video: {
        facingMode: { ideal: 'environment' }, // rear camera on phones
        width:  { ideal: 1280 },
        height: { ideal: 960 },
      },
      audio: false,
    };
    state.stream = await navigator.mediaDevices.getUserMedia(constraints);
    video.srcObject = state.stream;
    await video.play();
    state.cameraOn = true;
    cameraOff.classList.add('hidden');
    cameraOff.hidden = true;
    cameraToggle.setAttribute('aria-pressed', 'true');
    showStatus('Camera on', 'info', 1500);
  } catch (err) {
    console.error('Camera error:', err);
    showStatus('Camera permission denied', 'error', 5000);
  }
}

function stopCamera() {
  if (state.stream) {
    state.stream.getTracks().forEach(t => t.stop());
    state.stream = null;
  }
  video.srcObject = null;
  state.cameraOn = false;
  cameraOff.hidden = false;
  cameraOff.classList.remove('hidden');
  cameraToggle.setAttribute('aria-pressed', 'false');
}

cameraToggle.addEventListener('click', () => {
  if (state.cameraOn) stopCamera();
  else startCamera();
});
enableCameraBtn.addEventListener('click', startCamera);

// ── Language picker ─────────────────────────────────────────────
langBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    langBtns.forEach(b => {
      b.classList.remove('active');
      b.setAttribute('aria-pressed', 'false');
    });
    btn.classList.add('active');
    btn.setAttribute('aria-pressed', 'true');
    state.language = btn.dataset.lang;
    showStatus(`Language: ${btn.textContent.trim()}`, 'info', 1500);
  });
});

// ── Mic Recording ───────────────────────────────────────────────
async function startRecording() {
  if (state.processing || state.recording) return;

  // Request mic permission
  let micStream;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    showStatus('Microphone permission denied', 'error', 5000);
    return;
  }

  state.audioChunks = [];
  state.recording   = true;

  // UI feedback
  micBtn.classList.add('recording');
  micBtn.setAttribute('aria-pressed', 'true');
  micIconDefault.style.display   = 'none';
  micIconRecording.style.display = 'block';
  micLabel.textContent = 'Recording…';
  micLabel.classList.add('recording-label');
  micOuterRing.classList.add('pulsing');
  showStatus('Listening…', 'info', 0);

  // MediaRecorder — prefer opus/webm, fall back to default
  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : MediaRecorder.isTypeSupported('audio/webm')
    ? 'audio/webm'
    : '';

  const recorder = new MediaRecorder(micStream, mimeType ? { mimeType } : {});
  state.mediaRecorder = recorder;

  recorder.ondataavailable = e => { if (e.data.size > 0) state.audioChunks.push(e.data); };
  recorder.onstop = async () => {
    micStream.getTracks().forEach(t => t.stop());
    await processRecording();
  };

  recorder.start(250); // collect in 250ms chunks
}

function stopRecording() {
  if (!state.recording || !state.mediaRecorder) return;
  state.recording = false;
  state.mediaRecorder.stop();

  // Reset mic UI immediately
  micBtn.classList.remove('recording');
  micBtn.setAttribute('aria-pressed', 'false');
  micIconDefault.style.display   = 'block';
  micIconRecording.style.display = 'none';
  micLabel.textContent = 'Hold to speak';
  micLabel.classList.remove('recording-label');
  micOuterRing.classList.remove('pulsing');
}

async function processRecording() {
  const audioBlob = new Blob(state.audioChunks, { type: 'audio/webm' });

  // Need at least a camera frame
  if (!state.cameraOn) {
    // Try to narrate without image — use a placeholder
    showStatus('Camera is off — narrating from voice only', 'info', 3000);
  }

  setProcessing(true);
  showStatus('Reading the scene…', 'info', 0);

  try {
    const imageBlob = await captureFrame();

    const formData = new FormData();
    formData.append('audio', audioBlob, 'recording.webm');
    if (imageBlob) formData.append('image', imageBlob, 'frame.jpg');
    formData.append('language', state.language);
    formData.append('session_id', state.sessionId);

    // If no camera, we still need an image field — send a 1px blank
    if (!imageBlob) {
      const blankCanvas = document.createElement('canvas');
      blankCanvas.width = 1; blankCanvas.height = 1;
      await new Promise(res => blankCanvas.toBlob(blob => {
        formData.append('image', blob, 'blank.jpg');
        res();
      }, 'image/jpeg'));
    }

    const res = await fetch(`${state.apiUrl}/narrate`, {
      method: 'POST',
      body: formData,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Server error');
    }

    const data = await res.json();
    state.sessionId = data.session_id;
    localStorage.setItem('vv_session_id', data.session_id);

    displayNarration(data.transcript, data.narration);

    if (state.voiceEnabled && data.audio_b64) {
      playAudio(data.audio_b64);
    }

    clearStatus();
  } catch (err) {
    console.error(err);
    showStatus(`Error: ${err.message}`, 'error', 6000);
  } finally {
    setProcessing(false);
  }
}

// ── Display narration ───────────────────────────────────────────
function displayNarration(question, narration) {
  state.lastNarration = { question, narration };

  narrationQuestion.textContent = question || 'Your question';
  narrationText.innerHTML = formatNarration(narration);

  narrationIdle.hidden    = true;
  narrationContent.hidden = false;
  narrationCard.classList.add('has-content');
}

function formatNarration(text) {
  // Detect hazard sentences — those containing safety words
  const hazardWords = ['step', 'stairs', 'drop', 'traffic', 'vehicle', 'car', 'wet',
    'slippery', 'uneven', 'careful', 'watch out', 'obstacle', 'hazard', 'caution'];

  const sentences = text.split(/(?<=[.!?])\s+/);
  let out = '';
  let hazardDone = false;

  for (const s of sentences) {
    const lower = s.toLowerCase();
    const isHazard = hazardWords.some(w => lower.includes(w));
    if (isHazard && !hazardDone) {
      out += `<div class="hazard-highlight">⚠️ ${escapeHtml(s)}</div>`;
      hazardDone = true;
    } else {
      out += escapeHtml(s) + ' ';
    }
  }
  return out.trim();
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Audio playback – Browser Native Speech Synthesis (Web Speech API) ──
function playAudio(b64) {
  // Ignore ElevenLabs b64, use Web Speech API instead (free, unlimited)
  if (!state.lastNarration) return;
  
  // Cancel any ongoing speech
  if (window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
  }
  
  const utterance = new SpeechSynthesisUtterance(state.lastNarration.narration);
  utterance.rate = 1.0;
  utterance.pitch = 1.0;
  utterance.volume = 1.0;
  
  window.speechSynthesis.speak(utterance);
}

replayBtn.addEventListener('click', () => {
  playAudio();
});

copyBtn.addEventListener('click', async () => {
  if (!state.lastNarration) return;
  try {
    await navigator.clipboard.writeText(state.lastNarration.narration);
    copyBtn.textContent = '✓ Copied';
    setTimeout(() => {
      copyBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <rect x="5" y="5" width="9" height="9" rx="1.5" stroke="currentColor" stroke-width="1.5"/>
        <path d="M3 11H2a1 1 0 01-1-1V2a1 1 0 011-1h8a1 1 0 011 1v1" stroke="currentColor" stroke-width="1.5"/>
      </svg> Copy`;
    }, 2000);
  } catch {
    showStatus('Copy failed', 'error', 2000);
  }
});

// ── Mic button — pointer events (touch + mouse) ─────────────────
micBtn.addEventListener('pointerdown', e => {
  e.preventDefault();
  startRecording();
});
micBtn.addEventListener('pointerup',  stopRecording);
micBtn.addEventListener('pointercancel', stopRecording);

// Keyboard support: Space/Enter to hold-to-record
micBtn.addEventListener('keydown', e => {
  if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) {
    e.preventDefault();
    startRecording();
  }
});
micBtn.addEventListener('keyup', e => {
  if (e.key === ' ' || e.key === 'Enter') {
    e.preventDefault();
    stopRecording();
  }
});

// ── Settings ────────────────────────────────────────────────────
settingsFab.addEventListener('click', () => {
  settingsDrawer.hidden = false;
  settingsFab.setAttribute('aria-expanded', 'true');
  apiUrlInput.value = state.apiUrl;
  voiceToggle.checked = state.voiceEnabled;
  closeSettings.focus();
});

closeSettings.addEventListener('click', closeSettingsDrawer);
settingsDrawer.addEventListener('click', e => {
  if (e.target === settingsDrawer) closeSettingsDrawer();
});

function closeSettingsDrawer() {
  settingsDrawer.hidden = true;
  settingsFab.setAttribute('aria-expanded', 'false');
  settingsFab.focus();

  // Save settings
  state.apiUrl = apiUrlInput.value.trim().replace(/\/$/, '') || DEFAULT_API_URL;
  state.voiceEnabled = voiceToggle.checked;
  localStorage.setItem('vv_api_url', state.apiUrl);
  localStorage.setItem('vv_voice', String(state.voiceEnabled));
}

voiceToggle.addEventListener('change', () => {
  document.getElementById('voice-toggle-label').textContent =
    voiceToggle.checked ? 'Voice on' : 'Voice off';
  voiceToggle.setAttribute('aria-checked', String(voiceToggle.checked));
});

clearMemoryBtn.addEventListener('click', async () => {
  if (!state.sessionId) { showStatus('No memory to clear', 'info', 2000); return; }
  try {
    await fetch(`${state.apiUrl}/session/${state.sessionId}`, { method: 'DELETE' });
    state.sessionId = '';
    localStorage.removeItem('vv_session_id');
    showStatus('Memory cleared ✓', 'info', 2000);
  } catch {
    // Session may not exist yet — just clear locally
    state.sessionId = '';
    localStorage.removeItem('vv_session_id');
    showStatus('Memory cleared ✓', 'info', 2000);
  }
});

// ── Keyboard trap for settings drawer ──────────────────────────
settingsDrawer.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSettingsDrawer();
});

// ── Init ────────────────────────────────────────────────────────
(async function init() {
  // Restore API URL in input
  apiUrlInput.value = state.apiUrl;

  // Start camera
  await startCamera();

  // Check backend health quietly
  try {
    const r = await fetch(`${state.apiUrl}/health`, { signal: AbortSignal.timeout(3000) });
    if (r.ok) showStatus('Connected to VisionVoice', 'info', 2000);
  } catch {
    showStatus('Backend not reachable — check Settings', 'error', 5000);
  }
})();
