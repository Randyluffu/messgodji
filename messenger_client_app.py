#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godji Messenger — КЛИЕНТСКОЕ приложение (игровые ПК).

Первый запуск: сама определяет имя ПК, сама находит сервер администратора
в сети (UDP-маячок, вводить IP не нужно), регистрирует автозапуск и
правило брандмауэра (один раз подтверждение Windows). Дальше — обычный
тихий запуск в фоне.

Pause/Break — открыть/скрыть чат. Если чат скрыт и приходит сообщение —
не разворачивается поверх игры, а показывается короткое уведомление (7 сек)
со звуком и подсказкой "Нажмите Pause/Break".

Сборка в exe: см. build_exe.bat в комплекте.
Зависимости (только на этапе сборки): pip install requests pillow
"""
import base64
import ctypes
from ctypes import wintypes
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import winsound
import tkinter as tk
from tkinter import filedialog

import requests
from PIL import Image, ImageGrab, ImageTk

APP_NAME = 'GodjiMessengerClient'
HTTP_PORT = 6070
BEACON_PORT = 47990
HEARTBEAT_INTERVAL = 5
POLL_INTERVAL = 2
READ_STATE_INTERVAL = 3
HTTP_TIMEOUT = 3
TOAST_MS = 7000
MAX_IMAGE_SIDE = 900
JPEG_QUALITY = 78

CONFIG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'GodjiMessenger')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'client_config.json')

# ── Тёмная тема в стиле Godji ────────────────────────────────
BG = '#0f1020'
HEADER_BG = '#161729'
ACCENT = '#cc0001'
ACCENT_HOVER = '#a80001'
BUBBLE_ADMIN_BG = '#1f2137'
BUBBLE_ME_BG = '#cc0001'
TEXT_LIGHT = '#eef0f5'
TEXT_MUTED = '#8b8fa3'
TEXT_READ = '#4fc3f7'
ENTRY_BG = '#1c1d33'
SYSTEM_TEXT = '#e0a800'

EMOJI_SET = ['😀', '😂', '😉', '😎', '👍', '👎', '🔥', '💯', '🙏', '😢',
             '😡', '❤️', '🎮', '⚡', '✅', '❌', '⏰', '💰', '🖥️', '❓']

_session = requests.Session()
_session.trust_env = False
_session.proxies = {'http': None, 'https': None}


# ───────────────────────── конфиг ─────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
    except Exception as e:
        print('[messenger] Не удалось сохранить конфиг:', e)


def get_self_path():
    if getattr(sys, 'frozen', False):
        return sys.executable
    return None


def detect_pc_name():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        last = int(ip.split('.')[-1])
        if 201 <= last <= 241:
            return '%02d' % (last - 200)
    except Exception:
        pass
    return socket.gethostname()


PC_NAME = detect_pc_name()


# ───────────────────────── автозапуск / брандмауэр ─────────────────────────
def install_autostart():
    exe = get_self_path()
    if not exe:
        print('[messenger] Автозапуск пропущен (режим разработки, не exe)')
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r'Software\Microsoft\Windows\CurrentVersion\Run',
                              0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
        winreg.CloseKey(key)
        print('[messenger] Автозапуск зарегистрирован')
    except Exception as e:
        print('[messenger] Ошибка автозапуска:', e)


def add_firewall_rule():
    exe = get_self_path() or sys.executable
    args = ('advfirewall firewall add rule name="Godji Messenger Client" '
            'dir=in action=allow protocol=UDP localport=%d program="%s" '
            'enable=yes profile=any') % (BEACON_PORT, exe)
    try:
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'netsh', args, None, 0)
        print('[messenger] Запрос на правило брандмауэра отправлен')
    except Exception as e:
        print('[messenger] Ошибка брандмауэра:', e)


# ───────────────────────── автообнаружение сервера ─────────────────────────
_admin_host = None
_admin_lock = threading.Lock()


def get_base_url():
    with _admin_lock:
        host = _admin_host
    return ('http://%s:%d' % (host, HTTP_PORT)) if host else None


def _set_admin_host(host):
    global _admin_host
    with _admin_lock:
        changed = _admin_host != host
        _admin_host = host
    if changed:
        print('[messenger] Сервер администратора найден: %s' % host)
        cfg = load_config()
        cfg['admin_host'] = host
        save_config(cfg)


def discover_once(timeout):
    """Разовый поиск с ожиданием — используется в мастере первого запуска."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('', BEACON_PORT))
    except Exception as e:
        print('[messenger] Не удалось слушать порт обнаружения:', e)
        return None
    s.settimeout(timeout)
    try:
        data, addr = s.recvfrom(2048)
        info = json.loads(data.decode('utf-8'))
        if info.get('service') == 'godji_messenger':
            return addr[0]
    except Exception:
        pass
    finally:
        s.close()
    return None


