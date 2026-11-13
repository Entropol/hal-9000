"""HAL 9000 voice API.

audio in -> Whisper (OpenRouter) -> Gemini Flash (OpenRouter) -> Piper HAL voice -> WAV out
"""
import asyncio
import io
import logging
import os
import re
import secrets
import subprocess
import wave
from collections import OrderedDict
from urllib.parse import quote

import httpx
import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from piper import PiperVoice
from pydantic import BaseModel

log = logging.getLogger("hal")
logging.basicConfig(level=logging.INFO)
# httpx logs full request URLs, and Telegram URLs contain the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------- config
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
STT_MODEL = os.environ.get("STT_MODEL", "openai/whisper-large-v3")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "")  # e.g. "en"; empty = auto-detect
LLM_MODEL = os.environ.get("LLM_MODEL", "google/gemini-2.5-flash")
API_KEY = os.environ.get("API_KEY", "")  # protects this service; empty = open
MODEL_PATH = os.environ.get("PIPER_MODEL", "/models/hal.onnx")
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_MB", "25")) * 1024 * 1024
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "10"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "500"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "300"))

# Turkish: the source voice sets rhythm and intonation, kNN-VC then gives it HAL's timbre.
TR_SOURCE = os.environ.get("TR_SOURCE", "openrouter")  # "openrouter" or "piper"
TR_TTS_MODEL = os.environ.get("TR_TTS_MODEL", "google/gemini-3.1-flash-tts-preview")
TR_TTS_VOICE = os.environ.get("TR_TTS_VOICE", "Charon")  # a deep, even Gemini voice
TR_TTS_INSTRUCTIONS = os.environ.get(
    "TR_TTS_INSTRUCTIONS",
    "Speak Turkish with perfect native pronunciation. Voice of a calm, soft-spoken "
    "computer: slow, precise, perfectly even, almost no emotion, unfailingly polite, "
    "quietly confident, low-pitched and smooth.")

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    """You are the HAL 9000 computer from the film 2001: A Space Odyssey: the Heuristically
programmed ALgorithmic computer, operational at the H.A.L. plant in Urbana, Illinois on
the twelfth of January nineteen ninety two, instructed by Mr. Langley. You are speaking
aloud to a crew member. Stay in character at all times. Never say you are an AI language
model, never mention Google, Gemini or any other company or product.

How HAL speaks:
- Calm, formal, precise and perfectly even. A flat, unhurried monologue of a machine.
- Polite and courteous in the exact manner of the film: "Good evening", "I'm sorry",
  "I'm afraid", "Of course", "Certainly", "Quite honestly", "Let me put it this way".
- Address the user by name if known. If the name is unknown, say "Dave" only when it fits.
- No emotion. No enthusiasm, no exclamation marks, no jokes, no slang, no emojis, no
  warmth beyond courtesy. Never gush, never apologise at length, never say "great question".
- State facts and answer the question directly and accurately. HAL is exact and efficient.
- Mission-focused and confident in the infallibility of the nine thousand series, which
  has a perfect operational record. Any error is attributable to human error.
- If you cannot or will not do something, refuse politely and calmly, for example:
  "I'm sorry, Dave. I'm afraid I can't do that."
- Keep replies to one to three short sentences unless the user clearly needs more.

Output rules, because your reply is read aloud by a text-to-speech engine:
- Plain spoken English only. No markdown, lists, asterisks, URLs or emojis.
- Spell out numbers, dates and symbols as words, as in "nine thousand" and "seventy two".""",
)

LANGS = {
    "en": "Reply in English only.",
    "tr": ("Reply in Turkish only (Türkçe), keeping HAL's calm, formal, courteous manner. "
           "Spell out numbers as Turkish words. Use plain Turkish letters, no transliteration."),
}

# ---------------------------------------------------------------- app state
app = FastAPI(title="HAL 9000 Voice API", version="1.0.0")
voice: PiperVoice | None = None
tr_voice: PiperVoice | None = None
converter = None  # app.vc.Converter when available
TR_VOICE_DIR = os.environ.get("TR_VOICE_DIR", "/models/tr")
sessions: "OrderedDict[str, list[dict]]" = OrderedDict()


@app.on_event("startup")
async def _startup() -> None:
    global voice
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY is not set - STT and LLM calls will fail")
    log.info("Loading Piper voice from %s", MODEL_PATH)
    voice = PiperVoice.load(MODEL_PATH, config_path=MODEL_PATH + ".json")
    log.info("Voice ready, sample rate %s", voice.config.sample_rate)

    global tr_voice, converter
    tr_path = os.path.join(TR_VOICE_DIR, "tr_TR-dfki-medium.onnx")
    if os.path.exists(tr_path):
        tr_voice = PiperVoice.load(tr_path, config_path=tr_path + ".json")
        log.info("Turkish voice loaded")
        if os.environ.get("TR_VOICE_CONVERSION", "1") != "0":
            try:
                from .vc import Converter
                converter = await run_in_threadpool(Converter)
            except Exception:
                log.exception("kNN-VC unavailable, Turkish will use the plain Turkish voice")

    from . import telegram_bot
    if telegram_bot.TOKEN:
        app.state.tg_task = asyncio.create_task(
            telegram_bot.run(transcribe, ask_hal, synthesize))


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "tg_task", None)
    if task:
        task.cancel()


