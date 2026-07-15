#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godji Messenger — КЛИЕНТСКОЕ приложение (игровые ПК).

Первый запуск: сама определяет имя ПК, сама находит сервер администратора
в сети (UDP-маячок по всем сетевым адаптерам), регистрирует автозапуск и
правило брандмауэра. Дальше — обычный тихий запуск в фоне.

Pause/Break — открыть/скрыть чат.

Сборка в exe: build_exe.bat (локально) или .github/workflows/build-exe.yml.
Зависимости (только на этапе сборки): pip install requests pillow pywin32 cryptography
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
import tkinter.font as tkfont
import tkinter.ttk as ttk
from tkinter import filedialog

import requests
from PIL import Image, ImageGrab, ImageTk
from cryptography.fernet import Fernet, InvalidToken

try:
    import win32clipboard
except Exception:
    win32clipboard = None

APP_NAME = 'GodjiMessengerClient'
HTTP_PORT = 6070
BEACON_PORT = 47990
HEARTBEAT_INTERVAL = 5
POLL_INTERVAL = 2
READ_STATE_INTERVAL = 3
SETTINGS_INTERVAL = 5
NICK_INTERVAL = 20
HTTP_TIMEOUT = 3
TOAST_MS = 7000
WELCOME_TOAST_MS = 15000
MAX_TOASTS_VISIBLE = 4
MAX_IMAGE_SIDE = 900
JPEG_QUALITY = 78
RADIUS = 14

SHARED_KEY = b'uus8GixjnYZbgjTRaHdUz3RSrHmgxIsoOfUMxL8Cufg='
_fernet = Fernet(SHARED_KEY)

CONFIG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'GodjiMessenger')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'client_config.json')

BG = '#181113'
HEADER_BG = '#221417'
PANEL_BORDER = '#3a2226'
ACCENT = '#d4172a'
ACCENT_HOVER = '#b01120'
BUBBLE_ADMIN_BG = '#2b1c1f'
BUBBLE_ME_BG = '#d4172a'
TEXT_LIGHT = '#f5eeee'
TEXT_MUTED = '#a68d8f'
TEXT_READ = '#f2a33c'
ENTRY_BG = '#241619'
SYSTEM_TEXT = '#e0a800'
ONLINE_GREEN = '#3ecf5e'
ONLINE_RED = '#e0393f'

EMOJI_SET = ['😀','😂','😉','😎','🙂','😅','🥲','😢','😡','🤔',
             '👍','👎','🙏','👏','🔥','💯','❤️','✅','❌','⏰',
             '💰','🎮','🖥️','❓','😴','🥳']

_session = requests.Session()
_session.trust_env = False
_session.proxies = {'http': None, 'https': None}

_settings_cache = {'notifySound': True, 'showOnlineIndicator': True}
_mute_state = {'mutedUntil': 0.0}
_manual_nickname = [None]


# ───────────────────────── DPI / скруглённые окна ─────────────────────────
def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def apply_rounded_corners(win):
    try:
        win.update_idletasks()
        hwnd = win.winfo_id()
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        value = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                                     ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def setup_ttk_style():
    style = ttk.Style()
    try:
        style.theme_use('clam')
    except Exception:
        pass
    style.configure('Godji.Vertical.TScrollbar', background=PANEL_BORDER, troughcolor=BG,
                     bordercolor=BG, arrowcolor=TEXT_MUTED, relief='flat', gripcount=0, width=10)
    style.map('Godji.Vertical.TScrollbar', background=[('active', ACCENT), ('!active', PANEL_BORDER)])


def enc_text(plain):
    try:
        return _fernet.encrypt(plain.encode('utf-8')).decode('ascii')
    except Exception:
        return plain


def dec_text(cipher):
    try:
        return _fernet.decrypt(cipher.encode('ascii')).decode('utf-8')
    except (InvalidToken, Exception):
        return cipher


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


def display_name():
    if _manual_nickname[0]:
        return '%s — ПК %s' % (_manual_nickname[0], PC_NAME)
    nick = _settings_cache.get('nickname')
    if nick:
        return '%s — ПК %s' % (nick, PC_NAME)
    return 'ПК ' + PC_NAME


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


