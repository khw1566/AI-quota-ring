"""AI 사용량 한도 작업표시줄 위젯 (Antigravity · Claude Code · Codex).

작업표시줄 위에 도구별 주간 잔량(원형 그래프)과 리셋까지 남은 시간을 표시한다.
마우스를 0.7초 올려두면 상세 패널, 클릭하면 패널 고정, 드래그로 좌우 이동, 우클릭으로 메뉴.
바는 작업표시줄이 소유한 창으로 띄워서, 작업표시줄을 클릭해도 가려지거나 깜박이지 않는다.
"""
import ctypes
import ctypes.wintypes
import json
import os
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import collector

CONFIG_FILE = os.path.join(collector.CACHE_DIR, "widget.json")
REFRESH_SEC = 120
TICK_MS = 20 * 1000
PANEL_W = 400          # 상세 패널 폭 (96dpi 기준)
CELL_W = 70            # 작업표시줄 칸 폭 (96dpi 기준)
HOVER_MS = 700         # 상세 패널이 뜨기까지 마우스를 올려둬야 하는 시간
TRAY_GAP = 6           # 알림 영역(트레이)과의 간격

C = {
    "bg": "#16171b", "card": "#1f2026", "line": "#2a2b33", "track": "#3a3b45",
    "text": "#ececf1", "sub": "#9a9ba7", "muted": "#5f606b",
    "good": "#3ecf8e", "warn": "#f5b544", "bad": "#f06464",
}
BRAND = {"Antigravity": "#4f8cff", "Claude Code": "#d97757", "Codex": "#a8a8b8"}
SHORT = {"Antigravity": "AG", "Claude Code": "Claude", "Codex": "Codex"}
FONT = "Malgun Gothic"


# ---------------------------------------------------------------- formatting

def level_color(pct):
    if pct is None:
        return C["muted"]
    return C["good"] if pct >= 50 else C["warn"] if pct >= 20 else C["bad"]


def _dhm(secs):
    d, rem = divmod(int(secs), 86400)
    return d, rem // 3600, rem % 3600 // 60


def fmt_short(ts):
    """상세 패널용 남은 시간: '6일 3시간', '4시간 12분', '35분'."""
    if not ts:
        return "–"
    secs = ts - time.time()
    if secs <= 0:
        return "리셋됨"
    d, h, m = _dhm(secs)
    return f"{d}일 {h}시간" if d else f"{h}시간 {m}분" if h else f"{max(m, 1)}분"


def fmt_tiny(ts):
    """작업표시줄용 아주 짧은 남은 시간: '6d 3h', '4h 12m', '35m'."""
    if not ts:
        return "–"
    secs = ts - time.time()
    if secs <= 0:
        return "리셋"
    d, h, m = _dhm(secs)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def fmt_left(ts, estimated=False):
    if not ts:
        return ""
    if ts - time.time() <= 0:
        return "리셋됨 (추정)" if estimated else "리셋됨"
    lt = time.localtime(ts)
    same_day = lt.tm_yday == time.localtime().tm_yday
    when = time.strftime("%H:%M" if same_day else "%m/%d %H:%M", lt)
    return f"{fmt_short(ts)} 후 · {when}"


def fmt_ago(ts):
    s = time.time() - ts
    if s < 60:
        return "방금"
    if s < 3600:
        return f"{int(s // 60)}분 전"
    return f"{int(s // 3600)}시간 전"


def antigravity_groups(src):
    """Antigravity 창을 (Gemini 묶음, Claude·GPT 묶음)으로 나눈다."""
    gem = other = None
    for w in (src or {}).get("windows", []):
        if w["missing"] or w["remaining"] is None:
            continue
        if "Gemini" in w["label"]:
            gem = gem or w
        elif "Claude" in w["label"] or "GPT" in w["label"]:
            other = other or w
    return gem, other


def weekly_window(src):
    """작업표시줄에 쓸 창: 주간 창 우선, 없으면 잔량이 가장 적은 창."""
    rows = [w for w in (src or {}).get("windows", []) if not w["missing"] and w["remaining"] is not None]
    weekly = [w for w in rows if w["label"].startswith("주간")]
    pool = weekly or rows
    return min(pool, key=lambda w: w["remaining"]) if pool else None


# ---------------------------------------------------------------- win32 helpers

class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


