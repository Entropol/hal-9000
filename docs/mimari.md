# Mimari

```
[Buton] → [ESP32: mikrofon → SD karta WAV] → HTTPS → [n8n webhook]
                                                        │
                        ┌───────────────────────────────┘
                        ▼
              Ses → yazı (STT) → Claude (HAL kişiliği) → ElevenLabs TTS (wav_16000)
                        │
                        ▼
[ESP32: cevabı SD karta indir → IO26 DAC → hoparlör, göz sesle titreşir]
```

## Neden SD kart?

Kartın modülünde PSRAM yok, sadece 520 KB RAM var. WiFi ve HTTPS bunun önemli kısmını
kullanıyor. 16 kHz, 16-bit, 10 saniyelik bir kayıt yaklaşık 320 KB tutuyor. Bu yüzden
SD kart ses tamponu olarak kullanılıyor:

- Kayıt SD karta yazılır, sonra parça parça n8n'e gönderilir.
- Cevap SD karta parça parça indirilir, sonra çalınır.
- Açılış sesi gibi sabit efektler de `/hal` klasöründe durur.

Aynı anda tek bir HTTPS bağlantısı açık olur.

## Neden n8n?

- API anahtarları (STT, Claude, ElevenLabs) cihazda değil, n8n'de durur.
- HAL'ın kişiliği ve sesi kart yazılımına dokunmadan değiştirilebilir.
- ESP32 sadece kaydeder, gönderir, indirir ve çalar.

## Durumlar ve göz

| Durum | Göz (arka ışık) |
| --- | --- |
| Kapalı | Söner |
| Dinliyor | Yavaşça nefes alır |
| Düşünüyor (n8n cevabı bekleniyor) | Sabit, orta parlaklık |
| Konuşuyor | Sesin şiddetine göre titreşir |

Sırayla dinleyip sırayla konuşur. HAL konuşurken mikrofon kaydı durur, böylece kendi
sesini duymaz.

## Ses

HAL'ın sesi ElevenLabs'te Voice Design ile sıfırdan tasarlanacak ya da kendi sesimiz
klonlanacak. Filmdeki sesin klonlanması ElevenLabs'in kullanım politikasına aykırı
olduğu için bu yol kullanılmıyor.

Voice Design için başlangıç tarifi:

> A calm, soft-spoken middle-aged male voice with a gentle mid-Atlantic accent.
> Speaks slowly and precisely, with perfect diction and almost no emotional
> variation. Warm but distant, unfailingly polite, quietly confident. Low-pitched,
> smooth, close to the microphone.

ElevenLabs ayarlarında "stability" yüksek tutulacak. Çıkış formatı `wav_16000`.