def verify_firewall_rule():
    import subprocess
    try:
        r = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule',
                             'name=Godji Messenger Client'], capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and 'No rules match' not in r.stdout
    except Exception:
        return False


# ───────────────────────── автообнаружение сервера (по всем сетевым адаптерам) ─────────────────────────
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


def _listen_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('', BEACON_PORT))
    except Exception as e:
        print('[messenger] Не удалось слушать порт обнаружения:', e)
        return None
    return s


def discover_once(timeout):
    s = _listen_socket()
    if not s:
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
    while True:
        s = _listen_socket()
        if not s:
            time.sleep(5)
            continue
        s.settimeout(6)
        try:
            data, addr = s.recvfrom(2048)
            info = json.loads(data.decode('utf-8'))
            if info.get('service') == 'godji_messenger':
                _set_admin_host(addr[0])
        except socket.timeout:
            pass
        except Exception:
            time.sleep(2)
        finally:
            try:
                s.close()
            except Exception:
                pass


# ───────────────────────── Win32 хелперы ─────────────────────────
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WM_HOTKEY = 0x0312
VK_PAUSE = 0x13
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 1


def make_noactivate(tk_root):
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


def is_muted():
    return time.time() < _mute_state.get('mutedUntil', 0)


def play_notify_sound():
    if not _settings_cache.get('notifySound', True) or is_muted():
        return
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def copy_text_to_clipboard(root, text):
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
    except Exception:
        pass


def copy_image_to_clipboard(pil_img):
    if win32clipboard is None:
        return False
    try:
        buf = io.BytesIO()
        pil_img.convert('RGB').save(buf, 'BMP')
        data = buf.getvalue()[14:]
        buf.close()
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


# ───────────────────────── единая система скруглённых форм ─────────────────────────
def _rounded_points(x1, y1, x2, y2, r):
    return [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2, x2-r,y2, x1+r,y2,
            x1,y2, x1,y2-r, x1,y1+r, x1,y1]


def make_bubble(parent, text, bg, fg, wrap_px=220, font=('Segoe UI', 10), pad_x=12, pad_y=9, radius=RADIUS):
    fnt = tkfont.Font(family=font[0], size=int(font[1]))
    probe = tk.Canvas(parent)
    item = probe.create_text(0, 0, text=text, font=fnt, width=wrap_px, anchor='nw')
    bbox = probe.bbox(item)
    probe.destroy()
    tw = (bbox[2] - bbox[0]) if bbox else 10
    th = (bbox[3] - bbox[1]) if bbox else 16
    w = tw + pad_x * 2
    h = th + pad_y * 2
    c = tk.Canvas(parent, width=w, height=h, bg=parent['bg'], highlightthickness=0, bd=0)
    c.create_polygon(_rounded_points(1, 1, w-1, h-1, radius), smooth=True, fill=bg, outline=bg)
    c.create_text(pad_x, pad_y, text=text, font=fnt, fill=fg, width=wrap_px, anchor='nw')
    return c


def rounded_button(parent, text, command, bg=ACCENT, hover=ACCENT_HOVER, fg='#fff',
                     w=44, h=34, font=('Segoe UI', 11, 'bold'), radius=RADIUS):
    """Единая скруглённая кнопка (вместо плоских Label-прямоугольников)."""
    c = tk.Canvas(parent, width=w, height=h, bg=parent['bg'], highlightthickness=0, bd=0, cursor='hand2')
    shape = c.create_polygon(_rounded_points(1, 1, w-1, h-1, radius), smooth=True, fill=bg, outline=bg)
    label = c.create_text(w/2, h/2, text=text, fill=fg, font=font)

    def on_click(e):
        command()
    def on_enter(e):
        c.itemconfig(shape, fill=hover)
    def on_leave(e):
        c.itemconfig(shape, fill=bg)

    c.bind('<Button-1>', on_click)
    c.bind('<Enter>', on_enter)
    c.bind('<Leave>', on_leave)
    c._shape = shape
    c._label = label
    return c