user32 = ctypes.windll.user32
GWLP_HWNDPARENT = -8
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x10
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]


def window_rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def taskbar_info():
    """(작업표시줄 hwnd, 작업표시줄 rect, 트레이 왼쪽 x) — 찾지 못하면 None."""
    tb = user32.FindWindowW("Shell_TrayWnd", None)
    if not tb:
        return None
    tray = user32.FindWindowExW(tb, 0, "TrayNotifyWnd", None)
    rect = window_rect(tb)
    return tb, rect, (window_rect(tray)[0] if tray else rect[2] - 330)


def cursor_pos():
    """실제 마우스 좌표. 바를 Win32 로 옮기므로 Tk 의 x_root 는 어긋날 수 있어 쓰지 않는다."""
    p = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def set_owner(hwnd, owner):
    fn = user32.SetWindowLongPtrW
    fn.argtypes, fn.restype = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
    return fn(hwnd, GWLP_HWNDPARENT, owner)


def screen_pixel(x, y):
    hdc = ctypes.windll.user32.GetDC(0)
    c = ctypes.windll.gdi32.GetPixel(hdc, x, y)
    ctypes.windll.user32.ReleaseDC(0, hdc)
    if c < 0 or c == 0xFFFFFFFF:
        return None
    return f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"


# ---------------------------------------------------------------- app