def discovery_loop():
    """Постоянный фоновый поиск — если сервер сменит IP, клиент сам это заметит."""
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('', BEACON_PORT))
            s.settimeout(6)
            data, addr = s.recvfrom(2048)
            info = json.loads(data.decode('utf-8'))
            if info.get('service') == 'godji_messenger':
                _set_admin_host(addr[0])
        except socket.timeout:
            pass
        except Exception as e:
            time.sleep(2)
        finally:
            try:
                s.close()
            except Exception:
                pass


# ───────────────────────── Win32 хелперы (без кражи фокуса у игры) ─────────────────────────
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WM_HOTKEY = 0x0312
VK_PAUSE = 0x13
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 1


def make_noactivate(tk_root):
    """Делает окно неактивируемым — не крадёт фокус и не сворачивает игру
    в полноэкранном режиме. Подходит для уведомлений (не для окна с вводом текста)."""
    try:
        hwnd = tk_root.winfo_id()
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception as e:
        print('[messenger] noactivate error', e)


def hotkey_loop(callback):
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, VK_PAUSE):
        print('[messenger] !!! Не удалось зарегистрировать Pause/Break — занята другой программой')
        return
    print('[messenger] Горячая клавиша Pause/Break зарегистрирована')
    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                callback()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


def play_notify_sound():
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ───────────────────────── Уведомление (не крадёт фокус) ─────────────────────────
class Toast:
    def __init__(self, parent_tk):
        self.parent = parent_tk
        self._win = None

    def show(self, text):
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass

        w, h = 300, 96
        win = tk.Toplevel(self.parent)
        self._win = win
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=HEADER_BG)
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = sw - w - 20
        y = sh - h - 60
        win.geometry('%dx%d+%d+%d' % (w, h, x, y))

        tk.Frame(win, bg=ACCENT, width=4).pack(side='left', fill='y')
        body = tk.Frame(win, bg=HEADER_BG)
        body.pack(side='left', fill='both', expand=True, padx=10, pady=8)

        head = tk.Frame(body, bg=HEADER_BG)
        head.pack(fill='x')
        tk.Label(head, text='Админ клуба', bg=HEADER_BG, fg=ACCENT,
                  font=('Segoe UI', 9, 'bold')).pack(side='left')

        msg = tk.Label(body, text=text, bg=HEADER_BG, fg=TEXT_LIGHT, font=('Segoe UI', 9),
                        wraplength=250, justify='left', anchor='w')
        msg.pack(fill='x', pady=(2, 6))

        hint = tk.Frame(body, bg=HEADER_BG)
        hint.pack(fill='x')
        keycap = tk.Label(hint, text='Pause / Break', bg='#232544', fg=TEXT_MUTED,
                            font=('Segoe UI', 7, 'bold'), padx=6, pady=2,
                            highlightthickness=1, highlightbackground='#33355a')
        keycap.pack(side='left')
        tk.Label(hint, text=' чтобы открыть чат', bg=HEADER_BG, fg=TEXT_MUTED,
                  font=('Segoe UI', 7)).pack(side='left')

        make_noactivate(win)
        play_notify_sound()

        def close():
            try:
                win.destroy()
            except Exception:
                pass
            if self._win is win:
                self._win = None

        win.after(TOAST_MS, close)