def rounded_pill(parent, text, command, bg=BUBBLE_ADMIN_BG, hover=ACCENT, fg=TEXT_LIGHT,
                   font=('Segoe UI', 9), pad_x=12, pad_y=6, radius=10):
    fnt = tkfont.Font(family=font[0], size=int(font[1]))
    probe = tk.Canvas(parent)
    item = probe.create_text(0, 0, text=text, font=fnt, anchor='nw')
    bbox = probe.bbox(item)
    probe.destroy()
    tw = (bbox[2]-bbox[0]) if bbox else 10
    th = (bbox[3]-bbox[1]) if bbox else 14
    w, h = tw+pad_x*2, th+pad_y*2
    c = tk.Canvas(parent, width=w, height=h, bg=parent['bg'], highlightthickness=0, bd=0, cursor='hand2')
    shape = c.create_polygon(_rounded_points(1, 1, w-1, h-1, radius), smooth=True, fill=bg, outline=bg)
    c.create_text(w/2, h/2, text=text, fill=fg, font=fnt)
    c.bind('<Button-1>', lambda e: command())
    c.bind('<Enter>', lambda e: c.itemconfig(shape, fill=hover))
    c.bind('<Leave>', lambda e: c.itemconfig(shape, fill=bg))
    return c


def build_message_menu(root, text=None, pil_img=None):
    menu = tk.Menu(root, tearoff=0, bg=HEADER_BG, fg=TEXT_LIGHT, activebackground=ACCENT,
                    activeforeground='#fff', bd=0, relief='flat', font=('Segoe UI', 9))
    if text is not None:
        menu.add_command(label='Скопировать текст', command=lambda: copy_text_to_clipboard(root, text))
    if pil_img is not None:
        menu.add_command(label='Скопировать изображение', command=lambda: copy_image_to_clipboard(pil_img))
        def save_as():
            path = filedialog.asksaveasfilename(defaultextension='.png',
                                                  filetypes=[('PNG', '*.png'), ('JPEG', '*.jpg')])
            if path:
                try:
                    pil_img.save(path)
                except Exception:
                    pass
        menu.add_command(label='Сохранить как…', command=save_as)
    return menu


