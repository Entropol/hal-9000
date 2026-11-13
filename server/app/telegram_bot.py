"""Telegram test front-end for HAL. Long polling, no webhook needed.

Send HAL a voice message (or text) and he answers with a voice message.
Enabled only when TELEGRAM_BOT_TOKEN is set.
"""
import asyncio
import logging
import os
import subprocess

import httpx

log = logging.getLogger("hal.telegram")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(" ", "").split(",") if x}
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"

langs: dict[int, str] = {}  # chat_id -> "en" | "tr" (resets on restart)


def _wav_to_ogg_opus(wav: bytes) -> bytes:
    """Telegram only shows a playable voice bubble for OGG/Opus."""
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus",
         "-b:a", "32k", "-f", "ogg", "pipe:1"],
        input=wav, capture_output=True, check=True,
    )
    return proc.stdout


async def _handle(client: httpx.AsyncClient, msg: dict, transcribe, ask_hal, synthesize) -> None:
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    if ALLOWED and user_id not in ALLOWED:
        await client.post(f"{API}/sendMessage", json={
            "chat_id": chat_id, "text": f"Access denied. Your user id is {user_id}."})
        return

    text = msg.get("text")
    media = msg.get("voice") or msg.get("audio")
    command = text.split()[0].split("@")[0].lower() if text and text.startswith("/") else None

    if command in ("/en", "/tr"):
        langs[chat_id] = command[1:]
        confirm = {"en": "Language set to English.", "tr": "Dil Türkçe olarak ayarlandı."}
        await client.post(f"{API}/sendMessage", json={
            "chat_id": chat_id, "text": confirm[langs[chat_id]]})
        return
    if command in ("/start", "/help"):
        await client.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": "Good afternoon. I am a HAL nine thousand computer. "
                    "Send me a voice message.\n\n/en - English\n/tr - Türkçe"})
        return
    lang = langs.get(chat_id, "en")

    await client.post(f"{API}/sendChatAction", json={"chat_id": chat_id, "action": "record_voice"})
    session = f"tg-{chat_id}"

    if media:
        info = (await client.get(f"{API}/getFile", params={"file_id": media["file_id"]})).json()
        path = info["result"]["file_path"]
        data = (await client.get(f"{FILE_API}/{path}")).content
        ext = path.rsplit(".", 1)[-1] if "." in path else "ogg"
        heard = await transcribe(data, f"audio.{ext}", "", lang)
    elif text and not text.startswith("/"):
        heard = text
    else:
        return

    log.info("[%s] heard: %r", lang, heard)
    if heard:
        reply = await ask_hal(heard, session, lang)
    elif lang == "tr":
        reply = "Üzgünüm, anlayamadım."
    else:
        reply = "I'm sorry, I didn't catch that."
    log.info("[%s] reply: %r", lang, reply)
    wav = await asyncio.to_thread(synthesize, reply, lang)
    ogg = await asyncio.to_thread(_wav_to_ogg_opus, wav)

    await client.post(f"{API}/sendVoice",
                      data={"chat_id": str(chat_id)},
                      files={"voice": ("hal.ogg", ogg, "audio/ogg")})


async def run(transcribe, ask_hal, synthesize) -> None:
    if not TOKEN:
        return
    log.info("Telegram bot polling started")
    offset = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10)) as client:
        while True:
            try:
                r = await client.get(f"{API}/getUpdates", params={"timeout": 50, "offset": offset})
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        try:
                            await _handle(client, upd["message"], transcribe, ask_hal, synthesize)
                        except Exception:
                            log.exception("Failed handling Telegram message")
                            await client.post(f"{API}/sendMessage", json={
                                "chat_id": upd["message"]["chat"]["id"],
                                "text": "I'm sorry, something went wrong."})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(5)