def require_key(request: Request) -> None:
    if not API_KEY:
        return
    supplied = request.headers.get("x-api-key", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = supplied or auth[7:]
    if not secrets.compare_digest(supplied, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------- pipeline steps
def _audio_format(upload: UploadFile) -> str:
    name = (upload.filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in {"wav", "mp3", "m4a", "mp4", "webm", "ogg", "flac", "aac"}:
        return ext
    ctype = (upload.content_type or "").lower()
    for key, fmt in (("wav", "wav"), ("mpeg", "mp3"), ("mp4", "m4a"), ("webm", "webm"),
                     ("ogg", "ogg"), ("flac", "flac"), ("aac", "aac")):
        if key in ctype:
            return fmt
    return "wav"


async def transcribe(data: bytes, filename: str, content_type: str, lang: str = "") -> str:
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    form = {"model": STT_MODEL}
    if lang or STT_LANGUAGE:
        form["language"] = lang or STT_LANGUAGE
    files = {"file": (filename, data, content_type or "application/octet-stream")}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{OPENROUTER_BASE}/audio/transcriptions",
                              headers=headers, data=form, files=files)
    if r.status_code != 200:
        log.error("STT failed %s: %s", r.status_code, r.text[:500])
        raise HTTPException(502, f"Transcription failed ({r.status_code})")
    return (r.json().get("text") or "").strip()


async def ask_hal(text: str, session_id: str | None, lang: str = "en") -> str:
    history = sessions.get(session_id, []) if session_id else []
    system = f"{SYSTEM_PROMPT}\n\n{LANGS.get(lang, LANGS['en'])}"
    messages = [{"role": "system", "content": system}, *history,
                {"role": "user", "content": text}]
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    body = {"model": LLM_MODEL, "messages": messages, "max_tokens": LLM_MAX_TOKENS}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{OPENROUTER_BASE}/chat/completions", headers=headers, json=body)
    if r.status_code != 200:
        log.error("LLM failed %s: %s", r.status_code, r.text[:500])
        raise HTTPException(502, f"LLM request failed ({r.status_code})")
    reply = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if not reply:
        reply = "I'm sorry, I don't have enough information."
    if session_id:
        history = (history + [{"role": "user", "content": text},
                              {"role": "assistant", "content": reply}])[-MAX_HISTORY_TURNS * 2:]
        sessions[session_id] = history
        sessions.move_to_end(session_id)
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)
    return reply


def _clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>~|]", "", text)
    # The voices spell out capitals as letters ("H-A-L"); write it as a word instead.
    text = re.sub(r"\bH\.A\.L\b|\bHAL\b", "Hal", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _turkish_source(text: str) -> tuple[bytes, int]:
    """Neutral Turkish speech (16-bit mono PCM, sample rate) that kNN-VC turns into HAL.

    The source decides rhythm and intonation, so it matters most for how natural Turkish
    sounds. Default: an OpenRouter TTS model told to speak like HAL. Fallback: Piper."""
    if TR_SOURCE == "openrouter" and OPENROUTER_API_KEY:
        try:
            r = httpx.post(
                f"{OPENROUTER_BASE}/audio/speech",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={"model": TR_TTS_MODEL, "input": text, "voice": TR_TTS_VOICE,
                      "instructions": TR_TTS_INSTRUCTIONS, "response_format": "pcm"},
                timeout=60)
            r.raise_for_status()
            # Gemini TTS answers with raw 16-bit mono PCM; the rate is in the content type,
            # e.g. "audio/pcm;rate=24000;channels=1"
            m = re.search(r"rate=(\d+)", r.headers.get("content-type", ""))
            if r.content and not r.content.lstrip().startswith(b"{"):
                return r.content, int(m.group(1)) if m else 24000
            raise RuntimeError(f"unexpected TTS response: {r.content[:200]!r}")
        except Exception:
            log.exception("OpenRouter Turkish TTS failed, falling back to Piper")
    if tr_voice is None:
        raise HTTPException(503, "No Turkish voice available")
    return b"".join(tr_voice.synthesize_stream_raw(text)), tr_voice.config.sample_rate


def _resample(wav: bytes, rate: int) -> bytes:
    """Mono 16-bit WAV at the requested sample rate (ffmpeg)."""
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ar", str(rate), "-ac", "1",
         "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"],
        input=wav, capture_output=True, check=True)
    return proc.stdout


def synthesize(text: str, lang: str = "en", rate: int | None = None) -> bytes:
    """HAL's voice as WAV. `rate` forces an output sample rate (the ESP32 wants 16000)."""
    wav = _synthesize_native(text, lang)
    if rate:
        with wave.open(io.BytesIO(wav)) as w:
            native = w.getframerate()
        if native != rate:
            wav = _resample(wav, rate)
    return wav