# ───────────────────────── стек уведомлений (максимум 4, дальше — колёсиком) ─────────────────────────
class ToastStack:
    CARD_H = 84
    GAP = 8
    WIDTH = 320

    def __init__(self, parent_tk):
        self.parent = parent_tk
        self.win = None
        self.canvas = None
        self.inner = None
        self._cards = []

    def _ensure_window(self):
        if self.win is not None:
            return
        win = tk.Toplevel(self.parent)
        self.win = win
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=BG)
        self.canvas = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0, width=self.WIDTH)
        self.canvas.pack()
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor='nw')
        self.canvas.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))
        self._reposition()
        make_noactivate(win)

    def _reposition(self):
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        h = max(min(len(self._cards), MAX_TOASTS_VISIBLE), 1) * (self.CARD_H + self.GAP)
        self.win.geometry('%dx%d+%d+%d' % (self.WIDTH, h, sw - self.WIDTH - 22, sh - h - 60))
        self.canvas.configure(height=h)

    def _rebuild(self):
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        self._reposition()

    def show(self, title, text, on_click=None):
        self._ensure_window()
        card = tk.Frame(self.inner, bg=HEADER_BG, highlightthickness=1, highlightbackground=PANEL_BORDER,
                          width=self.WIDTH, height=self.CARD_H)
        card.pack_propagate(False)
        card.pack(fill='x', pady=(0, self.GAP))
        tk.Frame(card, bg=ACCENT, width=4).pack(side='left', fill='y')
        body = tk.Frame(card, bg=HEADER_BG)
        body.pack(side='left', fill='both', expand=True, padx=10, pady=8)
        tk.Label(body, text=title, bg=HEADER_BG, fg=ACCENT, font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        tk.Label(body, text=text, bg=HEADER_BG, fg=TEXT_LIGHT, font=('Segoe UI', 9),
                  wraplength=250, justify='left', anchor='w').pack(fill='x', pady=(2, 0))

        def close(*_):
            try:
                card.destroy()
            except Exception:
                pass
            self._cards[:] = [c for c in self._cards if c[0] is not card]
            if self._cards:
                self._rebuild()
            elif self.win:
                self.win.destroy()
                self.win = None

        def click(*_):
            close()
            if on_click:
                on_click()

        card.bind('<Button-1>', click)
        for w_ in (body,) + tuple(body.winfo_children()):
            w_.bind('<Button-1>', click)

        after_id = card.after(TOAST_MS, close)
        self._cards.append((card, after_id))
        self._rebuild()
        play_notify_sound()


class Toast:
    def __init__(self, parent_tk):
        self.stack = ToastStack(parent_tk)

    def show(self, text, title='Админ клуба', on_click=None):
        self.stack.show(title, text, on_click)


# ───────────────────────── Окно чата ─────────────────────────
class ChatWindow:
    def __init__(self):
        self.root = tk.Tk()
        setup_ttk_style()
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        w, h = 360, 520
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, sw - w - 30)
        y = max(0, sh - h - 70)
        self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))
        self.root.attributes('-topmost', True)
        self.visible = True
        self._own_bubbles = []
        self._image_refs = []
        self._drag = {'x': 0, 'y': 0}
        self._emoji_panel = None

        header = tk.Frame(self.root, bg=HEADER_BG, height=50)
        header.pack(side='top', fill='x')
        header.pack_propagate(False)
        left = tk.Frame(header, bg=HEADER_BG)
        left.pack(side='left', padx=16)
        self.status_dot = tk.Label(left, text='●', bg=HEADER_BG, fg=ONLINE_RED, font=('Segoe UI', 11))
        self.status_dot.pack(side='left', padx=(0, 7))
        self.title_lbl = tk.Label(left, text='Админ клуба', bg=HEADER_BG, fg=TEXT_LIGHT,
                                    font=('Segoe UI', 11, 'bold'))
        self.title_lbl.pack(side='left')
        close_btn = tk.Label(header, text='—', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 13),
                               cursor='hand2', padx=14)
        close_btn.pack(side='right')
        close_btn.bind('<Button-1>', lambda e: self.hide())
        close_btn.bind('<Enter>', lambda e: close_btn.config(fg=TEXT_LIGHT))
        close_btn.bind('<Leave>', lambda e: close_btn.config(fg=TEXT_MUTED))

        for w_ in (header, left):
            w_.bind('<Button-1>', self._drag_start)
            w_.bind('<B1-Motion>', self._drag_move)

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(side='top', fill='both', expand=True)
        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(outer, orient='vertical', command=self.canvas.yview, style='Godji.Vertical.TScrollbar')
        self.msg_frame = tk.Frame(self.canvas, bg=BG)
        self.msg_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self._canvas_win = self.canvas.create_window((0, 0), window=self.msg_frame, anchor='nw')
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfig(self._canvas_win, width=e.width))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.msg_frame)
        self._bind_wheel(outer)

        entry_frame = tk.Frame(self.root, bg=HEADER_BG)
        entry_frame.pack(side='bottom', fill='x')
        tk.Frame(entry_frame, bg=PANEL_BORDER, height=1).pack(side='top', fill='x')
        inner = tk.Frame(entry_frame, bg=HEADER_BG)
        inner.pack(fill='x', padx=12, pady=12)

        emoji_btn = tk.Label(inner, text='㋡', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 14),
                               cursor='hand2', padx=5)
        emoji_btn.pack(side='left')
        emoji_btn.bind('<Button-1>', lambda e: self.toggle_emoji_panel())
        emoji_btn.bind('<Enter>', lambda e: emoji_btn.config(fg=TEXT_LIGHT))
        emoji_btn.bind('<Leave>', lambda e: emoji_btn.config(fg=TEXT_MUTED))

        attach_btn = tk.Label(inner, text='📎', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 12),
                                cursor='hand2', padx=5)
        attach_btn.pack(side='left')
        attach_btn.bind('<Button-1>', lambda e: self.attach_image())
        attach_btn.bind('<Enter>', lambda e: attach_btn.config(fg=TEXT_LIGHT))
        attach_btn.bind('<Leave>', lambda e: attach_btn.config(fg=TEXT_MUTED))

        entry_wrap = tk.Frame(inner, bg=ENTRY_BG, highlightthickness=1,
                                highlightbackground=PANEL_BORDER, highlightcolor=ACCENT)
        entry_wrap.pack(side='left', fill='x', expand=True, padx=8)
        self.entry = tk.Entry(entry_wrap, font=('Segoe UI', 10), bg=ENTRY_BG, fg=TEXT_LIGHT,
                               insertbackground=TEXT_LIGHT, relief='flat', bd=0)
        self.entry.pack(fill='x', ipady=8, padx=10)
        self.entry.bind('<Return>', self.send_text)
        self.entry.bind('<Control-v>', self._on_paste)
        self.root.bind('<Control-v>', self._on_paste, add='+')

        send_btn = rounded_button(inner, '➤', lambda: self.send_text(), w=42, h=36)
        send_btn.pack(side='left')
        send_btn.bind('<Button-3>', self._send_btn_menu)

        self.root.bind('<Button-1>', lambda e: self.entry.focus_set(), add='+')

        self.toast = Toast(self.root)
        self.hide()
        self.root.after(300, apply_rounded_corners, self.root)

    def _drag_start(self, event):
        self._drag['x'] = event.x
        self._drag['y'] = event.y

    def _drag_move(self, event):
        x = self.root.winfo_pointerx() - self._drag['x']
        y = self.root.winfo_pointery() - self._drag['y']
        self.root.geometry('+%d+%d' % (x, y))

    def _bind_wheel(self, widget):
        widget.bind('<MouseWheel>', self._on_wheel)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

    def _scroll_to_end(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    # ── Собственная эмодзи-панель (надёжнее системной — не зависит от фокуса/оверлея) ──
    def toggle_emoji_panel(self):
        if self._emoji_panel is not None:
            self._emoji_panel.destroy()
            self._emoji_panel = None
            return
        panel = tk.Frame(self.root, bg=HEADER_BG, highlightthickness=1, highlightbackground=PANEL_BORDER)
        self._emoji_panel = panel
        panel.place(x=10, rely=1.0, y=-64, anchor='sw')
        cols = 7
        grid = tk.Frame(panel, bg=HEADER_BG)
        grid.pack(padx=6, pady=6)
        for i, em in enumerate(EMOJI_SET):
            b = tk.Label(grid, text=em, bg=HEADER_BG, font=('Segoe UI Emoji', 15), cursor='hand2', padx=5, pady=4)
            b.grid(row=i // cols, column=i % cols)
            b.bind('<Button-1>', lambda e, ch=em: self._insert_emoji(ch))
            b.bind('<Enter>', lambda e, w=b: w.config(bg=BUBBLE_ADMIN_BG))
            b.bind('<Leave>', lambda e, w=b: w.config(bg=HEADER_BG))

    def _insert_emoji(self, ch):
        self.entry.insert('insert', ch)
        self.entry.focus_set()

    def _send_btn_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0, bg=HEADER_BG, fg=TEXT_LIGHT, activebackground=ACCENT,
                        activeforeground='#fff', bd=0, relief='flat')
        menu.add_command(label='🔇  Отправить без звука у получателя', command=lambda: self.send_text(silent=True))
        menu.tk_popup(event.x_root, event.y_root)

    def set_status_dot(self, active):
        try:
            self.status_dot.config(fg=ONLINE_GREEN if active else ONLINE_RED)
        except Exception:
            pass

    def refresh_title(self):
        try:
            self.root.title(display_name())
        except Exception:
            pass

    def _bind_menu(self, widget, text=None, pil_img=None):
        def show(e):
            menu = build_message_menu(self.root, text=text, pil_img=pil_img)
            menu.tk_popup(e.x_root, e.y_root)
        widget.bind('<Button-3>', show)

    def append_system(self, text):
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=8, padx=12)
        lbl = tk.Label(row, text=text, bg=BG, fg=SYSTEM_TEXT, font=('Segoe UI', 8, 'italic'),
                        wraplength=280, justify='center')
        lbl.pack(anchor='center')
        self._bind_wheel(row); self._bind_wheel(lbl)
        self._scroll_to_end()

    def append_text(self, text, mine=False, msg_id=None):
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=4, padx=12)
        bubble_bg = BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG
        bubble_fg = '#ffffff' if mine else TEXT_LIGHT
        bubble = make_bubble(row, text, bubble_bg, bubble_fg)
        bubble.pack(side='right' if mine else 'left')
        self._bind_menu(bubble, text=text)
        meta = tk.Label(row, text=time.strftime('%H:%M') + (' ✓' if mine else ''),
                         bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w',
                   padx=(0, 3) if mine else (3, 0), pady=(2, 0))
        if mine and msg_id is not None:
            self._own_bubbles.append((msg_id, meta))
        self._bind_wheel(row); self._bind_wheel(bubble); self._bind_wheel(meta)
        self._scroll_to_end()

    def append_image(self, pil_img, mine=False, msg_id=None):
        thumb = pil_img.copy()
        thumb.thumbnail((230, 230))
        photo = ImageTk.PhotoImage(thumb)
        self._image_refs.append(photo)

        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=4, padx=12)
        wrap = tk.Frame(row, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG)
        wrap.pack(side='right' if mine else 'left')
        holder = tk.Label(wrap, image=photo, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG,
                            cursor='hand2', bd=0)
        holder.pack(padx=4, pady=4)
        holder.bind('<Button-1>', lambda e: self._open_full(pil_img))
        self._bind_menu(holder, pil_img=pil_img)

        meta = tk.Label(row, text=time.strftime('%H:%M') + (' ✓' if mine else ''),
                         bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w',
                   padx=(0, 3) if mine else (3, 0), pady=(2, 0))
        if mine and msg_id is not None:
            self._own_bubbles.append((msg_id, meta))
        self._bind_wheel(row); self._bind_wheel(wrap); self._bind_wheel(holder); self._bind_wheel(meta)
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

    def send_text(self, event=None, silent=False):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, 'end')
        base = get_base_url()
        if not base:
            self.append_system('Сервер администратора ещё не найден в сети…')
            return
        try:
            payload = {'pc': PC_NAME, 'from': 'client', 'type': 'text',
                       'text': enc_text(text), 'enc': True, 'silent': bool(silent)}
            r = _session.post(base + '/send', json=payload, timeout=HTTP_TIMEOUT)
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
            payload = {'pc': PC_NAME, 'from': 'client', 'type': 'image',
                       'text': enc_text(b64), 'enc': True}
            r = _session.post(base + '/send', json=payload, timeout=HTTP_TIMEOUT + 5)
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
        clip = None
        try:
            clip = ImageGrab.grabclipboard()
        except Exception as e:
            print('[messenger] clipboard error', e)

        if isinstance(clip, Image.Image):
            self.send_image(clip)
            return 'break'

        if isinstance(clip, list) and clip:
            for path in clip:
                try:
                    img = Image.open(path)
                    self.send_image(img)
                    return 'break'
                except Exception:
                    continue

        return None

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
        self.root.after(0, self.toast.show, text, 'Админ клуба', self.show)

    def welcome_toast(self):
        self.root.after(2000, self.toast.show,
                          'Нажми Pause / Break, чтобы открыть чат с админом',
                          'Godji Messenger', None)


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
            r = _session.get(base + '/messages', params={'pc': PC_NAME, 'since': _last_msg_id, 'enc': 1},
                              timeout=HTTP_TIMEOUT)
            for m in r.json():
                _last_msg_id = max(_last_msg_id, m['id'])
                if m['from'] != 'admin':
                    continue
                raw_text = dec_text(m['text']) if m.get('enc') else m['text']
                is_image = m.get('type') == 'image'
                if is_image:
                    try:
                        raw = base64.b64decode(raw_text)
                        img = Image.open(io.BytesIO(raw))
                        win.root.after(0, win.append_image, img, False, m['id'])
                    except Exception:
                        win.root.after(0, win.append_system, 'Не удалось загрузить изображение')
                else:
                    win.root.after(0, win.append_text, raw_text, False, m['id'])

                if not win.visible and not m.get('silent'):
                    preview = '📷 Изображение' if is_image else raw_text
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


