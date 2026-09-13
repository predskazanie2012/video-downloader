"""
Загрузчик видео с allatra.tv
Источники: Odysee (yt-dlp) · AllatRa TV (прямые MP4) · YouTube (yt-dlp)
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import re
import html as html_mod
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field


# ─── Палитра ────────────────────────────────────────────────────────────────
BG      = "#16161e"
CARD    = "#24273a"
BORDER  = "#363653"
FG      = "#cad3f5"
FG_DIM  = "#6e7094"
ACCENT  = "#8aadf4"
GREEN   = "#a6da95"
RED     = "#ed8796"
YELLOW  = "#eed49f"
PURPLE  = "#c6a0f6"
CYAN    = "#8bd5ca"
FONT    = "Segoe UI"
MONO    = "Consolas"

BADGE_COLORS = {
    "YouTube":    ("#e05252", "#fff"),
    "Odysee":     ("#8b5cf6", "#fff"),
    "AllatRa TV": ("#0ea5e9", "#fff"),
}


# ─── Структуры данных ────────────────────────────────────────────────────────
@dataclass
class VideoSource:
    name: str
    url: str                           # для yt-dlp или embed_url для AllatRa TV
    kind: str = "ytdlp"               # "ytdlp" | "allatra_direct"
    mp4_urls: dict = field(default_factory=dict)  # quality → direct url


# ─── Парсинг источников ──────────────────────────────────────────────────────
def _get(url: str, referer: str = "") -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_allatra_mp4_urls(embed_url: str) -> dict[str, str]:
    """Парсит embed-страницу allatra.video → {качество: прямая_ссылка_mp4}"""
    html = _get(embed_url, referer="https://allatra.tv/")
    # Все уникальные mp4-ссылки
    urls = list(dict.fromkeys(re.findall(
        r'https://storage\d*\.allatra\.video/[^\s"\']+\.mp4', html)))
    result: dict[str, str] = {}
    for u in urls:
        m = re.search(r'_(\d+p)\.mp4', u)
        if m:
            result[m.group(1)] = u
    return result


def embed_to_ytdlp_url(name: str, embed_url: str) -> str | None:
    """Конвертирует embed-ссылку в URL для yt-dlp (YouTube / Odysee)."""
    m = re.search(r'allatra\.video/embed\?ytv=([A-Za-z0-9_-]{8,15})', embed_url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    m2 = re.search(r'odysee\.com/\$/embed/(.+?)(\?|$)', embed_url)
    if m2:
        return f"https://odysee.com/{m2.group(1)}"
    if embed_url.startswith("http") and "embed" not in embed_url:
        return embed_url
    return None


def fetch_video_sources(page_url: str) -> list[VideoSource]:
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
                    sources.append(VideoSource(
                        name=name, url=embed, kind="allatra_direct"))
                else:
                    dl_url = embed_to_ytdlp_url(name, embed)
                    if dl_url:
                        sources.append(VideoSource(name=name, url=dl_url, kind="ytdlp"))
        except (json.JSONDecodeError, KeyError):
            pass

    if sources:
        return sources

    # Запасные паттерны
    yt = re.search(r'youtube\.com(?:/embed/|/v/)([A-Za-z0-9_-]{8,15})', src)
    if yt:
        sources.append(VideoSource("YouTube", f"https://www.youtube.com/watch?v={yt.group(1)}"))
    od = re.search(r'odysee\.com/\$/embed/([^"\'&]+)', src)
    if od:
        sources.append(VideoSource("Odysee", f"https://odysee.com/{od.group(1).split('?')[0]}"))

    if not sources:
        raise ValueError("Не удалось найти видео на этой странице")
    return sources


# ─── Скачивание с прогрессом (прямой URL) ───────────────────────────────────
def download_direct(url: str, dest_path: str, progress_cb, log_cb):
    """Скачивает файл по прямой ссылке с отображением прогресса."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://allatra.video/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        chunk = 1024 * 256  # 256 KB

        with open(dest_path, "wb") as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    pct = done / total * 100
                    progress_cb(pct)
                    mb_done = done / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    log_cb(f"[download] {pct:5.1f}%  {mb_done:.1f} / {mb_total:.1f} МБ")
                else:
                    log_cb(f"[download] {done // 1024} КБ получено...")


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


