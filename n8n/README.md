# n8n akışı

Akış dışa aktarılınca (JSON) buraya konacak.

## Plan

1. **Webhook (POST):** ESP32'den WAV dosyasını alır.
2. **Ses → yazı:** Konuşmayı metne çevirir.
3. **Claude:** HAL kişiliğiyle kısa, kibar ve sakin bir cevap üretir.
4. **ElevenLabs TTS:** Cevabı HAL sesiyle `wav_16000` formatında seslendirir.
5. **Webhook yanıtı:** WAV dosyasını ESP32'ye geri döndürür.

API anahtarları n8n'in kimlik bilgileri (credentials) bölümünde tutulur,
dışa aktarılan JSON'a girmez.
