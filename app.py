"""
Flask-бэкенд загрузчика видео с allatra.tv
"""

import json
import os
import queue
import re
import html as html_mod
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from flask import Flask, render_template, request, Response, jsonify, stream_with_context


app = Flask(__name__)

from local_access import protect_flask
protect_flask(app)



# ─── Модели ──────────────────────────────────────────────────────────────────
@dataclass
class VideoSource:
    name: str
    url: str
    kind: str = "ytdlp"           # "ytdlp" | "allatra_direct"
    mp4_urls: dict = field(default_factory=dict)


# ─── Парсинг ─────────────────────────────────────────────────────────────────
def _get(url: str, referer: str = "") -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_allatra_mp4_urls(embed_url: str) -> dict[str, str]:
    html = _get(embed_url, referer="https://allatra.tv/")
    # storage.allatra.video (старый CDN) и api.allatra.video/storage (новый CDN)
    urls = list(dict.fromkeys(re.findall(
        r'https://(?:storage\d*\.allatra\.video|api\.allatra\.video/storage)/[^\s"\'<>]+\.mp4',
        html)))
    result = {}
    for u in urls:
        m = re.search(r'_(\d+p)\.mp4', u)
        if m:
            result[m.group(1)] = u
    return result


_DIRECT_URL_PATTERNS = [
    (r'(?:youtube\.com/watch|youtu\.be/)',  "YouTube"),
    (r'odysee\.com/',                       "Odysee"),
    (r'rumble\.com/',                       "Rumble"),
    (r'facebook\.com/',                     "Facebook"),
    (r'fb\.watch/',                         "Facebook"),
    (r'vimeo\.com/',                        "Vimeo"),
]


def _as_direct_source(url: str) -> VideoSource | None:
    """Если URL — прямая ссылка на видеоплатформу (не allatra.tv), возвращает VideoSource сразу."""
    for pattern, name in _DIRECT_URL_PATTERNS:
        if re.search(pattern, url, re.I):
            return VideoSource(name=name, url=url, kind="ytdlp")
    return None


def embed_to_ytdlp_url(embed_url: str) -> str | None:
    """Конвертирует embed-ссылку в URL для yt-dlp. Не вызывается для allatra.video/embed."""
    if "odysee.com/$/embed/" in embed_url:
        return embed_url.split("?")[0]  # убираем referral-параметры, LBRY extractor
    if "rumble.com/embed/" in embed_url:
        return embed_url
    if "facebook.com" in embed_url or "fb.watch" in embed_url:
        return embed_url
    if embed_url.startswith("http") and "embed" not in embed_url:
        return embed_url
    return None


def _sorted_ytdlp_sources(raw_sources: list[VideoSource]) -> list[VideoSource]:
    """ytdlp-источники в порядке приоритета: Odysee → YouTube → Rumble → остальные."""
    _priority = {"odysee": 0, "youtube": 1, "rumble": 2}
    return sorted(
        [s for s in raw_sources if s.kind == "ytdlp"],
        key=lambda s: next((v for k, v in _priority.items() if k in s.url.lower()), 3),
    )


def fetch_video_sources(page_url: str) -> list[VideoSource]:
    # Прямые ссылки на видеоплатформы — парсить allatra.tv не нужно
    direct = _as_direct_source(page_url)
    if direct:
        return [direct]

    src = _get(page_url)
    sources: list[VideoSource] = []

    m = re.search(r':players="(\[.*?\])"', src, re.DOTALL)
    if m:
        try:
            players = json.loads(html_mod.unescape(m.group(1)))
            for p in players:
                name = p.get("name", "Источник")
                embed = p.get("videoUrl", "")
                if "allatra.video/embed" in embed:
                    sources.append(VideoSource(name=name, url=embed, kind="allatra_direct"))
                    # Если у allatra.video есть ytv-параметр — добавляем YouTube как резерв
                    yt_m = re.search(r'allatra\.video/embed\?ytv=([A-Za-z0-9_-]{8,15})', embed)
                    if yt_m:
                        yt_url = f"https://www.youtube.com/watch?v={yt_m.group(1)}"
                        sources.append(VideoSource(name=f"{name} (YouTube)", url=yt_url, kind="ytdlp"))
                else:
                    dl_url = embed_to_ytdlp_url(embed)
                    if dl_url:
                        sources.append(VideoSource(name=name, url=dl_url, kind="ytdlp"))
        except (json.JSONDecodeError, KeyError):
            pass

    if not sources:
        yt = re.search(r'youtube\.com(?:/embed/|/v/)([A-Za-z0-9_-]{8,15})', src)
        if yt:
            sources.append(VideoSource("YouTube", f"https://www.youtube.com/watch?v={yt.group(1)}"))
        od = re.search(r'odysee\.com/\$/embed/([^"\'&]+)', src)
        if od:
            sources.append(VideoSource("Odysee", f"https://odysee.com/{od.group(1).split('?')[0]}"))

    if not sources:
        raise ValueError("Не удалось найти видео на этой странице")
    return sources