# ───────────────────────── Окно чата ─────────────────────────
class ChatWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        w, h = 340, 480
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, sw - w - 30)
        y = max(0, sh - h - 70)
        self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))
        self.root.attributes('-topmost', True)
        self.visible = True
        self._own_bubbles = []      # [(msg_id, meta_label)]
        self._image_refs = []       # чтобы PhotoImage не собрал GC
        self._drag = {'x': 0, 'y': 0}

        # ── Шапка (своя, без белых системных рамок) ──
        header = tk.Frame(self.root, bg=HEADER_BG, height=46)
        header.pack(side='top', fill='x')
        header.pack_propagate(False)
        left = tk.Frame(header, bg=HEADER_BG)
        left.pack(side='left', padx=14)
        tk.Label(left, text='●', bg=HEADER_BG, fg=ACCENT, font=('Segoe UI', 12)).pack(side='left', padx=(0, 6))
        tk.Label(left, text='Админ клуба', bg=HEADER_BG, fg=TEXT_LIGHT,
                  font=('Segoe UI', 11, 'bold')).pack(side='left')
        close_btn = tk.Label(header, text='—', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 12),
                               cursor='hand2', padx=12)
        close_btn.pack(side='right')
        close_btn.bind('<Button-1>', lambda e: self.hide())
        close_btn.bind('<Enter>', lambda e: close_btn.config(fg=TEXT_LIGHT))
        close_btn.bind('<Leave>', lambda e: close_btn.config(fg=TEXT_MUTED))

        for w_ in (header, left):
            w_.bind('<Button-1>', self._drag_start)
            w_.bind('<B1-Motion>', self._drag_move)

        # ── Прокручиваемая область сообщений ──
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(side='top', fill='both', expand=True)
        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient='vertical', command=self.canvas.yview)
        self.msg_frame = tk.Frame(self.canvas, bg=BG)
        self.msg_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self._canvas_win = self.canvas.create_window((0, 0), window=self.msg_frame, anchor='nw')
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfig(self._canvas_win, width=e.width))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.msg_frame)

        # ── Панель эмодзи (скрыта по умолчанию) ──
        self._emoji_panel = None

        # ── Нижняя панель ввода (закреплена снизу — всегда видна) ──
        entry_frame = tk.Frame(self.root, bg=HEADER_BG)
        entry_frame.pack(side='bottom', fill='x')
        inner = tk.Frame(entry_frame, bg=HEADER_BG)
        inner.pack(fill='x', padx=10, pady=10)

        emoji_btn = tk.Label(inner, text='🙂', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 13),
                               cursor='hand2', padx=4)
        emoji_btn.pack(side='left')
        emoji_btn.bind('<Button-1>', lambda e: self.toggle_emoji_panel())

        attach_btn = tk.Label(inner, text='📎', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 12),
                                cursor='hand2', padx=4)
        attach_btn.pack(side='left')
        attach_btn.bind('<Button-1>', lambda e: self.attach_image())

        self.entry = tk.Entry(inner, font=('Segoe UI', 10), bg=ENTRY_BG, fg=TEXT_LIGHT,
                               insertbackground=TEXT_LIGHT, relief='flat', bd=0,
                               highlightthickness=1, highlightbackground='#2a2c42', highlightcolor=ACCENT)
        self.entry.pack(side='left', fill='x', expand=True, ipady=7, padx=8)
        self.entry.bind('<Return>', self.send_text)
        self.entry.bind('<Control-v>', self._on_paste)
        self.entry.bind('<Control-KeyPress-v>', self._on_paste)

        send_btn = tk.Label(inner, text='➤', bg=ACCENT, fg='#fff', font=('Segoe UI', 12, 'bold'),
                              cursor='hand2', width=3)
        send_btn.pack(side='left')
        send_btn.bind('<Button-1>', self.send_text)
        send_btn.bind('<Enter>', lambda e: send_btn.config(bg=ACCENT_HOVER))
        send_btn.bind('<Leave>', lambda e: send_btn.config(bg=ACCENT))

        self.root.bind('<Button-1>', lambda e: self.entry.focus_set(), add='+')

        self.toast = Toast(self.root)
        self.hide()

    # ── перетаскивание безрамочного окна ──
    def _drag_start(self, event):
        self._drag['x'] = event.x
        self._drag['y'] = event.y

    def _drag_move(self, event):
        x = self.root.winfo_pointerx() - self._drag['x']
        y = self.root.winfo_pointery() - self._drag['y']
        self.root.geometry('+%d+%d' % (x, y))

    # ── прокрутка колесом (рекурсивный бинд — иначе не работает над дочерними виджетами) ──
    def _bind_wheel(self, widget):
        widget.bind('<MouseWheel>', self._on_wheel)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

    def _scroll_to_end(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    # ── эмодзи-панель ──
    def toggle_emoji_panel(self):
        if self._emoji_panel is not None:
            self._emoji_panel.destroy()
            self._emoji_panel = None
            return
        panel = tk.Frame(self.root, bg=HEADER_BG, highlightthickness=1, highlightbackground='#2a2c42')
        self._emoji_panel = panel
        panel.place(relx=0, rely=1.0, y=-56, x=8, anchor='sw')
        cols = 8
        for i, em in enumerate(EMOJI_SET):
            b = tk.Label(panel, text=em, bg=HEADER_BG, font=('Segoe UI Emoji', 13), cursor='hand2', padx=4, pady=2)
            b.grid(row=i // cols, column=i % cols)
            b.bind('<Button-1>', lambda e, ch=em: self._insert_emoji(ch))

    def _insert_emoji(self, ch):
        self.entry.insert('insert', ch)
        if self._emoji_panel is not None:
            self._emoji_panel.destroy()
            self._emoji_panel = None
        self.entry.focus_set()

    # ── отрисовка сообщений ──
    def append_system(self, text):
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=6, padx=10)
        lbl = tk.Label(row, text=text, bg=BG, fg=SYSTEM_TEXT, font=('Segoe UI', 8, 'italic'),
                        wraplength=260, justify='center')
        lbl.pack(anchor='center')
        self._bind_wheel(row); self._bind_wheel(lbl)
        self._scroll_to_end()

    def append_text(self, text, mine=False, msg_id=None):
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=3, padx=10)
        bubble_bg = BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG
        bubble_fg = '#ffffff' if mine else TEXT_LIGHT
        bubble = tk.Label(row, text=text, bg=bubble_bg, fg=bubble_fg, font=('Segoe UI', 10),
                           wraplength=210, justify='left', padx=10, pady=7, bd=0)
        bubble.pack(side='right' if mine else 'left')
        meta = tk.Label(row, text=time.strftime('%H:%M') + (' ✓' if mine else ''),
                         bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w',
                   padx=(0, 2) if mine else (2, 0))
        if mine and msg_id is not None:
            self._own_bubbles.append((msg_id, meta))
        self._bind_wheel(row); self._bind_wheel(bubble); self._bind_wheel(meta)
        self._scroll_to_end()

    def append_image(self, pil_img, mine=False, msg_id=None):
        thumb = pil_img.copy()
        thumb.thumbnail((220, 220))
        photo = ImageTk.PhotoImage(thumb)
        self._image_refs.append(photo)

        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=3, padx=10)
        holder = tk.Label(row, image=photo, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG,
                            cursor='hand2', bd=0)
        holder.pack(side='right' if mine else 'left')
        holder.bind('<Button-1>', lambda e: self._open_full(pil_img))

        meta = tk.Label(row, text=time.strftime('%H:%M') + (' ✓' if mine else ''),
                         bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w',
                   padx=(0, 2) if mine else (2, 0))
        if mine and msg_id is not None:
            self._own_bubbles.append((msg_id, meta))
        self._bind_wheel(row); self._bind_wheel(holder); self._bind_wheel(meta)
        self._scroll_to_end()

    def _open_full(self, pil_img):
        try:
            fd, path = tempfile.mkstemp(suffix='.png')
            os.close(fd)
            pil_img.save(path)
            os.startfile(path)
        except Exception as e:
            self.append_system('Не удалось открыть изображение: %s' % e)

    def update_read_status(self, read_admin_id):
        ts = time.strftime('%H:%M')
        for msg_id, label in self._own_bubbles:
            try:
                if msg_id <= read_admin_id:
                    label.config(text=ts + ' ✓✓', fg=TEXT_READ)
                else:
                    label.config(text=ts + ' ✓', fg=TEXT_MUTED)
            except Exception:
                pass

    # ── отправка ──
    def send_text(self, event=None):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, 'end')
        base = get_base_url()
        if not base:
            self.append_system('Сервер администратора ещё не найден в сети…')
            return
        try:
            r = _session.post(base + '/send', json={'pc': PC_NAME, 'from': 'client', 'type': 'text', 'text': text},
                               timeout=HTTP_TIMEOUT)
            data = r.json() if r.status_code == 200 else {}
            self.append_text(text, mine=True, msg_id=data.get('id'))
        except Exception:
            self.append_system('Нет связи с сервером администратора')

    def send_image(self, pil_img):
        base = get_base_url()
        if not base:
            self.append_system('Сервер администратора ещё не найден в сети…')
            return
        img = pil_img.convert('RGB')
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        try:
            r = _session.post(base + '/send', json={'pc': PC_NAME, 'from': 'client', 'type': 'image', 'text': b64},
                               timeout=HTTP_TIMEOUT + 5)
            data = r.json() if r.status_code == 200 else {}
            self.append_image(img, mine=True, msg_id=data.get('id'))
        except Exception:
            self.append_system('Не удалось отправить изображение')

    def attach_image(self):
        path = filedialog.askopenfilename(
            title='Выбери изображение',
            filetypes=[('Изображения', '*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp')])
        if not path:
            return
        try:
            img = Image.open(path)
            self.send_image(img)
        except Exception as e:
            self.append_system('Не удалось открыть файл: %s' % e)

    def _on_paste(self, event=None):
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            clip = None
        if isinstance(clip, Image.Image):
            self.send_image(clip)
            return 'break'
        return None  # обычная вставка текста сработает сама

    # ── видимость ──
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', False)
        self.root.attributes('-topmost', True)
        self.root.focus_force()
        self.entry.focus_set()
        self.visible = True

    def hide(self):
        if self._emoji_panel is not None:
            self._emoji_panel.destroy()
            self._emoji_panel = None
        self.root.withdraw()
        self.visible = False

    def _toggle_impl(self):
        self.hide() if self.visible else self.show()

    def toggle(self):
        self.root.after(0, self._toggle_impl)

    def notify(self, text):
        self.root.after(0, self.toast.show, text)