def settings_loop(win):
    while True:
        base = get_base_url()
        if base:
            try:
                r = _session.get(base + '/settings', timeout=HTTP_TIMEOUT)
                s = r.json()
                _settings_cache['notifySound'] = bool(s.get('notifySound', True))
                _settings_cache['showOnlineIndicator'] = bool(s.get('showOnlineIndicator', True))
            except Exception:
                pass
            try:
                rm = _session.get(base + '/mute_state', params={'pc': PC_NAME}, timeout=HTTP_TIMEOUT)
                _mute_state['mutedUntil'] = float(rm.json().get('mutedUntil', 0) or 0)
            except Exception:
                pass

            if not _settings_cache.get('showOnlineIndicator', True):
                win.root.after(0, win.set_status_dot, False)
            else:
                try:
                    r2 = _session.get(base + '/chat_active', params={'pc': PC_NAME}, timeout=HTTP_TIMEOUT)
                    active = bool(r2.json().get('active'))
                    win.root.after(0, win.set_status_dot, active)
                except Exception:
                    win.root.after(0, win.set_status_dot, False)
        time.sleep(SETTINGS_INTERVAL)


def nickname_loop(win):
    while True:
        base = get_base_url()
        if base and not _manual_nickname[0]:
            try:
                r = _session.get(base + '/nickname', params={'pc': PC_NAME}, timeout=HTTP_TIMEOUT)
                data = r.json()
                nick = data.get('nickname')
                if nick != _settings_cache.get('nickname'):
                    _settings_cache['nickname'] = nick
                    win.root.after(0, win.refresh_title)
            except Exception:
                pass
        time.sleep(NICK_INTERVAL)