def slug_from_page_url(page_url: str) -> str:
    """Извлекает slug из allatra.tv URL. /video/stat-luchshei-versiei-sebia → stat-luchshei-versiei-sebia"""
    m = re.search(r'/(?:video|category)/([^/?#]+)', page_url)
    if m:
        return re.sub(r'[\\/*?:"<>|]', '_', m.group(1))
    return ""


def parse_progress(line: str) -> float | None:
    m = re.search(r'\[download\]\s+([\d.]+)%', line)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def ytdlp_mp4_format(height=None):
    height_filter = f"[height<={height}]" if height else ""
    return (
        f"bestvideo[ext=mp4][vcodec^=avc1]{height_filter}+bestaudio[ext=m4a]/"
        f"bestvideo[ext=mp4]{height_filter}+bestaudio[ext=m4a]/"
        f"bestvideo{height_filter}+bestaudio/"
        f"best[ext=mp4]{height_filter}/"
        f"best{height_filter}/best"
    )


def sse_response(q: queue.Queue) -> Response:
    """Оборачивает Queue в SSE-поток."""
    @stream_with_context
    def generate():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(generate(),
                    content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def make_bulk_sender(idx: int, root_send):
    """Фабрика callback-функции для одного элемента очереди bulk-загрузки."""
    def sender(text=None, pct=None, label=None, done=False, dest=None, **_):
        if done:
            root_send(idx, type="item-done", dest=dest or "")
        elif pct is not None:
            root_send(idx, type="progress", pct=pct, label=label or "")
            if text:
                root_send(idx, type="log", text=text)
        elif text:
            root_send(idx, type="log", text=text)
    return sender


# ─── Загрузка прямым URL ──────────────────────────────────────────────────────
def download_direct(mp4_url: str, folder: str, send, filename_override: str = ""):
    if filename_override:
        filename = filename_override
    else:
        filename = re.sub(r'[\\/*?:"<>|]', "_", os.path.basename(mp4_url.split("?")[0]))
    dest = os.path.join(folder, filename)

    send(text=f"  MP4: {mp4_url}", info=True)
    send(text=f"  Сохраняю: {dest}")

    req = urllib.request.Request(mp4_url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://allatra.video/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        chunk = 256 * 1024
        last_logged_pct = -10

        with open(dest, "wb") as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    pct = done / total * 100
                    mb_done = done / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    send(pct=pct, label=f"{mb_done:.1f} / {mb_total:.1f} МБ")
                    if pct - last_logged_pct >= 10:
                        last_logged_pct = pct
                        send(text=f"[download] {pct:5.1f}%  {mb_done:.1f} / {mb_total:.1f} МБ")
    send(done=True, dest=dest)


# ─── Flask маршруты ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/default-folder")
def default_folder():
    return jsonify({"folder": os.path.expanduser("~\\Downloads").replace("\\", "/")})


@app.route("/api/browse-folder")
def browse_folder():
    result = {"folder": ""}
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(parent=root)
        root.destroy()
        result["folder"] = folder.replace("\\", "/") if folder else ""
    except Exception:
        pass
    return jsonify(result)


@app.route("/api/find", methods=["POST"])
def find():
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL не указан"})
    try:
        raw_sources = fetch_video_sources(url)
        result = []
        for s in raw_sources:
            entry = {"name": s.name, "url": s.url, "kind": s.kind, "mp4_urls": {}}
            if s.kind == "allatra_direct":
                try:
                    entry["mp4_urls"] = fetch_allatra_mp4_urls(s.url)
                except Exception:
                    pass
            result.append(entry)
        return jsonify({"sources": result})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/download")
def download_stream():
    url      = request.args.get("url", "")
    kind     = request.args.get("kind", "ytdlp")
    quality  = request.args.get("quality", "Лучшее (bestvideo+bestaudio)")
    folder   = request.args.get("folder", os.path.expanduser("~\\Downloads"))
    subs     = request.args.get("subs", "0") == "1"
    mp4_url  = request.args.get("mp4_url", "")
    page_url = request.args.get("page_url", "")
    slug     = slug_from_page_url(page_url) if page_url else ""

    q: queue.Queue = queue.Queue()

    def send(text=None, pct=None, label=None, done=False, dest=None, info=False):
        msg: dict = {}
        if done:
            msg["type"] = "done"
            if dest:
                msg["dest"] = dest
        elif text and (text.lower().startswith("error") or "✗" in text):
            msg["type"] = "error"
            msg["text"] = text
        else:
            msg["type"] = "log"
            if text is not None:
                msg["text"] = text
            if pct is not None:
                msg["pct"] = round(pct, 1)
            if label is not None:
                msg["label"] = label
        q.put(msg)

    def run():
        try:
            if kind == "allatra_direct":
                _run_direct(mp4_url or url, folder, send, slug=slug)
            else:
                _run_ytdlp(url, folder, quality, subs, send)
        except Exception as e:
            send(text=f"✗ {e}")
            q.put({"type": "error", "text": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return sse_response(q)


def _run_direct(mp4_url: str, folder: str, send, slug: str = ""):
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Папка не существует: {folder}")

    # Если передан embed-URL (не прямой MP4), получаем настоящую ссылку
    if not mp4_url.lower().endswith(".mp4"):
        send(text="  MP4-ссылка не передана, получаю с embed-страницы...")
        mp4_urls = fetch_allatra_mp4_urls(mp4_url)
        if not mp4_urls:
            raise ValueError("Не удалось получить MP4-ссылки с embed-страницы")
        qs = sorted(mp4_urls.keys(), key=lambda x: int(x.replace("p", "")), reverse=True)
        mp4_url = mp4_urls[qs[0]]
        send(text=f"  Выбрано качество: {qs[0]}")

    filename_override = f"{slug}.mp4" if slug else ""
    download_direct(mp4_url, folder, send, filename_override)


def _run_ytdlp(url: str, folder: str, quality: str, subs: bool, send, filename_prefix: str = ""):
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Папка не существует: {folder}")

    fmt_map = {
        "Лучшее (bestvideo+bestaudio)": ytdlp_mp4_format(),
        "1080p": ytdlp_mp4_format(1080),
        "720p":  ytdlp_mp4_format(720),
        "480p":  ytdlp_mp4_format(480),
        "360p":  ytdlp_mp4_format(360),
        "Только аудио (mp3)": "bestaudio/best",
    }
    fmt = fmt_map.get(quality, ytdlp_mp4_format())
    out_template = os.path.join(folder, f"{filename_prefix}%(title)s.%(ext)s")
    args = [
        sys.executable, "-m", "yt_dlp",
        "-f", fmt,
        "-o", out_template,
        "--no-playlist", "--newline", "--progress",
    ]
    if quality == "Только аудио (mp3)":
        args += ["-x", "--audio-format", "mp3"]
    else:
        args += ["--recode-video", "mp4"]
    if subs:
        args += ["--write-subs", "--write-auto-subs", "--sub-langs", "ru,en"]
    args.append(url)

    send(text=f"yt-dlp format: {fmt}")

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        pct = parse_progress(line)
        if pct is not None:
            send(pct=pct, text=line, label="Загрузка...")
        else:
            send(text=line)

    proc.wait()
    if proc.returncode == 0:
        send(done=True, dest=folder)
    else:
        raise RuntimeError(f"yt-dlp завершился с кодом {proc.returncode}")


@app.route("/api/parse-category")
def parse_category_stream():
    """SSE-поток: парсит ссылки на видео со страниц категории allatra.tv."""
    cat_url   = request.args.get("url", "").strip()
    all_pages = request.args.get("all_pages", "0") == "1"

    q: queue.Queue = queue.Queue()

    def send(**kwargs):
        q.put(kwargs)

    def run():
        try:
            collected: list[str] = []
            base_url = re.sub(r'[?&]page=\d+', '', cat_url).rstrip('&?')

            # Определяем диапазон страниц
            first_html = _get(cat_url)
            title_m = re.search(r'<h1[^>]*>(.*?)</h1>', first_html, re.S)
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else cat_url

            page_nums = [int(p) for p in re.findall(r'[?&]page=(\d+)', first_html)]
            max_page = max(page_nums, default=1)

            # Текущая страница из URL
            cur_page_m = re.search(r'[?&]page=(\d+)', cat_url)
            start_page = int(cur_page_m.group(1)) if cur_page_m else 1

            if all_pages:
                pages = list(range(1, max_page + 1))
                send(type="info", text=f"Заголовок: {title}", title=title)
                send(type="info", text=f"Страниц для обхода: {max_page}")
            else:
                pages = [start_page]
                send(type="info", text=f"Заголовок: {title}", title=title)
                send(type="info", text=f"Парсю страницу {start_page} из {max_page}")

            for page in pages:
                if page == start_page and not all_pages:
                    html = first_html
                elif page == 1 and all_pages:
                    html = first_html
                else:
                    sep = '&' if '?' in base_url else '?'
                    html = _get(f"{base_url}{sep}page={page}")

                links = re.findall(r'href="(/video/[^"?#]+)"', html)
                new_links = [f"https://allatra.tv{l}" for l in links
                             if f"https://allatra.tv{l}" not in collected]
                new_links = list(dict.fromkeys(new_links))
                collected.extend(new_links)

                send(type="page_done", page=page, found=len(new_links),
                     total=len(collected), max_page=max_page,
                     text=f"  Стр. {page}/{max_page}: +{len(new_links)} ссылок (всего {len(collected)})")

            send(type="done", urls=collected, total=len(collected), title=title)
        except Exception as e:
            send(type="error", text=str(e))
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return sse_response(q)


@app.route("/api/bulk-download")
def bulk_download_stream():
    """SSE-поток массовой загрузки: обрабатывает URL по очереди."""
    raw_urls = request.args.get("urls", "")
    mode     = request.args.get("mode", "video")    # "video" | "audio"
    quality  = request.args.get("quality", "720p")
    folder   = request.args.get("folder", os.path.expanduser("~\\Downloads"))

    urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]

    q: queue.Queue = queue.Queue()

    def send(idx: int, **kwargs):
        msg = {"idx": idx, **kwargs}
        q.put(msg)

    def run():
        for idx, page_url in enumerate(urls):
            send(idx, type="item-start", url=page_url)
            try:
                # Определяем источники
                raw_sources = fetch_video_sources(page_url)

                # Выбираем источник: предпочитаем allatra_direct, затем odysee
                src = (
                    next((s for s in raw_sources if s.kind == "allatra_direct"), None)
                    or next((s for s in raw_sources if "odysee" in s.url.lower()), None)
                    or raw_sources[0]
                )

                send(idx, type="log", text=f"  Источник: {src.name} ({src.kind})")

                cb = make_bulk_sender(idx, send)
                num = idx + 1
                slug = slug_from_page_url(page_url)
                num_prefix = f"{num}. "

                if mode == "audio":
                    if src.kind == "allatra_direct":
                        fallbacks = _sorted_ytdlp_sources(raw_sources)
                        if not fallbacks:
                            raise ValueError("Нет yt-dlp источника для аудио")
                        src = fallbacks[0]
                        send(idx, type="log", text=f"  Аудио: переключаюсь на {src.name}")
                    _run_ytdlp(src.url, folder, "Только аудио (mp3)", False, cb,
                               filename_prefix=num_prefix)

                elif src.kind == "allatra_direct":
                    mp4_urls = fetch_allatra_mp4_urls(src.url)
                    if not mp4_urls:
                        # Прямые MP4 недоступны — перебираем ytdlp-источники по приоритету
                        downloaded = False
                        for fb in _sorted_ytdlp_sources(raw_sources):
                            try:
                                send(idx, type="log",
                                     text=f"  AllatRa MP4 не найдены → пробую {fb.name}")
                                _run_ytdlp(fb.url, folder, quality, False, cb,
                                           filename_prefix=num_prefix)
                                downloaded = True
                                break
                            except Exception as fb_err:
                                send(idx, type="log",
                                     text=f"  {fb.name} не удался: {fb_err}")
                        if not downloaded:
                            raise ValueError("Все резервные источники недоступны")
                    else:
                        qs = sorted(mp4_urls.keys(), key=lambda x: int(x.replace("p", "")), reverse=True)
                        chosen_q = int(quality.replace("p", "") or "720")
                        mp4_url = mp4_urls.get(
                            quality,
                            next((mp4_urls[k] for k in qs if int(k.replace("p", "")) <= chosen_q),
                                 mp4_urls[qs[0]])
                        )
                        filename_override = f"{num_prefix}{slug}.mp4" if slug else ""
                        send(idx, type="log", text=f"  Качество: {quality}  →  {filename_override or mp4_url.split('/')[-1]}")
                        download_direct(mp4_url, folder, cb, filename_override)

                else:
                    _run_ytdlp(src.url, folder, quality, False, cb,
                               filename_prefix=num_prefix)

            except Exception as e:
                send(idx, type="item-error", text=str(e))

        q.put({"type": "all-done"})
        q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return sse_response(q)


if __name__ == "__main__":
    port = 7842
    url = f"http://localhost:{port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"  Открываю браузер: {url}")
    app.run(port=port, threaded=True, debug=False)
