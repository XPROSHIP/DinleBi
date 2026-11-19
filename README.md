# DinleBi – Render (öncelikli) + Fly.io

Bu klasör `Dinlebi Sesli Kitaplar` uygulamasının **internet sürümü**dür.
HTML + Python sunucu aynı origin'den servis edilir; şifreli HLS çözme ve
ses dosyası üretimi sunucuda yapılır (tarayıcı CORS/DRM engeline takılmaz).

## ✅ Canlı adresler

| Platform | URL | Durum |
|---|---|---|
| **Render** | **https://dinlebi.onrender.com** | ✅ aktif, öncelikli |
| GitHub | https://github.com/XPROSHIP/DinleBi | ✅ kaynak repo |
| Fly.io | https://dinlebi.fly.dev | ⏸ trial/5 dk limiti (kullanılmıyor) |

Render servisi: **DinleBi** · plan **free** · region **frankfurt** · runtime docker
Dashboard: https://dashboard.render.com/web/srv-dbvm7bm1egvs73anht5g

## Dosyalar

| Dosya | Görev |
|---|---|
| `index.html` | Uygulama (2.051 kitap gömülü) |
| `server.py` | Bulut sunucusu (HOST=0.0.0.0, `$PORT`, /tmp çalışma dizinleri) |
| `dinlebi-katalog.json` | Katalog verisi |
| `requirements.txt` | `av>=12,<14`, `pycryptodome>=3.20` |
| `Dockerfile` | python:3.11-slim-bookworm (PyAV tekerleği libmp3lame + ffmpeg içerir) |
| `render.yaml` | Render Blueprint (opsiyonel) |
| `.gitignore` | token notları + mp3/cache hariç |

> `_render ve github api'lerim.txt` gibi kişisel dosyalar **.gitignore**'da,
> GitHub'a gitmez (secret scanning engelliyor).

## İndirme biçimleri

| Biçim | Süre (7 dk kitap) | Not |
|---|---|---|
| **M4A** (varsayılan) | **~20 sn** | AAC akışı yeniden kodlanmadan .m4a kabına taşınır — kalite kaybı yok |
| MP3 (128 kbps) | ~4 dk (yerel) / çok daha uzun (Render 0.1 CPU) | Yeniden kodlama gerekir |

Uzun kitaplar için **M4A** önerilir. Ayarlar → *Ses İndirme* → M4A / MP3.

## İndirme akışı (arayüz)

1. Kartın sağ üstündeki **yuvarlak ↓** düğmesi (veya oynatıcıdaki ↓).
2. Dosya hazırsa indirme anında başlar.
3. Hazır değilse ekranın altında **sabit ilerleme çubuğu** açılır:
   - `Sıraya alındı` → `İndiriliyor %N` → `Dosya hazırlanıyor %N` → `İndirme başladı ✓`
   - Yüzde **asla geri gitmez**, toast yağmuru yok (tek başlangıç bildirimi).
   - ✕ ile gizlenir; sunucudaki üretim devam eder, dosya hazır olunca iner.
4. Sunucu yeniden başlasa bile indirilen segmentler diskte kaldığı için
   **kaldığı yerden devam eder** (`resumed: true`).

## Uçlar

| Uç | Açıklama |
|---|---|
| `GET /` | Uygulama |
| `GET /health` | `{"ok":true,"aes":"pycryptodome","books":2051}` |
| `GET /api/appdata` | Katalog (uygulama verisi) |
| `GET /api/progress/<akey>?f=mp3\|m4a` | İş durumu: `state`, `progress`, `enc`, `done`, `total` |
| `GET /hls/<akey>/index.m3u8` | Çözülmüş HLS (hls.js oynatır) |
| `GET /hls/<akey>/seg/N.ts?q=hq` | AES-128 çözülmüş segment |
| `GET /dl/<bookId>?q=hq&f=mp3\|m4a` | Dosya (hazırsa 200 + Range desteği, değilse 202) |

## Yeniden deploy

```bash
cd WebPages-Render
git add -A && git commit -m "..." && git push
# Render autoDeploy=yes → otomatik build
```

Manuel tetikleme (API):
```bash
curl -X POST -H "Authorization: Bearer $RENDER_API_KEY" \
  https://api.render.com/v1/services/srv-dbvm7bm1egvs73anht5g/deploys \
  -H "Content-Type: application/json" -d '{}'
```

## Bilinen sınırlar (Render free)

- **0.1 CPU** → MP3 dönüştürme çok yavaş; M4A kullan.
- Disk **geçici** (`/tmp`): yeni deploy/restart sonrası üretilmiş dosyalar silinir.
  (Render'da kalıcı disk ücretli.)
- 15 dk trafik yoksa servis uyur; ilk istek ~30-50 sn sürebilir.
- Aylık 750 instance-saat.

## Yerel kullanım (aynı uygulama, internetsiz)

`..\Dinlebi Baslat.bat` → http://127.0.0.1:8765/