class App:
    def __init__(self):
        try:  # 고해상도 화면에서 흐릿하지 않게
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        self.cfg = self._load_cfg()
        self.data = [collector.load_cache().get(n) for n, _ in collector.SOURCES]
        self.loading = False
        self.timer = None
        self.pinned = False
        self.hide_job = None
        self.show_job = None
        self.hwnd = None          # 바 창 핸들
        self.owner_tb = None   # 바를 소유한 작업표시줄 hwnd
        self._dragged = False

        self.root = tk.Tk()
        self.root.title("AI Token Info")
        self.s = self.root.winfo_fpixels("1i") / 96
        self.f_title = tkfont.Font(family=FONT, size=10, weight="bold")
        self.f_body = tkfont.Font(family=FONT, size=9)
        self.f_small = tkfont.Font(family=FONT, size=8)
        self.f_ring = tkfont.Font(family="Segoe UI", size=7, weight="bold")
        self.f_ring_small = tkfont.Font(family="Segoe UI", size=6, weight="bold")  # 이중 원 안쪽용
        self.f_bar_name = tkfont.Font(family="Segoe UI", size=7)
        self.f_bar_time = tkfont.Font(family="Segoe UI", size=8, weight="bold")

        self._build_bar()
        self._build_panel()
        self._build_menu()

        self.draw_bar()
        self.refresh()
        self.root.after(TICK_MS, self._tick)
        self.root.after(3000, self._watch_taskbar)

    # ------------------------------------------------------------ config
    def _load_cfg(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        os.makedirs(collector.CACHE_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f)

    # ------------------------------------------------------------ taskbar bar
    def _build_bar(self):
        self.root.overrideredirect(True)
        self.bar_w = int(CELL_W * len(collector.SOURCES) * self.s)
        self.cv = tk.Canvas(self.root, width=self.bar_w, highlightthickness=0, bd=0, cursor="hand2")
        self.cv.pack()
        self.root.update_idletasks()
        self.hwnd = int(self.root.wm_frame(), 16)
        self._place_bar()
        self.cv.bind("<Enter>", lambda e: self._hover_in())
        self.cv.bind("<Leave>", lambda e: self._hover_out())
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Button-3>", lambda e: self.menu.tk_popup(*cursor_pos()))

    def _attach(self, tb):
        """작업표시줄을 바의 소유자(owner)로 지정한다.

        소유된 창은 항상 소유자 위에 그려지므로, 작업표시줄이 앞으로 나와도 같이 올라와
        가려졌다 다시 나타나는 깜박임이 없다. (Win11 은 자식 창으로 넣으면 XAML 레이어에 가려진다)
        """
        if self.owner_tb == tb:
            return
        set_owner(self.hwnd, tb)
        self.root.attributes("-topmost", True)
        self.owner_tb = tb

    def _target_x(self, info):
        """트레이 왼쪽에 붙인다. 사용자가 옮겼다면 트레이로부터의 거리를 유지한다."""
        tb, (tl, _, _, _), tray_left = info
        offset = self.cfg.get("tray_offset", TRAY_GAP)
        if not 0 <= offset <= tray_left - tl - self.bar_w:   # 비정상 값이면 기본 위치
            offset = TRAY_GAP
        return max(tl, tray_left - offset - self.bar_w)

    def _place_bar(self):
        info = taskbar_info()
        if info:
            tb, (tl, tt, _, tbm), _ = info
            x, tb_h = self._target_x(info), tbm - tt
        else:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            tb, tl, tb_h = None, 0, int(48 * self.s)
            tt, x = sh - tb_h, sw - self.bar_w - int(330 * self.s)
        self.bar_h = tb_h
        self.bar_x, self.bar_y = x, tt
        self.cv.config(height=tb_h)
        if tb:
            self._attach(tb)
        else:
            self.root.attributes("-topmost", True)
        self._move()
        self._match_taskbar_color()

    def _move(self):
        # 소유자를 바꾼 뒤에는 Tk geometry 가 반영되지 않아 Win32 로 직접 옮긴다
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self.bar_x, self.bar_y, self.bar_w, self.bar_h,
                            SWP_NOACTIVATE)

    def _watch_taskbar(self):
        """작업표시줄 재시작·트레이 폭 변화가 있을 때만 위치를 다시 잡는다."""
        info = taskbar_info()
        if info and not self._dragged:
            tb, (_, tt, _, _), _ = info
            if tb != self.owner_tb or self._target_x(info) != self.bar_x or tt != self.bar_y:
                self._place_bar()
                self.draw_bar()
        self.root.after(3000, self._watch_taskbar)

    def _match_taskbar_color(self):
        """작업표시줄 색을 바 바로 왼쪽에서 샘플링해 배경으로 쓴다."""
        px = screen_pixel(max(0, self.bar_x - 3), self.bar_y + self.bar_h // 2)
        self.bar_bg = px or "#202020"
        self.cv.config(bg=self.bar_bg)
        self.root.config(bg=self.bar_bg)
        r, g, b = (int(self.bar_bg[i:i + 2], 16) for i in (1, 3, 5))
        self.bar_light = (r * 299 + g * 587 + b * 114) / 1000 > 140   # 밝은 작업표시줄 테마

    def draw_bar(self):
        cv, s = self.cv, self.s
        cv.delete("all")
        h = self.bar_h
        fg = "#1a1a1a" if self.bar_light else "#f2f2f2"
        sub = "#555555" if self.bar_light else "#a8a8a8"
        track = "#d0d0d0" if self.bar_light else "#4a4a4a"
        d = min(h - 16 * s, 24 * s)                 # 원 지름
        width = max(2.5, 2.6 * s)
        cy = h / 2

        def ring(cx, dia, pct, wd):
            box = (cx - dia / 2, cy - dia / 2, cx + dia / 2, cy + dia / 2)
            cv.create_oval(*box, outline=track, width=wd)
            if pct:
                extent = -359.9 if pct >= 99.95 else -360 * pct / 100
                cv.create_arc(*box, start=90, extent=extent, style="arc", outline=level_color(pct), width=wd)

        cell = CELL_W * s
        for i, src in enumerate(self.data):
            x0 = i * cell + 3 * s
            name = src["name"] if src else ""
            font = self.f_ring
            if name == "Antigravity":
                # 이중 원: 바깥 = Gemini, 안쪽 = Claude·GPT. 숫자·시간은 Gemini 기준
                w, other = antigravity_groups(src)
                thin = max(2, 2.0 * s)
                ring(x0 + d / 2, d, w and w["remaining"], thin)
                ring(x0 + d / 2, d - 2 * thin - 2 * s, other and other["remaining"], thin)
                font = self.f_ring_small
            else:
                w = weekly_window(src)
                ring(x0 + d / 2, d, w and w["remaining"], width)
            pct = w["remaining"] if w else None
            cv.create_text(x0 + d / 2, cy, text="–" if pct is None else f"{pct:.0f}", fill=fg, font=font)
            tx = x0 + d + 4 * s
            cv.create_text(tx, cy - 6 * s, text=SHORT.get(name, name or "–"), anchor="w", fill=sub,
                           font=self.f_bar_name)
            cv.create_text(tx, cy + 6 * s, text=fmt_tiny(w["resets_at"]) if w else "–",
                           anchor="w", fill=fg, font=self.f_bar_time)
        if self.loading:
            cv.create_oval(self.bar_w - 5 * s, 3 * s, self.bar_w - 2 * s, 6 * s, fill=C["warn"], outline="")

    # bar interaction: 클릭 = 패널 고정, 드래그 = 좌우 이동
    def _press(self, e):
        self._drag_from = (cursor_pos()[0], self.bar_x)
        self._dragged = False

    def _drag(self, e):
        dx = cursor_pos()[0] - self._drag_from[0]
        if abs(dx) > 4 and not self._dragged:
            self._dragged = True
            self._cancel_show()
            if not self.pinned:
                self.panel.withdraw()
        if not self._dragged:
            return
        self.bar_x = max(0, min(self._drag_from[1] + dx, self.root.winfo_screenwidth() - self.bar_w))
        self._move()
        if self.pinned:
            self._position_panel()

    def _release(self, e):
        if self._dragged:
            info = taskbar_info()
            if info:
                self.cfg["tray_offset"] = info[2] - (self.bar_x + self.bar_w)
                self._save_cfg()
            self._dragged = False
            self._match_taskbar_color()
            self.draw_bar()
        else:
            self._cancel_show()
            self._toggle_pin()

    # ------------------------------------------------------------ detail panel
    def _build_panel(self):
        p = self.panel = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg=C["bg"])
        p.withdraw()
        self.pcv = tk.Canvas(p, width=int(PANEL_W * self.s), height=100, bg=C["bg"],
                             highlightthickness=1, highlightbackground=C["line"], bd=0)
        self.pcv.pack()
        self.pcv.bind("<Enter>", lambda e: self._cancel_hide())
        self.pcv.bind("<Leave>", lambda e: self._hover_out())
        self.pcv.bind("<Button-1>", lambda e: self._toggle_pin())
        self.pcv.bind("<Button-3>", lambda e: self.menu.tk_popup(*cursor_pos()))

    def _hover_in(self):
        self._cancel_hide()
        if not self.panel.winfo_viewable() and not self.show_job:
            self.show_job = self.root.after(HOVER_MS, self._show_panel)

    def _hover_out(self):
        self._cancel_show()
        self._schedule_hide()

    def _cancel_show(self):
        if self.show_job:
            self.root.after_cancel(self.show_job)
            self.show_job = None

    def _show_panel(self):
        self.show_job = None
        self._cancel_hide()
        self.draw_panel()
        self.panel.deiconify()
        self._position_panel()
        self.panel.lift()

    def _toggle_pin(self):
        self.pinned = not self.pinned
        if self.pinned:
            self._show_panel()
        else:
            self.panel.withdraw()

    def _position_panel(self):
        self.panel.update_idletasks()
        pw, ph = self.panel.winfo_reqwidth(), self.panel.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        x = max(0, min(self.bar_x + self.bar_w - pw + int(TRAY_GAP * self.s), sw - pw))
        y = self.bar_y - ph - int(8 * self.s)
        self.panel.geometry(f"+{x}+{y}")

    def _schedule_hide(self):
        if self.pinned:
            return
        self._cancel_hide()
        self.hide_job = self.root.after(350, self.panel.withdraw)

    def _cancel_hide(self):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None

    def draw_panel(self):
        cv = self.pcv
        cv.delete("all")
        pad, y = 14, 12
        cv.create_text(pad, y + 8, text="AI 사용량 한도", anchor="w", fill=C["text"], font=self.f_title)
        status = "갱신 중…" if self.loading else ("고정됨 · 클릭하여 닫기" if self.pinned else "클릭하여 고정")
        cv.create_text(PANEL_W - pad, y + 8, text=status, anchor="e", fill=C["muted"], font=self.f_small)
        y += 26
        for src in self.data:
            if src:
                y = self._draw_source(src, y, pad)
        cv.scale("all", 0, 0, self.s, self.s)
        cv.config(height=int((y + 4) * self.s))

    def _draw_source(self, src, y, pad):
        cv = self.pcv
        top, inner, name = y, pad + 12, src["name"]
        rows = src["windows"] or []
        h = 34 + max(len(rows), 1) * 40 + (16 if src.get("note") else 0)
        cv.create_rectangle(pad, top, PANEL_W - pad, top + h, fill=C["card"], outline="")
        cv.create_rectangle(pad, top, pad + 3, top + h, fill=BRAND.get(name, C["sub"]), outline="")
        cv.create_text(inner, top + 16, text=name, anchor="w", fill=C["text"], font=self.f_title)
        meta = [m for m in (src.get("plan"), fmt_ago(src["fetched_at"]) if src.get("fetched_at") else None) if m]
        cv.create_text(PANEL_W - pad - 12, top + 16, text=" · ".join(meta), anchor="e",
                       fill=C["sub"], font=self.f_small)
        y = top + 34
        if not rows:
            cv.create_text(inner, y + 14, text="데이터 없음", anchor="w", fill=C["muted"], font=self.f_body)
            y += 40
        bar_x0, bar_x1 = inner, PANEL_W - pad - 84
        right = PANEL_W - pad - 12
        for w in rows:
            if w["missing"]:
                cv.create_text(inner, y + 8, text=w["label"], anchor="w", fill=C["muted"], font=self.f_body)
                cv.create_text(right, y + 8, text="한도 없음 / 미제공", anchor="e", fill=C["muted"], font=self.f_small)
                cv.create_rectangle(bar_x0, y + 20, bar_x1, y + 26, fill=C["line"], outline="")
                y += 40
                continue
            pct = w["remaining"]
            col = level_color(pct)
            cv.create_text(inner, y + 8, text=self._ellipsize(w["label"], 190), anchor="w",
                           fill=C["sub"], font=self.f_body)
            cv.create_text(right, y + 8, text=fmt_left(w["resets_at"], w.get("estimated")), anchor="e",
                           fill=C["sub"], font=self.f_small)
            cv.create_rectangle(bar_x0, y + 20, bar_x1, y + 26, fill=C["track"], outline="")
            if pct:
                fx = bar_x0 + (bar_x1 - bar_x0) * pct / 100
                cv.create_rectangle(bar_x0, y + 20, max(fx, bar_x0 + 2), y + 26, fill=col, outline="")
            cv.create_text(right, y + 23, text=f"{pct:.0f}% 남음", anchor="e", fill=col, font=self.f_small)
            y += 40
        if src.get("note"):
            col = C["warn"] if not src.get("ok", True) else C["muted"]
            cv.create_text(inner, y + 2, text=self._ellipsize(src["note"], PANEL_W - inner - pad - 12),
                           anchor="w", fill=col, font=self.f_small)
        return top + h + 8

    def _ellipsize(self, text, max_px):
        f = self.f_body
        max_px *= self.s
        if f.measure(text) <= max_px:
            return text
        while text and f.measure(text + "…") > max_px:
            text = text[:-1]
        return text + "…"

    # ------------------------------------------------------------ menu
    def _build_menu(self):
        m = self.menu = tk.Menu(self.root, tearoff=0)
        m.add_command(label="지금 새로고침", command=self.refresh)
        m.add_command(label="Antigravity 실행해서 조회", command=lambda: self.refresh(force_antigravity=True))
        m.add_command(label="상세 패널 고정/해제", command=self._toggle_pin)
        m.add_command(label="위치 초기화", command=self._reset_pos)
        m.add_separator()
        m.add_command(label="종료", command=self.root.destroy)

    def _reset_pos(self):
        self.cfg.pop("tray_offset", None)
        self._save_cfg()
        self._place_bar()
        self.draw_bar()

    # ------------------------------------------------------------ data
    def refresh(self, force_antigravity=False):
        if self.loading:
            return
        self.loading = True
        self._redraw()

        def work():
            try:
                data = collector.collect_all(force_antigravity=force_antigravity)
            except Exception:
                data = self.data
            self.root.after(0, self._on_data, data)

        threading.Thread(target=work, daemon=True).start()

    def _on_data(self, data):
        self.data, self.loading = data, False
        self._redraw()
        if self.timer:
            self.root.after_cancel(self.timer)
        self.timer = self.root.after(REFRESH_SEC * 1000, self.refresh)

    def _redraw(self):
        self.draw_bar()
        if self.panel.winfo_viewable():
            self.draw_panel()
            self._position_panel()

    def _tick(self):
        self._redraw()
        self.root.after(TICK_MS, self._tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    # 중복 실행 방지: 이미 떠 있으면 조용히 종료 (시작 프로그램 + 수동 실행이 겹칠 때)
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AITokenInfoWidget")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        raise SystemExit
    App().run()
