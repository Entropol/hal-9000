# Firmware

ESP32 yazılımı, Arduino IDE projesi olarak burada olacak.

## Plan

**1. aşama** (mikrofon gelmeden test edilebilir)
- Göz görüntüsünün ekrana çizilmesi
- Arka ışık ile nefes alma ve titreşim efektleri
- Buton ile dinleme modunu açma/kapama
- SD karttan WAV çalma, çalarken gözün sesle titremesi
- Ses seviyesinin %70 ile sınırlanması

**2. aşama**
- INMP441'den SD karta kayıt (I2S1)
- Sessizlik algılama
- WiFi kurulumu
- n8n webhook'una gönderme ve cevabı indirme

## Arduino IDE

- Kart: "ESP32 Dev Module"
- TFT_eSPI ayarları için `docs/donanim.md` dosyasındaki pin tablosuna bak.
- WiFi ve webhook bilgileri `secrets.h` dosyasında tutulacak. Bu dosya `.gitignore` ile
  repodan hariç tutuluyor.
