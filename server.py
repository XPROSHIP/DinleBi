#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DinleBi indirici + yerel oynatıcı sunucusu
==========================================
DinleBi sesli kitaplarını HLS'ten (AES-128) çözüp MP3'e çevirir ve
"Dinlebi Sesli Kitaplar.html" uygulamasına yerel bir sunucu üzerinden
hem oynatma (HLS) hem de tek tık MP3 indirme sağlar.

Hızlı başlangıç
---------------
    pip install av
    python dinlebi-sunucu.py                # sunucu + tarayıcıyı açar
    python dinlebi-sunucu.py --id 5f1ccb7d7a80e73c094bfb3b
    python dinlebi-sunucu.py --all --out "indirilenler" --limit 10
    python dinlebi-sunucu.py --list         # katalogdaki ilk 20 kitap

Sunucu adresi: http://127.0.0.1:8765
"""

import argparse
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# Ayarlar
# ----------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.environ.get("CATALOG_FILE") or os.path.join(APP_DIR, "dinlebi-katalog.json")
HTML_FILE = os.environ.get("HTML_FILE") or os.path.join(APP_DIR, "index.html")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR") or "/tmp/dinlebi/indirilenler"
HLS_CACHE = os.environ.get("CACHE_DIR") or "/tmp/dinlebi/cache/hls"

S3 = "https://dinlebi-app.s3.eu-central-1.amazonaws.com/audibles-0"
AES_KEY = b"c8a0922b6e3ca7e7"                              # tüm kitaplarda aynı
AES_IV = bytes.fromhex("c8a0922b6e3ca7e7f70efee54cdfd80b")  # m3u8 EXT-X-KEY IV
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

DEFAULT_PORT = int(os.environ.get("PORT") or 8765)
MP3_BITRATE = 128000
MP3_RATE = 44100

# ----------------------------------------------------------------------------
# AES-128-CBC çözücü (pycryptodome varsa onu kullanır, yoksa saf Python)
# ----------------------------------------------------------------------------
def _ensure_deps():
    """Gerekli paketleri (av, pycryptodome) yoksa kurar."""
    missing = []
    try:
        import av  # noqa: F401
    except Exception:
        missing.append("av")
    try:
        from Crypto.Cipher import AES  # noqa: F401
    except Exception:
        missing.append("pycryptodome")
    if not missing:
        return
    print("Eksik paketler kuruluyor: " + ", ".join(missing), flush=True)
    import subprocess
    for pkg in missing:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pkg])



from Crypto.Cipher import AES as _AES  # noqa: E402

AES_BACKEND = "pycryptodome"


def aes_decrypt(data: bytes) -> bytes:
    """DinleBi HLS segmentlerini AES-128-CBC ile çözer (anahtar/IV tüm kitaplarda sabit)."""
    return _AES.new(AES_KEY, _AES.MODE_CBC, AES_IV).decrypt(data)


# ----------------------------------------------------------------------------
# Küçük yardımcılar
# ----------------------------------------------------------------------------
_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def slugify(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or "kitap"


def human_secs(sec) -> str:
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def http_get(url: str, timeout=45, tries=3) -> bytes:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # ağ hatası → tekrar
            last = e
            # kurumsal proxy / antivirüs araya giriyorsa sertifika doğrulamasını atla
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                try:
                    import ssl
                    ctx = ssl._create_unverified_context()
                    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
                    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                        return r.read()
                except Exception as e2:
                    last = e2
            time.sleep(0.5 * (i + 1))
    raise last


def http_get_head(url: str, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception:
        return 0


# ----------------------------------------------------------------------------
# Katalog
# ----------------------------------------------------------------------------
_CATALOG = None


def load_catalog() -> dict:
    global _CATALOG
    if _CATALOG is None:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            _CATALOG = json.load(f)
    return _CATALOG


def book_by_key(key: str):
    for b in load_catalog()["books"]:
        if b["id"] == key:
            return b
    return None


def audio_key_of(book) -> str:
    """Kitabın S3 klasör anahtarı (ör. 1cbb09ba08ddf13a)."""
    url = book.get("audio_url") or ""
    m = re.search(r"/audibles-0/([0-9a-f]+)(?:_lq)?/multi\.m3u8", url)
    return m.group(1) if m else ""


# ----------------------------------------------------------------------------
# HLS çözümleme
# ----------------------------------------------------------------------------
_playlist_cache = {}
_playlist_lock = threading.Lock()

SEG_RE = re.compile(r"/audibles-0/([0-9a-f]+)(?:_lq)?/(audible\d+\.ts)$")


def get_playlist(akey: str, quality: str = "hq"):
    """[(seg_url, duration), ...] döner."""
    ck = (akey, quality)
    with _playlist_lock:
        if ck in _playlist_cache:
            return _playlist_cache[ck]

    folder = akey if quality == "hq" else akey + "_lq"
    base = f"{S3}/{folder}/"
    master = http_get(base + "multi.m3u8").decode("utf-8", "ignore")
    names = [l.strip() for l in master.splitlines() if l.strip() and not l.startswith("#")]
    variant = names[0] if names else "audible.m3u8"
    txt = http_get(base + variant).decode("utf-8", "ignore")

    segs, durs = [], []
    cur = 0.0
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                cur = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                cur = 0.0
        elif line and not line.startswith("#"):
            segs.append((base + line, cur))
            cur = 0.0

    with _playlist_lock:
        _playlist_cache[ck] = segs
    return segs


def get_segment(akey: str, quality: str, idx: int) -> bytes:
    """Segmenti indirir, AES-128 CBC ile çözer ve döndürür (disk önbellekli)."""
    segs = get_playlist(akey, quality)
    if idx < 0 or idx >= len(segs):
        raise IndexError(idx)
    seg_url = segs[idx][0]
    name = seg_url.rsplit("/", 1)[-1]
    cache_dir = os.path.join(HLS_CACHE, akey + ("" if quality == "hq" else "_lq"))
    cache_path = os.path.join(cache_dir, name)
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()
    raw = http_get(seg_url)
    if raw[:1] == b"\x47":          # zaten açık (şifresiz) ise dokunma
        plain = raw
    else:
        plain = aes_decrypt(raw)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(plain)
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return plain


# ----------------------------------------------------------------------------
# MP3 üretimi
# ----------------------------------------------------------------------------
_jobs = {}
_jobs_lock = threading.Lock()
_JOBS_FILE = os.path.join(HLS_CACHE, "..", "jobs.json")


def _jobs_load():
    try:
        with open(_JOBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _jobs.update(data)
    except Exception:
        pass
    # Önceki süreçten kalan yarım işler ölüdür: yeniden başlatılacak
    for v in _jobs.values():
        if isinstance(v, dict) and v.get("state") in ("download", "encode", "queued"):
            v["state"] = "interrupted"


def _jobs_save():
    try:
        os.makedirs(os.path.dirname(_JOBS_FILE), exist_ok=True)
        tmp = _JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_jobs, f, ensure_ascii=False)
        os.replace(tmp, _JOBS_FILE)
    except Exception:
        pass


_jobs_load()


def job_state(akey):
    with _jobs_lock:
        return dict(_jobs.get(akey) or {})


def _set_job(akey, **kw):
    with _jobs_lock:
        st = _jobs.setdefault(akey, {})
        st.update(kw)
        st["ts"] = time.time()
        _jobs_save()


def out_path_for(book, fmt="mp3") -> str:
    akey = audio_key_of(book)
    ext = "m4a" if fmt == "m4a" else "mp3"
    name = f"{slugify(book.get('author') or 'Bilinmeyen Yazar')} - {slugify(book.get('title') or akey)}.{ext}"
    return os.path.join(DOWNLOAD_DIR, name)


def mp3_path_for(book) -> str:
    akey = audio_key_of(book)
    return out_path_for(book, "mp3"), akey


def build_mp3(book, quality="hq", threads=12, cover=True, fmt="mp3") -> str:
    """Kitabı indirip MP3 (veya kopya ile M4A) üretir. Hazır dosya yolunu döndürür."""
    akey = audio_key_of(book)
    if not akey:
        raise ValueError("Ses anahtarı bulunamadı")
    out_path = out_path_for(book, fmt)
    job_key = akey + ("" if fmt == "mp3" else ":m4a")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        _set_job(job_key, state="done", progress=1.0, path=out_path, fmt=fmt)
        return out_path

    segs = get_playlist(akey, quality)
    total = len(segs)
    tmp_ts = out_path + ".part.ts"
    lock = threading.Lock()

    cache_dir = os.path.join(HLS_CACHE, akey if quality == "hq" else akey + "_lq")
    cached = 0
    if os.path.isdir(cache_dir):
        try:
            cached = min(total, sum(1 for n in os.listdir(cache_dir) if n.endswith(".ts")))
        except OSError:
            cached = 0
    done = [cached]
    _set_job(job_key, state="download", progress=round(cached / total * 0.85, 4),
             total=total, done=cached, path=out_path, resumed=cached > 0, fmt=fmt)

    with ThreadPoolExecutor(max_workers=threads) as ex, open(tmp_ts, "wb") as fh:
        pending = {}
        nxt_write = 0
        nxt_submit = 0

        def submit_until(limit):
            nonlocal nxt_submit
            while nxt_submit < total and nxt_submit < limit:
                pending[nxt_submit] = ex.submit(_fetch_seg, akey, quality, nxt_submit)
                nxt_submit += 1

        submit_until(threads * 3)
        while nxt_write < total:
            fut = pending.pop(nxt_write)
            fh.write(fut.result())
            with lock:
                done[0] += 1
                if done[0] % 5 == 0 or done[0] == total:
                    _set_job(job_key, progress=round(done[0] / total * 0.85, 4), done=done[0])
            nxt_write += 1
            submit_until(nxt_write + threads * 3)

    if fmt == "m4a":
        _set_job(job_key, state="encode", progress=0.88)
        _transcode_aac_copy(tmp_ts, out_path)
    else:
        _set_job(job_key, state="encode", progress=0.86)
        total_sec = sum((d or 0) for _u, d in segs) or 0

        def enc_prog(frac):
            _set_job(job_key, progress=round(0.86 + max(0.0, min(1.0, frac)) * 0.13, 4))

        _transcode(tmp_ts, out_path, book, total_sec, enc_prog)

    try:
        os.remove(tmp_ts)
    except OSError:
        pass
    _set_job(job_key, state="done", progress=1.0, size=os.path.getsize(out_path), fmt=fmt)
    return out_path




def _fetch_seg(akey, quality, idx):
    return get_segment(akey, quality, idx)


def _transcode_aac_copy(src_ts, out_m4a):
    """AAC akışını yeniden kodlamadan .m4a kabına taşır (çok hızlı)."""
    import av

    tmp = out_m4a + ".tmp.m4a"
    with av.open(src_ts) as inp:
        astream = next(s for s in inp.streams if s.type == "audio")
        with av.open(tmp, "w", format="mp4") as out:
            ostream = out.add_stream_from_template(astream)
            for pkt in inp.demux(astream):
                if pkt.dts is None:
                    continue
                pkt.stream = ostream
                out.mux(pkt)
    os.replace(tmp, out_m4a)


def _transcode(src_ts: str, out_mp3: str, book, total_sec=0, on_progress=None):
    import av  # yerel import: sunucu av olmadan da açılabilsin

    tmp_out = out_mp3 + ".tmp.mp3"
    with av.open(src_ts) as inp:
        astream = next(s for s in inp.streams if s.type == "audio")
        with av.open(tmp_out, "w", format="mp3", options={"write_id3v1": "1"}) as out:
            out.metadata.update({
                "title": book.get("title") or "",
                "artist": book.get("author") or "",
                "album": book.get("genre") or "Sesli Kitap",
                "album_artist": book.get("author") or "",
                "comment": "DinleBi · " + (book.get("narrator") or ""),
                "genre": book.get("genre") or "Audiobook",
                "date": (book.get("date_added") or "")[:4],
                "encoded_by": "DinleBi Downloader",
            })
            ostream = out.add_stream("libmp3lame", rate=MP3_RATE)
            ostream.bit_rate = MP3_BITRATE
            ostream.layout = "stereo"
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=MP3_RATE)
            done_sec = 0.0
            tick = 0.0
            for frame in inp.decode(astream):
                if total_sec:
                    done_sec += frame.samples / float(frame.sample_rate or 44100)
                    if on_progress and done_sec - tick >= 5.0:
                        tick = done_sec
                        on_progress(done_sec / total_sec)
                for rf in resampler.resample(frame):
                    for pkt in ostream.encode(rf):
                        out.mux(pkt)
            for pkt in ostream.encode(None):
                out.mux(pkt)
    os.replace(tmp_out, out_mp3)


def _add_cover(out_container, cover_url):
    import av
    raw = http_get(cover_url, timeout=20)
    with av.open(io.BytesIO(raw), format="image2") as im:
        frame = next(im.decode(video=0))
        frame = frame.reformat(format="yuvj420p")
        st = out_container.add_stream("mjpeg", rate=1)
        try:
            st.disposition = av.stream.Disposition.attached_pic
        except Exception:
            pass
        st.width, st.height = frame.width, frame.height
        for pkt in st.encode(frame):
            out_container.mux(pkt)


def ensure_mp3_async(book, quality="hq", fmt="mp3"):
    """Arka planda MP3/M4A üretimini başlatır (tek sefer)."""
    akey = audio_key_of(book)
    job_key = akey + ("" if fmt == "mp3" else ":m4a")
    st = job_state(job_key)
    if st.get("state") in ("download", "encode", "queued"):
        return st
    out_path = out_path_for(book, fmt)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        _set_job(job_key, state="done", progress=1.0, path=out_path, fmt=fmt)
        return job_state(job_key)

    def run():
        try:
            build_mp3(book, quality, fmt=fmt)
        except Exception as e:
            _set_job(job_key, state="error", error=str(e))

    _set_job(job_key, state="queued", progress=0.0, fmt=fmt)
    threading.Thread(target=run, daemon=True).start()
    return job_state(job_key)


# ----------------------------------------------------------------------------
# HTML uygulaması için veri
# ----------------------------------------------------------------------------
def build_app_data() -> dict:
    cat = load_catalog()
    books = []
    for i, b in enumerate(cat["books"]):
        akey = audio_key_of(b)
        chapters = b.get("chapters") or []
        books.append({
            "id": i + 1,
            "kyId": b["id"],                 # DinleBi içerik id'si
            "akey": akey,
            "title": b["title"],
            "author": b.get("author") or "",
            "narrator": b.get("narrator") or "",
            "genres": list(dict.fromkeys([g for g in [b.get("genre")] + (b.get("tags") or []) if g]))[:6],
            "category": b.get("genre") or "",
            "publisher": b.get("publisher") or "",
            "year": (b.get("date_added") or "")[:4],
            "image": b.get("cover_url") or "",
            "duration": human_secs(b.get("duration_seconds")),
            "durationSeconds": int(b.get("duration_seconds") or 0),
            "partCount": 1,
            "chapters": [
                {"t": c.get("title") or "", "s": int(c.get("start_seconds") or 0),
                 "d": int(c.get("duration_seconds") or 0)}
                for c in chapters
            ],
            "parts": [{"title": "Ses", "durationSeconds": int(b.get("duration_seconds") or 0)}],
        })
    return {
        "meta": {
            "title": "DinleBi Sesli Kitaplar",
            "source": "dinlebi",
            "language": "tr",
            "total": len(books),
            "builtAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "note": "Ses linkleri kalıcıdır; indirme ve oynatma yerel sunucu üzerinden yapılır.",
        },
        "books": books,
    }


# ----------------------------------------------------------------------------
# HTTP sunucusu
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "DinlebiLocal/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- yardımcılar ---------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, txt, ctype="text/plain; charset=utf-8", code=200):
        raw = txt.encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # -- yönlendirme ---------------------------------------------------------
    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html", "/app"):
                return self._serve_html(head_only)
            if path == "/health":
                return self._json({"ok": True, "version": 1, "aes": AES_BACKEND,
                                   "books": len(load_catalog()["books"])})
            if path == "/api/catalog":
                return self._json(load_catalog())
            if path == "/api/appdata":
                return self._json(build_app_data())
            if path.startswith("/api/progress/"):
                ak = path.rsplit("/", 1)[-1]
                pf = "m4a" if (qs.get("f") or ["mp3"])[0] in ("m4a", "aac") else "mp3"
                return self._json(job_state(ak + ("" if pf == "mp3" else ":m4a")))

            m = re.match(r"^/hls/([0-9a-f]+)/(index\.m3u8|master\.m3u8)$", path)
            if m:
                return self._serve_playlist(m.group(1), qs)

            m = re.match(r"^/hls/([0-9a-f]+)/seg/(\d+)\.ts$", path)
            if m:
                return self._serve_segment(m.group(1), int(m.group(2)), head_only)

            m = re.match(r"^/(?:dl|stream|indir)/([A-Za-z0-9]+)$", path)
            if m:
                return self._serve_download(m.group(1), qs, head_only, inline=False)

            m = re.match(r"^/oynat/([A-Za-z0-9]+)$", path)
            if m:
                return self._serve_download(m.group(1), qs, head_only, inline=True)

            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            return self._text("Bulunamadı", code=404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, code=500)
            except Exception:
                pass

    # -- uçlar ---------------------------------------------------------------
    def _serve_html(self, head_only=False):
        try:
            with open(HTML_FILE, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return self._text("HTML dosyası bulunamadı: " + HTML_FILE, code=404)
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if not head_only:
            self.wfile.write(raw)

    def _serve_playlist(self, akey, qs):
        quality = (qs.get("q") or ["hq"])[0]
        segs = get_playlist(akey, quality)
        target = 11
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
        for i, (_url, dur) in enumerate(segs):
            lines.append(f"#EXTINF:{dur:.6f},")
            lines.append(f"seg/{i}.ts?q={quality}")
        lines.append("#EXT-X-ENDLIST")
        self._text("\n".join(lines), ctype="application/vnd.apple.mpegurl")

    def _serve_segment(self, akey, idx, head_only):
        quality = "hq"
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        quality = (qs.get("q") or ["hq"])[0]
        data = get_segment(akey, quality, idx)
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _serve_download(self, key, qs, head_only, inline=False):
        """MP3/M4A hazır olduğunda sunar; hazır değilse 202 + durum döner."""
        book = book_by_key(key)
        if not book:
            return self._json({"error": "kitap bulunamadı"}, code=404)
        quality = "lq" if (qs.get("q") or ["hq"])[0] in ("lq", "low") else "hq"
        fmt = "m4a" if (qs.get("f") or ["mp3"])[0] in ("m4a", "aac", "mp4") else "mp3"
        akey = audio_key_of(book)
        job_key = akey + ("" if fmt == "mp3" else ":m4a")
        out_path = out_path_for(book, fmt)
        ready = os.path.exists(out_path) and os.path.getsize(out_path) > 1024

        if not ready:
            st = job_state(job_key)
            if st.get("state") not in ("download", "encode", "queued"):
                ensure_mp3_async(book, quality, fmt)
                st = job_state(job_key)
            if head_only:
                self.send_response(202)
                self._cors()
                self.end_headers()
                return
            return self._json({"status": "preparing", "state": st.get("state", "queued"),
                               "progress": st.get("progress", 0), "fmt": fmt, "akey": akey}, code=202)

        size = os.path.getsize(out_path)
        fname = os.path.basename(out_path)
        ctype = "audio/mp4" if fmt == "m4a" else "audio/mpeg"

        # Range desteği (oynatıcı / devam ettirilebilir indirme)
        start_b, end_b = 0, size - 1
        code = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start_b = int(m.group(1))
                if m.group(2):
                    end_b = min(int(m.group(2)), size - 1)
                code = 206

        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end_b - start_b + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start_b}-{end_b}/{size}")
        self.send_header("Content-Disposition",
                         ("inline" if inline else "attachment") + "; filename*=UTF-8''" +
                         urllib.parse.quote(fname))
        self.end_headers()
        if head_only:
            return
        with open(out_path, "rb") as f:
            f.seek(start_b)
            remaining = end_b - start_b + 1
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_list(limit=20):
    cat = load_catalog()
    for b in cat["books"][:limit]:
        print(f"{b['id']}  {human_secs(b['duration_seconds']):>9}  {b['author'][:28]:28s}  {b['title'][:48]}")


def cmd_download(ids, out_dir, quality, workers):
    cat = load_catalog()
    by_id = {b["id"]: b for b in cat["books"]}
    if out_dir:
        global DOWNLOAD_DIR
        DOWNLOAD_DIR = os.path.abspath(out_dir)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    todo = [by_id[i] for i in ids if i in by_id]
    log(f"{len(todo)} kitap indirilecek → {DOWNLOAD_DIR}")

    def one(b):
        try:
            t0 = time.time()
            p = build_mp3(b, quality)
            mb = os.path.getsize(p) / 1048576
            log(f"✓ {b['title'][:50]:50s} {mb:7.1f} MB  {time.time()-t0:6.1f}s")
        except Exception as e:
            log(f"✗ {b['title'][:50]:50s} HATA: {e}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))


def find_free_port(start=DEFAULT_PORT, tries=20):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def run_server(port, open_browser=True):
    host = os.environ.get("HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    url = f"http://{'127.0.0.1' if host == '127.0.0.1' else host}:{port}/"
    log("=" * 62)
    log("  DinleBi yerel sunucu çalışıyor")
    log(f"  Uygulama : {url}")
    log(f"  AES      : {AES_BACKEND}")
    log(f"  Katalog  : {len(load_catalog()['books'])} kitap")
    log(f"  İndirmeler: {DOWNLOAD_DIR}")
    log("  Kapatmak için Ctrl+C")
    log("=" * 62)
    if open_browser and os.environ.get("OPEN_BROWSER") == "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("\nKapatılıyor…")
    finally:
        srv.server_close()


def main():
    ap = argparse.ArgumentParser(description="DinleBi MP3 indirici ve yerel oynatıcı")
    ap.add_argument("--id", action="append", default=[], help="İndirilecek DinleBi kitap id'si (tekrarlanabilir)")
    ap.add_argument("--all", action="store_true", help="Katalogdaki tüm kitapları indir")
    ap.add_argument("--limit", type=int, default=0, help="--all ile ilk N kitap")
    ap.add_argument("--out", default="", help="İndirme klasörü")
    ap.add_argument("--q", default="hq", choices=["hq", "lq"], help="Ses kalitesi")
    ap.add_argument("--workers", type=int, default=2, help="Aynı anda işlenecek kitap sayısı")
    ap.add_argument("--list", action="store_true", help="Katalogdaki kitapları listele")
    ap.add_argument("--serve", action="store_true", help="Sadece sunucuyu çalıştır")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.list:
        cmd_list(30)
        return

    if args.id or args.all:
        cat = load_catalog()
        ids = args.id or [b["id"] for b in cat["books"]]
        if args.limit:
            ids = ids[: args.limit]
        cmd_download(ids, args.out, args.q, args.workers)
        return

    port = int(os.environ.get("PORT") or args.port)
    if os.environ.get("PORT"):
        run_server(port, False)
    else:
        run_server(find_free_port(port), not args.no_browser)


if __name__ == "__main__":
    main()