# ─── Приложение ──────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Загрузчик видео — allatra.tv")
        self.geometry("820x720")
        self.minsize(680, 600)
        self.configure(bg=BG)
        self._setup_styles()
        self._build_ui()
        self._sources: list[VideoSource] = []
        self._selected_source: VideoSource | None = None
        self._source_var = tk.StringVar()

    def _setup_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TCombobox",
            fieldbackground=CARD, background=CARD, foreground=FG,
            arrowcolor=ACCENT, bordercolor=BORDER,
            lightcolor=BORDER, darkcolor=BORDER,
            selectbackground=ACCENT, selectforeground=BG,
            font=(FONT, 10), padding=6)
        s.map("TCombobox",
            fieldbackground=[("readonly", CARD)],
            background=[("readonly", CARD), ("active", BORDER)],
            foreground=[("readonly", FG)])
        s.configure("TScrollbar",
            troughcolor="#1a1a28", background=BORDER,
            arrowcolor=FG_DIM, bordercolor=BG)
        s.map("TScrollbar", background=[("active", ACCENT)])
        s.configure("prog.Horizontal.TProgressbar",
            troughcolor=CARD, background=ACCENT,
            bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT)

    # ─── Построение UI ───────────────────────────────────────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Заголовок
        hdr = tk.Frame(self, bg=BG)
        hdr.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 0))
        tk.Label(hdr, text="⬇", bg=BG, fg=ACCENT, font=(FONT, 22)).pack(side="left", padx=(0, 10))
        c = tk.Frame(hdr, bg=BG)
        c.pack(side="left")
        tk.Label(c, text="Загрузчик видео", bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(c, text="allatra.tv  ·  AllatRa TV (прямой MP4)  ·  Odysee  ·  YouTube",
                 bg=BG, fg=FG_DIM, font=(FONT, 9)).pack(anchor="w")

        # Тело
        body = tk.Frame(self, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=22, pady=10)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)

        self._build_settings(body)
        self._build_sources(body)
        self._build_quality_info(body)
        self._build_actions(body)
        self._build_log(body)

        # Статус
        self.status_var = tk.StringVar(value="Готов")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=FG_DIM,
                 font=(FONT, 9), anchor="w"
                 ).grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 8))

    def _build_settings(self, parent):
        f = tk.LabelFrame(parent, text=" Параметры ", bg=BG, fg=FG_DIM,
                          font=(FONT, 9), bd=1, relief="solid")
        f.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(1, weight=1)

        self._lbl(f, "Страница allatra.tv", 0)
        self.url_var = tk.StringVar(value="https://allatra.tv/video/spasibo")
        self._entry(f, self.url_var, 0, colspan=2)

        self._lbl(f, "Папка сохранения", 1)
        self.folder_var = tk.StringVar(value=os.path.expanduser("~\\Downloads"))
        self._entry(f, self.folder_var, 1)
        tk.Button(f, text="📁", bg=CARD, fg=FG, relief="flat", font=(FONT, 11),
                  cursor="hand2", activebackground=BORDER, activeforeground=FG,
                  command=self._choose_folder
                  ).grid(row=1, column=2, padx=(4, 12), pady=4)

    def _build_sources(self, parent):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        outer.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        outer.columnconfigure(0, weight=1)

        tk.Label(outer, text="Источники", bg=CARD, fg=FG_DIM,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=12, pady=(8, 2))

        self._src_frame = tk.Frame(outer, bg=CARD)
        self._src_frame.pack(fill="x", padx=12, pady=(0, 8))
        self._src_frame.columnconfigure(2, weight=1)

        tk.Label(self._src_frame, text="— нажми «Найти видео»",
                 bg=CARD, fg=FG_DIM, font=(FONT, 10, "italic")).grid(row=0, column=0, sticky="w")

    def _build_quality_info(self, parent):
        """Строка с информацией о качестве (динамическая)."""
        self._quality_frame = tk.Frame(parent, bg=BG)
        self._quality_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._quality_frame.columnconfigure(1, weight=1)

        tk.Label(self._quality_frame, text="Качество:", bg=BG, fg=FG_DIM,
                 font=(FONT, 9)).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.quality_var = tk.StringVar(value="Лучшее")
        self._quality_cb = ttk.Combobox(self._quality_frame, textvariable=self.quality_var,
                                        values=["Лучшее"], state="readonly", font=(FONT, 10))
        self._quality_cb.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        self._quality_note = tk.Label(self._quality_frame, text="", bg=BG, fg=FG_DIM,
                                      font=(FONT, 9, "italic"))
        self._quality_note.grid(row=0, column=2, sticky="w")

        self.subs_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self._quality_frame, text="Субтитры (для YouTube/Odysee)",
                       variable=self.subs_var, bg=BG, fg=FG_DIM,
                       selectcolor=CARD, activebackground=BG, activeforeground=FG,
                       font=(FONT, 9)).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_actions(self, parent):
        f = tk.Frame(parent, bg=BG)
        f.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        f.columnconfigure(2, weight=1)

        self.btn_detect = tk.Button(
            f, text="🔍  Найти видео", bg=GREEN, fg=BG,
            font=(FONT, 10, "bold"), relief="flat", padx=16, pady=8,
            cursor="hand2", activebackground="#b5e8a0", activeforeground=BG,
            command=self._detect)
        self.btn_detect.grid(row=0, column=0, padx=(0, 8))

        self.dl_btn = tk.Button(
            f, text="⬇  Скачать", bg=ACCENT, fg=BG,
            font=(FONT, 10, "bold"), relief="flat", padx=16, pady=8,
            cursor="hand2", activebackground="#a0c4f5", activeforeground=BG,
            command=self._download, state="disabled", disabledforeground="#555577")
        self.dl_btn.grid(row=0, column=1, padx=(0, 16))

        prog_col = tk.Frame(f, bg=BG)
        prog_col.grid(row=0, column=2, sticky="ew")
        prog_col.columnconfigure(0, weight=1)
        self.pct_var = tk.StringVar(value="")
        tk.Label(prog_col, textvariable=self.pct_var, bg=BG, fg=ACCENT,
                 font=(MONO, 9), anchor="e").grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(prog_col, style="prog.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(2, 0))

    def _build_log(self, parent):
        f = tk.Frame(parent, bg=BG)
        f.grid(row=4, column=0, sticky="nsew")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        hdr = tk.Frame(f, bg=BG)
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        tk.Label(hdr, text="Лог", bg=BG, fg=FG_DIM, font=(FONT, 9, "bold")).pack(side="left")
        tk.Button(hdr, text="очистить", bg=BG, fg=FG_DIM, relief="flat",
                  font=(FONT, 8), cursor="hand2", activebackground=BG, activeforeground=FG,
                  command=self._clear_log).pack(side="right")

        wrap = tk.Frame(f, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.log = tk.Text(wrap, bg=CARD, fg=FG, relief="flat", font=(MONO, 10),
                           state="disabled", wrap="word", padx=10, pady=8,
                           selectbackground=ACCENT, selectforeground=BG)
        scroll = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.log.tag_config("ok",      foreground=GREEN)
        self.log.tag_config("err",     foreground=RED)
        self.log.tag_config("info",    foreground=CYAN)
        self.log.tag_config("warn",    foreground=YELLOW)
        self.log.tag_config("prog",    foreground=ACCENT)
        self.log.tag_config("dim",     foreground=FG_DIM)
        self.log.tag_config("section", foreground=PURPLE, font=(MONO, 10, "bold"))

    # ─── Вспомогательные методы ──────────────────────────────────────────────
    def _lbl(self, p, t, r):
        tk.Label(p, text=t, bg=BG, fg=FG_DIM, font=(FONT, 9), anchor="e"
                 ).grid(row=r, column=0, padx=(12, 8), pady=4, sticky="e")

    def _entry(self, p, v, r, colspan=1):
        e = tk.Entry(p, textvariable=v, bg=CARD, fg=FG, insertbackground=FG,
                     relief="flat", font=(FONT, 10),
                     highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        e.grid(row=r, column=1, columnspan=colspan,
               padx=(0, 4 if colspan > 1 else 0), pady=4, sticky="ew", ipady=5)
        return e

    def _log(self, text: str, tag: str = ""):
        tag = tag or self._auto_tag(text)
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _auto_tag(self, text: str) -> str:
        t = text.lower()
        if text.startswith(("✅", "✓")):                 return "ok"
        if text.startswith(("✗", "❌")) or "error" in t: return "err"
        if text.startswith("▶"):                          return "section"
        if "[download]" in t:                             return "prog"
        if "[info]" in t or "[youtube]" in t or "[odysee]" in t: return "info"
        if "warning" in t:                                return "warn"
        if text.startswith("─"):                          return "dim"
        return ""

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_status(self, t): self.status_var.set(t)

    def _choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get())
        if d:
            self.folder_var.set(d)

    def _start_spin(self):
        self.progress.configure(mode="indeterminate")
        self.pct_var.set("")
        self.progress.start(12)

    def _stop_spin(self):
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.pct_var.set("")

    def _set_progress(self, pct: float):
        self.progress.configure(mode="determinate", value=pct)
        self.pct_var.set(f"{pct:.0f}%")

    # ─── Источники ───────────────────────────────────────────────────────────
    def _render_sources(self, sources: list[VideoSource]):
        for w in self._src_frame.winfo_children():
            w.destroy()

        if not sources:
            tk.Label(self._src_frame, text="Источники не найдены",
                     bg=CARD, fg=RED, font=(FONT, 10, "italic")).grid(row=0, column=0)
            return

        for i, src in enumerate(sources):
            rb = tk.Radiobutton(
                self._src_frame, text="", variable=self._source_var, value=src.url,
                bg=CARD, activebackground=CARD, selectcolor=CARD, fg=FG,
                command=lambda s=src: self._on_source_select(s))
            rb.grid(row=i, column=0, padx=(0, 4), sticky="w")

            bc, bfg = BADGE_COLORS.get(src.name, (BORDER, FG))
            lbl = tk.Label(self._src_frame,
                           text=f"  {src.name}  ",
                           bg=bc, fg=bfg, font=(FONT, 9, "bold"), padx=2)
            lbl.grid(row=i, column=1, padx=(0, 10), sticky="w")
            lbl.bind("<Button-1>", lambda e, s=src: self._select_source(s))

            kind_lbl = "(прямой MP4)" if src.kind == "allatra_direct" else "(yt-dlp)"
            url_short = src.url if len(src.url) < 65 else src.url[:62] + "..."
            tk.Label(self._src_frame,
                     text=f"{kind_lbl}  {url_short}",
                     bg=CARD, fg=FG_DIM, font=(MONO, 9), anchor="w"
                     ).grid(row=i, column=2, sticky="ew")

        # Авто-выбор: предпочесть AllatRa TV (прямой MP4), потом Odysee
        preferred = (
            next((s for s in sources if s.kind == "allatra_direct"), None)
            or next((s for s in sources if "odysee" in s.url.lower()), None)
            or sources[0]
        )
        self._select_source(preferred)

    def _select_source(self, src: VideoSource):
        self._source_var.set(src.url)
        self._on_source_select(src)

    def _on_source_select(self, src: VideoSource):
        self._selected_source = src
        self.dl_btn.configure(state="normal")

        if src.kind == "allatra_direct":
            if src.mp4_urls:
                qualities = sorted(src.mp4_urls.keys(),
                                   key=lambda q: int(q.replace("p", "")), reverse=True)
                self._quality_cb.configure(values=qualities, state="readonly")
                self.quality_var.set(qualities[0])
                self._quality_note.configure(
                    text=f"Доступно: {', '.join(qualities)}  — прямой MP4 с allatra.video",
                    fg=CYAN)
            else:
                self._quality_cb.configure(values=["Определится при загрузке"], state="readonly")
                self.quality_var.set("Определится при загрузке")
                self._quality_note.configure(text="Качество будет определено при загрузке", fg=FG_DIM)
        else:
            self._quality_cb.configure(
                values=["Лучшее (bestvideo+bestaudio)", "1080p", "720p", "480p", "360p", "Только аудио (mp3)"],
                state="readonly")
            self.quality_var.set("Лучшее (bestvideo+bestaudio)")
            self._quality_note.configure(text="", fg=FG_DIM)

    # ─── Найти видео ─────────────────────────────────────────────────────────
    def _detect(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Пустая ссылка", "Введите URL страницы allatra.tv")
            return
        self.dl_btn.configure(state="disabled")
        self._sources = []
        self._selected_source = None
        self._start_spin()
        self._set_status("Загружаю страницу...")
        self._log("─" * 65, "dim")
        self._log(f"▶ Открываю: {url}", "section")
        threading.Thread(target=self._detect_worker, args=(url,), daemon=True).start()

    def _detect_worker(self, url: str):
        try:
            sources = fetch_video_sources(url)
            # Для AllatRa TV — сразу грузим MP4-ссылки
            for src in sources:
                if src.kind == "allatra_direct":
                    try:
                        src.mp4_urls = fetch_allatra_mp4_urls(src.url)
                    except Exception as e:
                        self.after(0, lambda e=e: self._log(f"  ! Не удалось получить MP4: {e}", "warn"))

            self._sources = sources
            self.after(0, lambda: self._render_sources(sources))
            self.after(0, lambda: self._log(f"✓ Найдено источников: {len(sources)}", "ok"))
            for s in sources:
                extra = ""
                if s.kind == "allatra_direct" and s.mp4_urls:
                    qs = sorted(s.mp4_urls, key=lambda q: int(q.replace("p", "")), reverse=True)
                    extra = f"  [MP4: {', '.join(qs)}]"
                self.after(0, lambda s=s, x=extra: self._log(f"  • {s.name}: {s.url}{x}", "info"))
            self.after(0, lambda: self._set_status(f"Найдено источников: {len(sources)}"))
        except Exception as e:
            self.after(0, lambda: self._log(f"✗ {e}", "err"))
            self.after(0, lambda: self._set_status("Ошибка"))
        finally:
            self.after(0, self._stop_spin)

    # ─── Скачать ─────────────────────────────────────────────────────────────
    def _download(self):
        if not self._selected_source:
            messagebox.showwarning("Источник", "Выбери источник")
            return
        folder = self.folder_var.get()
        if not os.path.isdir(folder):
            messagebox.showerror("Папка не найдена", f"Папка не существует:\n{folder}")
            return

        self.dl_btn.configure(state="disabled")
        self.btn_detect.configure(state="disabled")
        self._start_spin()
        q = self.quality_var.get()
        self._log("─" * 65, "dim")
        self._log(f"▶ Загрузка  [{self._selected_source.name}]  {q}", "section")
        self._set_status("Загружаю...")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        src = self._selected_source
        try:
            if src.kind == "allatra_direct":
                self._download_direct(src)
            else:
                self._download_ytdlp(src)
        except Exception as e:
            self.after(0, lambda: self._log(f"✗ Ошибка: {e}", "err"))
            self.after(0, lambda: self._set_status("Ошибка загрузки"))
        finally:
            self.after(0, self._stop_spin)
            self.after(0, lambda: self.dl_btn.configure(state="normal"))
            self.after(0, lambda: self.btn_detect.configure(state="normal"))

    def _download_direct(self, src: VideoSource):
        q = self.quality_var.get()
        # Если mp4_urls уже известны
        if not src.mp4_urls:
            self.after(0, lambda: self._log("Получаю ссылки MP4...", "info"))
            src.mp4_urls = fetch_allatra_mp4_urls(src.url)

        mp4_url = src.mp4_urls.get(q) or src.mp4_urls.get(
            sorted(src.mp4_urls, key=lambda x: int(x.replace("p", "")), reverse=True)[0])

        self.after(0, lambda: self._log(f"  MP4: {mp4_url}", "info"))

        filename = re.sub(r'[\\/*?:"<>|]', "_",
                          os.path.basename(mp4_url.split("?")[0]))
        dest = os.path.join(self.folder_var.get(), filename)

        def progress_cb(pct):
            self.after(0, lambda p=pct: self._set_progress(p))

        last_log = [0.0]
        def log_cb(text):
            pct = parse_progress(text)
            if pct is None or pct - last_log[0] >= 5:
                last_log[0] = pct or last_log[0]
                self.after(0, lambda t=text: self._log(t))

        download_direct(mp4_url, dest, progress_cb, log_cb)

        self.after(0, lambda: self._set_progress(100))
        self.after(0, lambda: self._log(f"✅ Сохранено: {dest}", "ok"))
        self.after(0, lambda: self._set_status(f"Готово! → {self.folder_var.get()}"))
        self.after(0, lambda: messagebox.showinfo("Готово", f"Файл сохранён:\n{dest}"))

    def _download_ytdlp(self, src: VideoSource):
        q = self.quality_var.get()
        fmt_map = {
            "Лучшее (bestvideo+bestaudio)": ytdlp_mp4_format(),
            "1080p": ytdlp_mp4_format(1080),
            "720p":  ytdlp_mp4_format(720),
            "480p":  ytdlp_mp4_format(480),
            "360p":  ytdlp_mp4_format(360),
            "Только аудио (mp3)": "bestaudio/best",
        }
        fmt = fmt_map.get(q, ytdlp_mp4_format())
        args = [
            sys.executable, "-m", "yt_dlp",
            "-f", fmt,
            "-o", os.path.join(self.folder_var.get(), "%(title)s.%(ext)s"),
            "--no-playlist", "--newline", "--progress",
        ]
        if q == "Только аудио (mp3)":
            args += ["-x", "--audio-format", "mp3"]
        else:
            args += ["--recode-video", "mp4"]
        if self.subs_var.get():
            args += ["--write-subs", "--write-auto-subs", "--sub-langs", "ru,en"]
        args.append(src.url)

        self.after(0, lambda f=fmt: self._log(f"yt-dlp format: {f}"))

        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            pct = parse_progress(line)
            if pct is not None:
                self.after(0, lambda p=pct: self._set_progress(p))
            self.after(0, lambda l=line: self._log(l))

        proc.wait()
        if proc.returncode == 0:
            self.after(0, lambda: self._set_progress(100))
            self.after(0, lambda: self._log("✅ Загрузка завершена!", "ok"))
            self.after(0, lambda: self._set_status(f"Готово! → {self.folder_var.get()}"))
            self.after(0, lambda: messagebox.showinfo(
                "Готово", f"Видео сохранено в:\n{self.folder_var.get()}"))
        else:
            self.after(0, lambda c=proc.returncode: self._log(
                f"✗ yt-dlp завершился с кодом {c}", "err"))
            self.after(0, lambda: self._set_status(f"Ошибка yt-dlp (код {proc.returncode})"))


if __name__ == "__main__":
    app = App()
    app.mainloop()