# ───────────────────────── мастер первого запуска ─────────────────────────
def run_wizard(on_done):
    wiz = tk.Tk()
    setup_ttk_style()
    wiz.overrideredirect(True)
    wiz.configure(bg=BG)
    w, h = 400, 220
    sw, sh = wiz.winfo_screenwidth(), wiz.winfo_screenheight()
    wiz.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
    wiz.attributes('-topmost', True)
    wiz.after(200, apply_rounded_corners, wiz)

    tk.Frame(wiz, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(wiz, text='Godji Messenger', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 14, 'bold')).pack(pady=(22, 2))
    tk.Label(wiz, text='Первый запуск — настройка', bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 9)).pack()
    status = tk.Label(wiz, text='Подготовка…', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 9), wraplength=340)
    status.pack(pady=14)
    warn = tk.Label(wiz, text='', bg=BG, fg='#e0a800', font=('Segoe UI', 8), wraplength=340, justify='center')
    warn.pack()

    def step_detect():
        print('[messenger] ПК определён как "%s"' % PC_NAME)

    def step_discover():
        host = discover_once(timeout=8)
        if host:
            _set_admin_host(host)

    def step_firewall():
        add_firewall_rule()
        time.sleep(2.5)
        if not verify_firewall_rule():
            warn.config(text='Не удалось подтвердить правило брандмауэра — если сервер не найдётся,\n'
                              'разреши вручную входящий UDP порт %d для этой программы.' % BEACON_PORT)

    steps = [
        ('Определяем этот ПК (%s)…' % PC_NAME, step_detect),
        ('Ищем сервер администратора в сети…', step_discover),
        ('Регистрируем автозапуск…', install_autostart),
        ('Настраиваем брандмауэр (может появиться запрос Windows)…', step_firewall),
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
            wiz.after(1800, lambda: (wiz.destroy(), on_done()))
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
    win.welcome_toast()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=poll_loop, args=(win,), daemon=True).start()
    threading.Thread(target=read_state_loop, args=(win,), daemon=True).start()
    threading.Thread(target=settings_loop, args=(win,), daemon=True).start()
    threading.Thread(target=nickname_loop, args=(win,), daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=hotkey_loop, args=(win.toggle,), daemon=True).start()
    win.root.mainloop()


def main():
    enable_dpi_awareness()
    print('=' * 50)
    print('[messenger] ПК: "%s"' % PC_NAME)
    print('=' * 50)
    cfg = load_config()
    if cfg.get('admin_host'):
        _set_admin_host(cfg['admin_host'])
    if cfg.get('configured'):
        start_app()
    else:
        run_wizard(start_app)


if __name__ == '__main__':
    main()
