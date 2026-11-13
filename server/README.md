# HAL 9000 Voice API

Send a voice recording, get HAL's voice back.

```
audio ──► Whisper (OpenRouter) ──► Gemini Flash (OpenRouter) ──► Piper HAL voice ──► WAV
```

The voice is [campwill/HAL-9000-Piper-TTS](https://huggingface.co/campwill/HAL-9000-Piper-TTS),
downloaded into the image at build time. It runs on CPU: 1 vCPU / 1 GB RAM is enough.
Speech-to-text and the LLM run on OpenRouter, so you only need one API key.

## Deploy on Coolify

1. Push this folder to a Git repo.
2. Coolify → **New Resource** → Application → pick the repo → **Build Pack: Dockerfile**.
3. Set **Ports Exposes** to `8000`.
4. Add environment variables: `OPENROUTER_API_KEY` and `API_KEY` (see `.env.example`).
5. Assign a domain and deploy. Coolify uses the `/health` endpoint as the healthcheck.

Conversation memory (`session_id`) is kept in process memory, so it resets on redeploy.
Run a single instance.

## Test with Telegram

1. In Telegram, talk to **@BotFather**, send `/newbot`, and copy the token.
2. Set `TELEGRAM_BOT_TOKEN` in Coolify (or `.env`) and redeploy.
3. Open your bot, hold the mic button, and send a voice message. HAL replies with a voice
   message, with the transcript and his text reply as the caption.

The bot uses long polling, so it needs no domain or webhook. To restrict it, set
`TELEGRAM_ALLOWED_USERS` to your numeric Telegram id (the bot tells you the id if you
are not on the list). Run only one instance, or two pollers will fight over updates.

## API

All endpoints except `/health` need `X-API-Key: <API_KEY>` (or `Authorization: Bearer <API_KEY>`).

### `POST /v1/converse`  (voice → voice)

Multipart form: `audio` (file: wav/mp3/m4a/webm/ogg/flac, max 25 MB) and an optional
`session_id` (reuse it so HAL remembers the conversation).

Returns `audio/wav`. The transcript and HAL's text reply are in the `X-Transcript` and
`X-Reply` headers (URL-encoded).

```bash
curl -X POST https://hal.example.com/v1/converse \
  -H "X-API-Key: $API_KEY" \
  -F audio=@question.wav \
  -F session_id=dave \
  -D headers.txt -o hal.wav
```

Optional form fields: `lang` (`en` or `tr`, default `en`) and `rate` (output sample rate in Hz).

### `POST /v1/device`  (for the ESP32)

The raw WAV recording is the request body, with no multipart. The response is a mono
16-bit WAV at 16 kHz, the same format the firmware expects from the n8n flow.

```bash
curl -X POST "https://hal.example.com/v1/device?lang=tr&session=desk" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: audio/wav" \
  --data-binary @recording.wav -o reply.wav
```

Query: `lang` (`en`|`tr`), `session` (conversation memory key), `rate` (default `16000`).
The transcript and the text reply are in the `X-Transcript` and `X-Reply` headers.

### `POST /v1/converse/json`

Same input, but returns JSON: `{transcript, reply, audio_base64, audio_format}`.
Handy for browsers and apps that don't want to read headers.

### `POST /v1/chat`  (text → voice)

```bash
curl -X POST https://hal.example.com/v1/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"text": "Open the pod bay doors, HAL.", "session_id": "dave"}' -o hal.wav
```

### `POST /v1/tts`  (say exactly this)

```bash
curl -X POST https://hal.example.com/v1/tts \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"text": "I am sorry Dave, I am afraid I cannot do that."}' -o hal.wav
```

### `DELETE /v1/sessions/{session_id}`, `GET /health`

## Run locally

```bash
docker build -t hal .
docker run --rm -p 8000:8000 --env-file .env hal
```

## Notes

- Languages: English uses the HAL Piper voice directly. For Turkish, a Gemini TTS voice
  (via OpenRouter, told to speak like a calm computer) reads the text, because it has natural
  Turkish rhythm and intonation. Then kNN-VC changes its timbre to HAL's. The reference is
  HAL's own Piper voice reading `app/ref_sentences.txt` (about 8 minutes, generated at build
  time), so Turkish is matched to exactly the voice the English replies use. In a
  speaker-similarity test this beat using the 96 noisy movie clips as the reference
  (0.66 vs 0.59 against the English HAL voice; 1.0 would be identical). Add more sentences to
  that file to widen the reference. `VC_TOPK` (default 8) trades smoothness for fidelity.
  A Turkish reply takes roughly 3-20 s (mostly the Gemini call) and costs a fraction of a cent.
  - `TR_TTS_MODEL` (default `google/gemini-3.1-flash-tts-preview`), `TR_TTS_VOICE`
    (default `Charon`), `TR_TTS_INSTRUCTIONS` tune the source voice.
  - `TR_SOURCE=piper` uses Piper's Turkish voice instead: free and offline, but flat and
    robotic. It is also the automatic fallback if the OpenRouter call fails.
  - `TR_VOICE_CONVERSION=0` turns the HAL conversion off.
- The image is about 3 GB (PyTorch CPU, WavLM and the voice models are baked in).
- Change HAL's personality with the `SYSTEM_PROMPT` env var.
- `LLM_MODEL` defaults to `google/gemini-2.5-flash`. Set it to any OpenRouter model slug,
  for example a newer Gemini Flash.
- The HAL voice is trained on audio from the film, which is copyrighted. Fine for a personal
  project; check before using it commercially. Note that `docs/mimari.md` chose not to clone
  the film voice on ElevenLabs because of its usage policy. This server does not use
  ElevenLabs, but the underlying concern about the film audio is the same.