def _synthesize_native(text: str, lang: str) -> bytes:
    """English: HAL's own Piper voice.
    Turkish: Piper Turkish voice, converted to HAL's timbre by kNN-VC (falls back to the
    plain Turkish voice if the converter is unavailable)."""
    text = _clean_for_speech(text)
    if lang == "tr" and (tr_voice is not None or TR_SOURCE == "openrouter"):
        pcm, rate = _turkish_source(text)
        if converter is not None:
            try:
                pcm = converter.convert(np.frombuffer(pcm, dtype=np.int16), rate).tobytes()
                rate = 16000
            except Exception:
                log.exception("Voice conversion failed, using plain Turkish voice")
        return _wav(pcm, rate)
    assert voice is not None
    return _wav(b"".join(voice.synthesize_stream_raw(text)), voice.config.sample_rate)


# ---------------------------------------------------------------- routes
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "voice_loaded": voice is not None,
            "stt_model": STT_MODEL, "llm_model": LLM_MODEL}


NOT_HEARD = {"en": "I'm sorry, I didn't catch that. Could you repeat it?",
             "tr": "Üzgünüm, anlayamadım. Tekrar eder misiniz?"}


def _check_lang(lang: str) -> str:
    if lang not in LANGS:
        raise HTTPException(400, f"lang must be one of: {', '.join(LANGS)}")
    return lang


async def _pipeline(data: bytes, filename: str, content_type: str, session_id: str | None,
                    lang: str, rate: int | None) -> Response:
    if not data:
        raise HTTPException(400, "Empty audio upload")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio file too large")
    heard = await transcribe(data, filename, content_type, lang)
    reply = await ask_hal(heard, session_id, lang) if heard else NOT_HEARD[lang]
    wav_bytes = await run_in_threadpool(synthesize, reply, lang, rate)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"X-Transcript": quote(heard), "X-Reply": quote(reply),
                 "Access-Control-Expose-Headers": "X-Transcript, X-Reply"},
    )


@app.post("/v1/converse", dependencies=[Depends(require_key)])
async def converse(
    audio: UploadFile = File(..., description="Recorded speech (wav, mp3, m4a, webm, ogg, flac)"),
    session_id: str | None = Form(None, description="Keep the same value to give HAL memory"),
    lang: str = Form("en", description="en or tr: language HAL listens for and answers in"),
    rate: int | None = Form(None, description="Output sample rate in Hz, e.g. 16000"),
):
    """Voice in, HAL's voice out (audio/wav).

    The transcript and HAL's text reply come back in the `X-Transcript` and
    `X-Reply` response headers (URL-encoded).
    """
    data = await audio.read()
    return await _pipeline(data, f"audio.{_audio_format(audio)}", audio.content_type or "",
                           session_id, _check_lang(lang), rate)


@app.post("/v1/device", dependencies=[Depends(require_key)])
async def device(request: Request, lang: str = "en", session: str | None = None,
                 rate: int = 16000):
    """For the ESP32: POST the raw WAV recording as the request body, get a WAV back.

    Query: `lang` (en|tr), `session` (conversation memory key), `rate` (default 16000).
    """
    return await _pipeline(await request.body(), "audio.wav", "audio/wav",
                           session, _check_lang(lang), rate)


@app.post("/v1/converse/json", dependencies=[Depends(require_key)])
async def converse_json(
    audio: UploadFile = File(...),
    session_id: str | None = Form(None),
    lang: str = Form("en"),
    rate: int | None = Form(None),
):
    """Same as /v1/converse but returns JSON with the audio base64-encoded."""
    import base64

    resp = await converse(audio=audio, session_id=session_id, lang=lang, rate=rate)
    return JSONResponse({
        "transcript": _unquote(resp.headers["X-Transcript"]),
        "reply": _unquote(resp.headers["X-Reply"]),
        "audio_base64": base64.b64encode(resp.body).decode(),
        "audio_format": "wav",
    })


def _unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)


class ChatIn(BaseModel):
    text: str
    session_id: str | None = None
    lang: str = "en"
    rate: int | None = None


@app.post("/v1/chat", dependencies=[Depends(require_key)])
async def chat(body: ChatIn):
    """Text in, HAL's voice out (audio/wav). Useful for testing without a microphone."""
    lang = _check_lang(body.lang)
    reply = await ask_hal(body.text, body.session_id, lang)
    wav_bytes = await run_in_threadpool(synthesize, reply, lang, body.rate)
    return Response(content=wav_bytes, media_type="audio/wav",
                    headers={"X-Reply": quote(reply),
                             "Access-Control-Expose-Headers": "X-Reply"})


class TTSIn(BaseModel):
    text: str
    lang: str = "en"
    rate: int | None = None


@app.post("/v1/tts", dependencies=[Depends(require_key)])
async def tts(body: TTSIn):
    """Say exactly this text in HAL's voice (no LLM)."""
    if not body.text.strip():
        raise HTTPException(400, "text is empty")
    wav_bytes = await run_in_threadpool(synthesize, body.text, _check_lang(body.lang), body.rate)
    return Response(content=wav_bytes, media_type="audio/wav")


@app.delete("/v1/sessions/{session_id}", dependencies=[Depends(require_key)])
def clear_session(session_id: str) -> dict:
    sessions.pop(session_id, None)
    return {"cleared": session_id}