# ───────────────────────── фоновые циклы ─────────────────────────
_last_msg_id = 0
_last_heartbeat_ok = None


def heartbeat_loop():
    global _last_heartbeat_ok
    while True:
        base = get_base_url()
        if not base:
            time.sleep(HEARTBEAT_INTERVAL)
            continue
        ok = False
        try:
            r = _session.post(base + '/heartbeat', json={'pc': PC_NAME}, timeout=HTTP_TIMEOUT)
            ok = r.status_code == 200
            if not ok and _last_heartbeat_ok is not False:
                print('[messenger] сервер ответил кодом %s' % r.status_code)
        except Exception as e:
            if _last_heartbeat_ok is not False:
                print('[messenger] НЕТ СВЯЗИ с сервером %s : %s' % (base, e))
        if ok and _last_heartbeat_ok is not True:
            print('[messenger] Связь установлена, heartbeat как ПК "%s"' % PC_NAME)
        _last_heartbeat_ok = ok
        time.sleep(HEARTBEAT_INTERVAL)


def poll_loop(win):
    global _last_msg_id
    while True:
        base = get_base_url()
        if not base:
            time.sleep(POLL_INTERVAL)
            continue
        try:
            r = _session.get(base + '/messages', params={'pc': PC_NAME, 'since': _last_msg_id}, timeout=HTTP_TIMEOUT)
            for m in r.json():
                _last_msg_id = max(_last_msg_id, m['id'])
                if m['from'] != 'admin':
                    continue
                is_image = m.get('type') == 'image'
                if is_image:
                    try:
                        raw = base64.b64decode(m['text'])
                        img = Image.open(io.BytesIO(raw))
                        win.root.after(0, win.append_image, img, False, m['id'])
                    except Exception:
                        win.root.after(0, win.append_system, 'Не удалось загрузить изображение')
                else:
                    win.root.after(0, win.append_text, m['text'], False, m['id'])

                if win.visible:
                    pass  # уже видно — ничего дополнительно делать не нужно
                else:
                    # НЕ разворачиваем окно поверх игры — только ненавязчивое уведомление
                    preview = 'Изображение' if is_image else m['text']
                    win.notify(preview)

            if win.visible and _last_msg_id > 0:
                try:
                    _session.post(base + '/read', json={'pc': PC_NAME, 'side': 'client', 'upto': _last_msg_id},
                                   timeout=HTTP_TIMEOUT)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


def read_state_loop(win):
    while True:
        base = get_base_url()
        if base:
            try:
                r = _session.get(base + '/read_state', params={'pc': PC_NAME}, timeout=HTTP_TIMEOUT)
                data = r.json()
                win.root.after(0, win.update_read_status, int(data.get('readAdmin', 0)))
            except Exception:
                pass
        time.sleep(READ_STATE_INTERVAL)


# ───────────────────────── мастер первого запуска ─────────────────────────
def run_wizard(on_done):
    wiz = tk.Tk()
    wiz.overrideredirect(True)
    wiz.configure(bg=BG)
    w, h = 380, 200
    sw, sh = wiz.winfo_screenwidth(), wiz.winfo_screenheight()
    wiz.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
    wiz.attributes('-topmost', True)

    tk.Frame(wiz, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(wiz, text='Godji Messenger', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 14, 'bold')).pack(pady=(22, 2))
    tk.Label(wiz, text='Первый запуск — настройка', bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 9)).pack()
    status = tk.Label(wiz, text='Подготовка…', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 9), wraplength=320)
    status.pack(pady=18)

    def step_detect():
        print('[messenger] ПК определён как "%s"' % PC_NAME)

    def step_discover():
        host = discover_once(timeout=8)
        if host:
            _set_admin_host(host)

    steps = [
        ('Определяем этот ПК (%s)…' % PC_NAME, step_detect),
        ('Ищем сервер администратора в сети…', step_discover),
        ('Регистрируем автозапуск…', install_autostart),
        ('Настраиваем брандмауэр (может появиться запрос Windows)…', add_firewall_rule),
    ]
    idx = [0]

    def next_step():
        if idx[0] >= len(steps):
            if get_base_url():
                status.config(text='Готово! Сервер найден.')
            else:
                status.config(text='Готово! Сервер пока не найден — попробую ещё раз в фоне.')
            wiz.update()
            cfg = load_config()
            cfg['configured'] = True
            cfg['pc_name'] = PC_NAME
            save_config(cfg)
            wiz.after(1200, lambda: (wiz.destroy(), on_done()))
            return
        text, fn = steps[idx[0]]
        idx[0] += 1
        status.config(text=text)
        wiz.update()
        try:
            fn()
        except Exception as e:
            print('[messenger] Ошибка шага настройки:', e)
        wiz.after(300, next_step)

    wiz.after(400, next_step)
    wiz.mainloop()


def start_app():
    win = ChatWindow()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=poll_loop, args=(win,), daemon=True).start()
    threading.Thread(target=read_state_loop, args=(win,), daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=hotkey_loop, args=(win.toggle,), daemon=True).start()
    win.root.mainloop()


def main():
    print('=' * 50)
    print('[messenger] ПК: "%s"' % PC_NAME)
    print('=' * 50)
    cfg = load_config()
    if cfg.get('admin_host'):
        _set_admin_host(cfg['admin_host'])  # используем кэш, пока идёт фоновый поиск
    if cfg.get('configured'):
        start_app()
    else:
        run_wizard(start_app)


if __name__ == '__main__':
    main()
