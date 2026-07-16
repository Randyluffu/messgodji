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
import tkinter.ttk as ttk
from tkinter import filedialog

import requests
from PIL import Image, ImageDraw, ImageGrab, ImageTk
from cryptography.fernet import Fernet, InvalidToken

try:
    import win32clipboard
except Exception:
    win32clipboard = None

APP_NAME = 'GodjiMessengerClient'
HTTP_PORT = 6070
BEACON_PORT = 47990
HEARTBEAT_INTERVAL = 4
POLL_INTERVAL = 1
READ_STATE_INTERVAL = 2
SETTINGS_INTERVAL = 2
NICK_INTERVAL = 20
HTTP_TIMEOUT = 3
TOAST_MS = 7000
MAX_TOASTS_VISIBLE = 4
MAX_IMAGE_SIDE = 900
JPEG_QUALITY = 78
MAX_FILE_BYTES = 6 * 1024 * 1024
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

_settings_cache = {'notifySound': True, 'showOnlineIndicator': True, 'autostart': True}
_mute_state = {'mutedUntil': 0.0}
_manual_nickname = [None]
_image_cache = []  # держим ссылки на PhotoImage, чтобы GC не удалил


# ───────────────────────── DPI ─────────────────────────
def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ───────────────────────── звук ─────────────────────────
def play_chime():
    if not _settings_cache.get('notifySound', True) or time.time() < _mute_state.get('mutedUntil', 0):
        return
    try:
        winsound.Beep(880, 90)
        winsound.Beep(1175, 110)
    except Exception:
        try:
            winsound.MessageBeep(-1)
        except Exception:
            pass


# ───────────────────────── скруглённые окна (GDI, работает на всех Windows) ─────────────────────────
def apply_rounded_corners(win, radius=16):
    try:
        win.update_idletasks()
        hwnd = win.winfo_id()
        w = win.winfo_width()
        h = win.winfo_height()
        hrgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
        ctypes.windll.user32.SetWindowRgn(hwnd, hrgn, True)
    except Exception:
        pass


def setup_ttk_style():
    style = ttk.Style()
    try:
        style.theme_use('clam')
    except Exception:
        pass
    style.configure('Godji.Vertical.TScrollbar', background=PANEL_BORDER, troughcolor=BG,
                     bordercolor=BG, arrowcolor=TEXT_MUTED, relief='flat', gripcount=0, width=8)
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
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r'Software\Microsoft\Windows\CurrentVersion\Run',
                              0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
        winreg.CloseKey(key)
    except Exception as e:
        print('[messenger] Ошибка автозапуска:', e)


def remove_autostart():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r'Software\Microsoft\Windows\CurrentVersion\Run',
                              0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception as e:
        print('[messenger] Ошибка отключения автозапуска:', e)


def add_firewall_rule():
    exe = get_self_path() or sys.executable
    args = ('advfirewall firewall add rule name="Godji Messenger Client" '
            'dir=in action=allow protocol=UDP localport=%d program="%s" '
            'enable=yes profile=any') % (BEACON_PORT, exe)
    try:
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'netsh', args, None, 0)
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
    except Exception:
        pass


def hotkey_loop(callback):
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, VK_PAUSE):
        print('[messenger] !!! Не удалось зарегистрировать Pause/Break')
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


# ═══════════════ ЕДИНАЯ СИСТЕМА ДИЗАЙНА (свои скругления, без Windows-виджетов) ═══════════════
def rounded_rect_photo(w, h, radius, color, scale=4):
    """Гладкий скруглённый прямоугольник через супersample в Pillow —
    без 'лесенок', которые даёт голый Canvas.create_polygon."""
    w, h = max(int(w), 1), max(int(h), 1)
    W, H, R = w * scale, h * scale, radius * scale
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=color)
    img = img.resize((w, h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    _image_cache.append(photo)
    return photo


def make_bubble(parent, text, bg, fg, wrap_px=220, font=('Segoe UI', 10), pad_x=12, pad_y=9, radius=RADIUS):
    """Скруглённый пузырь = гладкий PIL-фон (Canvas.create_image) + настоящий
    Label поверх (create_window) — Label рендерит эмодзи в цвете нативно,
    в отличие от Canvas.create_text, который красит их одним цветом."""
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg, font=font, wraplength=wrap_px, justify='left', bd=0)
    lbl.update_idletasks()
    tw = lbl.winfo_reqwidth()
    th = lbl.winfo_reqheight()
    w, h = tw + pad_x * 2, th + pad_y * 2
    c = tk.Canvas(parent, width=w, height=h, bg=parent['bg'], highlightthickness=0, bd=0)
    photo = rounded_rect_photo(w, h, radius, bg)
    c.create_image(0, 0, image=photo, anchor='nw')
    c.create_window(pad_x, pad_y, window=lbl, anchor='nw')
    return c


def rounded_button(parent, text, command, bg=ACCENT, hover=ACCENT_HOVER, fg='#fff',
                     w=42, h=34, font=('Segoe UI', 12, 'bold'), radius=RADIUS):
    c = tk.Canvas(parent, width=w, height=h, bg=parent['bg'], highlightthickness=0, bd=0, cursor='hand2')
    photo_n = rounded_rect_photo(w, h, radius, bg)
    photo_h = rounded_rect_photo(w, h, radius, hover)
    img_id = c.create_image(0, 0, image=photo_n, anchor='nw')
    c.create_text(w / 2, h / 2, text=text, fill=fg, font=font)
    c.bind('<Button-1>', lambda e: command())
    c.bind('<Enter>', lambda e: c.itemconfig(img_id, image=photo_h))
    c.bind('<Leave>', lambda e: c.itemconfig(img_id, image=photo_n))
    return c


def rounded_pill(parent, text, command, bg=BUBBLE_ADMIN_BG, hover=ACCENT, fg=TEXT_LIGHT,
                   font=('Segoe UI', 9), pad_x=13, pad_y=7, radius=11):
    lbl = tk.Label(parent, text=text, font=font)
    lbl.update_idletasks()
    tw, th = lbl.winfo_reqwidth(), lbl.winfo_reqheight()
    lbl.destroy()
    w, h = tw + pad_x * 2, th + pad_y * 2
    c = tk.Canvas(parent, width=w, height=h, bg=parent['bg'], highlightthickness=0, bd=0, cursor='hand2')
    photo_n = rounded_rect_photo(w, h, radius, bg)
    photo_h = rounded_rect_photo(w, h, radius, hover)
    img_id = c.create_image(0, 0, image=photo_n, anchor='nw')
    c.create_text(w / 2, h / 2, text=text, fill=fg, font=font)
    c.bind('<Button-1>', lambda e: command())
    c.bind('<Enter>', lambda e: c.itemconfig(img_id, image=photo_h))
    c.bind('<Leave>', lambda e: c.itemconfig(img_id, image=photo_n))
    return c


class CustomMenu:
    """Своё контекстное меню вместо системного tk.Menu — единый стиль."""
    def __init__(self, parent):
        self.parent = parent
        self.win = None
        self.items = []

    def add_command(self, label, command, enabled=True):
        self.items.append(('cmd', label, command, enabled))

    def add_separator(self):
        self.items.append(('sep', None, None, None))

    def popup(self, x, y):
        self.close_all()
        win = tk.Toplevel(self.parent)
        self.win = win
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=HEADER_BG)
        frame = tk.Frame(win, bg=HEADER_BG, highlightthickness=1, highlightbackground=PANEL_BORDER)
        frame.pack()
        for kind, label, cmd, enabled in self.items:
            if kind == 'sep':
                tk.Frame(frame, bg=PANEL_BORDER, height=1).pack(fill='x', padx=8, pady=4)
                continue
            row = tk.Label(frame, text=label, bg=HEADER_BG, fg=(TEXT_LIGHT if enabled else TEXT_MUTED),
                            font=('Segoe UI', 9), anchor='w', padx=14, pady=7,
                            cursor='hand2' if enabled else 'arrow')
            row.pack(fill='x')
            if enabled:
                row.bind('<Button-1>', lambda e, c=cmd: (self.close(), c()))
                row.bind('<Enter>', lambda e, w_=row: w_.config(bg=ACCENT))
                row.bind('<Leave>', lambda e, w_=row: w_.config(bg=HEADER_BG))
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        ww, wh = win.winfo_width(), win.winfo_height()
        x = min(x, sw - ww - 4)
        y = min(y, sh - wh - 4)
        win.geometry('+%d+%d' % (x, y))
        apply_rounded_corners(win, radius=10)
        win.after(30, lambda: win.bind('<FocusOut>', lambda e: self.close()))
        win.focus_force()

    def close(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None

    _all = []

    def close_all(self):
        pass


def show_menu(parent, x, y, items):
    """items: список (label, callback) или None для разделителя."""
    m = CustomMenu(parent)
    for it in items:
        if it is None:
            m.add_separator()
        else:
            m.add_command(it[0], it[1])
    m.popup(x, y)
    return m


def ask_string_dialog(parent, title, prompt, initial=''):
    """Свой диалог ввода текста вместо системного simpledialog."""
    result = {'value': None}
    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes('-topmost', True)
    win.configure(bg=HEADER_BG)
    w, h = 320, 150
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))

    tk.Frame(win, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(win, text=title, bg=HEADER_BG, fg=TEXT_LIGHT, font=('Segoe UI', 11, 'bold')).pack(pady=(14, 2))
    tk.Label(win, text=prompt, bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 9)).pack()

    entry_wrap = tk.Frame(win, bg=ENTRY_BG, highlightthickness=1, highlightbackground=PANEL_BORDER)
    entry_wrap.pack(padx=20, pady=12, fill='x')
    entry = tk.Entry(entry_wrap, bg=ENTRY_BG, fg=TEXT_LIGHT, insertbackground=TEXT_LIGHT, relief='flat', bd=0,
                      font=('Segoe UI', 10))
    entry.insert(0, initial)
    entry.pack(fill='x', ipady=6, padx=8)
    entry.focus_set()
    entry.select_range(0, 'end')

    btns = tk.Frame(win, bg=HEADER_BG)
    btns.pack(pady=6)

    def confirm(event=None):
        result['value'] = entry.get().strip()
        win.destroy()

    def cancel(event=None):
        win.destroy()

    rounded_pill(btns, 'Отмена', cancel, bg=BUBBLE_ADMIN_BG, hover='#3a2226').pack(side='left', padx=6)
    rounded_pill(btns, 'Сохранить', confirm, bg=ACCENT, hover=ACCENT_HOVER, fg='#fff').pack(side='left', padx=6)
    entry.bind('<Return>', confirm)
    entry.bind('<Escape>', cancel)
    apply_rounded_corners(win, radius=14)

    win.grab_set()
    win.wait_window()
    return result['value']


def build_message_menu(parent, x, y, text=None, pil_img=None):
    items = []
    if text is not None:
        items.append(('Скопировать текст', lambda: copy_text_to_clipboard(parent, text)))
    if pil_img is not None:
        items.append(('Скопировать изображение', lambda: copy_image_to_clipboard(pil_img)))
        def save_as():
            path = filedialog.asksaveasfilename(defaultextension='.png',
                                                  filetypes=[('PNG', '*.png'), ('JPEG', '*.jpg')])
            if path:
                try:
                    pil_img.save(path)
                except Exception:
                    pass
        items.append(('Сохранить как…', save_as))
    show_menu(parent, x, y, items)


# ───────────────────────── стек уведомлений — ПРАВЫЙ ВЕРХНИЙ УГОЛ, максимум 4 ─────────────────────────
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
        h = max(min(len(self._cards), MAX_TOASTS_VISIBLE), 1) * (self.CARD_H + self.GAP)
        self.win.geometry('%dx%d+%d+%d' % (self.WIDTH, h, sw - self.WIDTH - 20, 20))
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
        play_chime()


class Toast:
    def __init__(self, parent_tk):
        self.stack = ToastStack(parent_tk)

    def show(self, text, title='Админ клуба', on_click=None):
        self.stack.show(title, text, on_click)


# ───────────────────────── список с автоскрытием скроллбара ─────────────────────────
def make_scroll_area(parent, bg=BG):
    outer = tk.Frame(parent, bg=bg)
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview, style='Godji.Vertical.TScrollbar')
    inner = tk.Frame(canvas, bg=bg)
    win_id = canvas.create_window((0, 0), window=inner, anchor='nw')

    def check_visibility():
        canvas.update_idletasks()
        bbox = canvas.bbox('all')
        content_h = (bbox[3] - bbox[1]) if bbox else 0
        visible_h = canvas.winfo_height()
        if content_h > visible_h + 2:
            if not vsb.winfo_ismapped():
                vsb.pack(side='right', fill='y')
        else:
            if vsb.winfo_ismapped():
                vsb.pack_forget()

    def on_inner_configure(e):
        canvas.configure(scrollregion=canvas.bbox('all'))
        check_visibility()

    inner.bind('<Configure>', on_inner_configure)
    canvas.bind('<Configure>', lambda e: (canvas.itemconfig(win_id, width=e.width), check_visibility()))
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side='left', fill='both', expand=True)

    def on_wheel(e):
        canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
    canvas.bind('<MouseWheel>', on_wheel)
    inner.bind('<MouseWheel>', on_wheel)

    outer._canvas = canvas
    outer._inner = inner
    outer._check = check_visibility
    outer._on_wheel = on_wheel
    return outer, inner, canvas


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
        self.focused = False
        self._own_bubbles = []
        self._drag = {'x': 0, 'y': 0}
        self._emoji_panel = None
        self.root.bind('<FocusIn>', self._on_focus_in)
        self.root.bind('<FocusOut>', self._on_focus_out)

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

        right = tk.Frame(header, bg=HEADER_BG)
        right.pack(side='right', padx=10)
        settings_btn = tk.Label(right, text='⚙', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 12), cursor='hand2')
        settings_btn.pack(side='left', padx=6)
        settings_btn.bind('<Button-1>', lambda e: self.open_settings())
        close_btn = tk.Label(right, text='—', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 13), cursor='hand2')
        close_btn.pack(side='left', padx=6)
        close_btn.bind('<Button-1>', lambda e: self.hide())
        for b in (settings_btn, close_btn):
            b.bind('<Enter>', lambda e, w_=b: w_.config(fg=TEXT_LIGHT))
            b.bind('<Leave>', lambda e, w_=b: w_.config(fg=TEXT_MUTED))

        for w_ in (header, left):
            w_.bind('<Button-1>', self._drag_start)
            w_.bind('<B1-Motion>', self._drag_move)

        scroll_outer, self.msg_frame, self.canvas = make_scroll_area(self.root, bg=BG)
        scroll_outer.pack(side='top', fill='both', expand=True)

        entry_frame = tk.Frame(self.root, bg=HEADER_BG)
        entry_frame.pack(side='bottom', fill='x')
        tk.Frame(entry_frame, bg=PANEL_BORDER, height=1).pack(side='top', fill='x')
        inner = tk.Frame(entry_frame, bg=HEADER_BG)
        inner.pack(fill='x', padx=12, pady=12)

        emoji_btn = tk.Label(inner, text='☺', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 15), cursor='hand2', padx=5)
        emoji_btn.pack(side='left')
        emoji_btn.bind('<Button-1>', lambda e: self.toggle_emoji_panel())
        emoji_btn.bind('<Enter>', lambda e: emoji_btn.config(fg=TEXT_LIGHT))
        emoji_btn.bind('<Leave>', lambda e: emoji_btn.config(fg=TEXT_MUTED))

        attach_btn = tk.Label(inner, text='📎', bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 12), cursor='hand2', padx=5)
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

        send_btn = rounded_button(inner, '➤', lambda: self.send_text())
        send_btn.pack(side='left')
        send_btn.bind('<Button-3>', self._send_btn_menu)

        self.root.bind('<Button-1>', lambda e: self.entry.focus_set(), add='+')

        self.toast = Toast(self.root)
        self.hide()
        self.root.after(200, apply_rounded_corners, self.root)

    def _on_focus_in(self, event=None):
        if event is None or event.widget == self.root:
            self.focused = True

    def _on_focus_out(self, event=None):
        if event is None or event.widget == self.root:
            self.focused = False

    def _drag_start(self, event):
        self._drag['x'] = event.x
        self._drag['y'] = event.y

    def _drag_move(self, event):
        x = self.root.winfo_pointerx() - self._drag['x']
        y = self.root.winfo_pointery() - self._drag['y']
        self.root.geometry('+%d+%d' % (x, y))

    def _scroll_to_end(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def toggle_emoji_panel(self):
        if self._emoji_panel is not None:
            self._emoji_panel.destroy()
            self._emoji_panel = None
            return
        panel = tk.Frame(self.root, bg=HEADER_BG, highlightthickness=1, highlightbackground=PANEL_BORDER)
        self._emoji_panel = panel
        panel.place(x=10, rely=1.0, y=-66, anchor='sw')
        grid = tk.Frame(panel, bg=HEADER_BG)
        grid.pack(padx=6, pady=6)
        cols = 7
        for i, em in enumerate(EMOJI_SET):
            b = tk.Label(grid, text=em, bg=HEADER_BG, font=('Segoe UI Emoji', 15), cursor='hand2', padx=5, pady=4)
            b.grid(row=i // cols, column=i % cols)
            b.bind('<Button-1>', lambda e, ch=em: self._insert_emoji(ch))
            b.bind('<Enter>', lambda e, w_=b: w_.config(bg=BUBBLE_ADMIN_BG))
            b.bind('<Leave>', lambda e, w_=b: w_.config(bg=HEADER_BG))

    def _insert_emoji(self, ch):
        self.entry.insert('insert', ch)
        self.entry.focus_set()

    def _send_btn_menu(self, event):
        show_menu(self.root, event.x_root, event.y_root,
                   [('🔇  Отправить без звука у получателя', lambda: self.send_text(silent=True))])

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
        widget.bind('<Button-3>', lambda e: build_message_menu(self.root, e.x_root, e.y_root, text=text, pil_img=pil_img))

    def append_system(self, text):
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=8, padx=12)
        tk.Label(row, text=text, bg=BG, fg=SYSTEM_TEXT, font=('Segoe UI', 8, 'italic'),
                  wraplength=280, justify='center').pack(anchor='center')
        self._scroll_to_end()

    def append_text(self, text, mine=False, msg_id=None):
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=4, padx=12)
        bubble = make_bubble(row, text, BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG, '#ffffff' if mine else TEXT_LIGHT)
        bubble.pack(side='right' if mine else 'left')
        self._bind_menu(bubble, text=text)
        meta = tk.Label(row, text=time.strftime('%H:%M') + (' ✓' if mine else ''),
                         bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w',
                   padx=(0, 3) if mine else (3, 0), pady=(2, 0))
        if mine and msg_id is not None:
            self._own_bubbles.append((msg_id, meta))
        self._scroll_to_end()

    def append_image(self, pil_img, mine=False, msg_id=None):
        thumb = pil_img.copy()
        thumb.thumbnail((230, 230))
        photo = ImageTk.PhotoImage(thumb)
        _image_cache.append(photo)

        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=4, padx=12)
        wrap = tk.Frame(row, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG)
        wrap.pack(side='right' if mine else 'left')
        holder = tk.Label(wrap, image=photo, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG, cursor='hand2', bd=0)
        holder.pack(padx=4, pady=4)
        holder.bind('<Button-1>', lambda e: self._open_full(pil_img))
        self._bind_menu(holder, pil_img=pil_img)

        meta = tk.Label(row, text=time.strftime('%H:%M') + (' ✓' if mine else ''),
                         bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w',
                   padx=(0, 3) if mine else (3, 0), pady=(2, 0))
        if mine and msg_id is not None:
            self._own_bubbles.append((msg_id, meta))
        self._scroll_to_end()

    def _open_full(self, pil_img):
        try:
            fd, path = tempfile.mkstemp(suffix='.png')
            os.close(fd)
            pil_img.save(path)
            os.startfile(path)
        except Exception as e:
            self.append_system('Не удалось открыть изображение: %s' % e)

    def append_file(self, filename, raw_bytes, mine=False, msg_id=None):
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', pady=4, padx=12)
        card_bg = BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG
        card_fg = '#ffffff' if mine else TEXT_LIGHT
        card = tk.Frame(row, bg=card_bg, cursor='hand2')
        card.pack(side='right' if mine else 'left')
        photo = rounded_rect_photo(220, 46, RADIUS, card_bg)
        c = tk.Canvas(card, width=220, height=46, bg=row['bg'], highlightthickness=0, bd=0)
        c.create_image(0, 0, image=photo, anchor='nw')
        c.create_text(14, 23, text='📄', font=('Segoe UI', 15), anchor='w')
        short = filename if len(filename) <= 26 else filename[:23] + '…'
        c.create_text(40, 23, text=short, fill=card_fg, font=('Segoe UI', 9), anchor='w')
        c.pack()

        def save_as():
            path = filedialog.asksaveasfilename(initialfile=filename)
            if path:
                try:
                    with open(path, 'wb') as f:
                        f.write(raw_bytes)
                except Exception:
                    pass

        c.bind('<Button-1>', lambda e: save_as())
        c.bind('<Button-3>', lambda e: show_menu(self.root, e.x_root, e.y_root, [('Сохранить как…', save_as)]))

        meta = tk.Label(row, text=time.strftime('%H:%M') + (' ✓' if mine else ''),
                         bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w',
                   padx=(0, 3) if mine else (3, 0), pady=(2, 0))
        if mine and msg_id is not None:
            self._own_bubbles.append((msg_id, meta))
        self._scroll_to_end()

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
            payload = {'pc': PC_NAME, 'from': 'client', 'type': 'image', 'text': enc_text(b64), 'enc': True}
            r = _session.post(base + '/send', json=payload, timeout=HTTP_TIMEOUT + 5)
            data = r.json() if r.status_code == 200 else {}
            self.append_image(img, mine=True, msg_id=data.get('id'))
        except Exception:
            self.append_system('Не удалось отправить изображение')

    def send_file(self, path):
        base = get_base_url()
        if not base:
            self.append_system('Сервер администратора ещё не найден в сети…')
            return
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except Exception as e:
            self.append_system('Не удалось прочитать файл: %s' % e)
            return
        if len(raw) > MAX_FILE_BYTES:
            self.append_system('Файл слишком большой (максимум %d МБ)' % (MAX_FILE_BYTES // (1024 * 1024)))
            return
        filename = os.path.basename(path)
        b64 = base64.b64encode(raw).decode('ascii')
        try:
            payload = {'pc': PC_NAME, 'from': 'client', 'type': 'file', 'filename': filename,
                       'text': enc_text(b64), 'enc': True}
            r = _session.post(base + '/send', json=payload, timeout=HTTP_TIMEOUT + 10)
            data = r.json() if r.status_code == 200 else {}
            self.append_file(filename, raw, mine=True, msg_id=data.get('id'))
        except Exception:
            self.append_system('Не удалось отправить файл')

    def attach_image(self):
        """Кнопка 📎 — теперь любой файл, не только картинка."""
        path = filedialog.askopenfilename(title='Выбери файл')
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
            try:
                self.send_image(Image.open(path))
                return
            except Exception:
                pass  # не открылась как картинка — отправим как обычный файл
        self.send_file(path)

    def _on_paste(self, event=None):
        clip = None
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            pass
        if isinstance(clip, Image.Image):
            self.send_image(clip)
            return 'break'
        if isinstance(clip, list) and clip:
            for path in clip:
                try:
                    self.send_image(Image.open(path))
                    return 'break'
                except Exception:
                    self.send_file(path)
                    return 'break'
        return None

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=HEADER_BG)
        w, h = 300, 230
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
        tk.Frame(win, bg=ACCENT, height=3).pack(fill='x')
        tk.Label(win, text='Настройки', bg=HEADER_BG, fg=TEXT_LIGHT, font=('Segoe UI', 12, 'bold')).pack(pady=(14, 10))

        var_auto = tk.BooleanVar(value=_settings_cache.get('autostart', True))
        def on_auto():
            if var_auto.get():
                install_autostart()
            else:
                remove_autostart()
            _settings_cache['autostart'] = var_auto.get()
        tk.Checkbutton(win, text='Автозапуск с Windows', variable=var_auto, command=on_auto,
                        bg=HEADER_BG, fg=TEXT_LIGHT, selectcolor=BUBBLE_ADMIN_BG, activebackground=HEADER_BG,
                        activeforeground=TEXT_LIGHT, font=('Segoe UI', 9)).pack(anchor='w', padx=20, pady=4)

        muted = time.time() < _mute_state.get('mutedUntil', 0)
        status_text = 'Звук от админа временно приглушён им самим' if muted else 'Звук включён'
        tk.Label(win, text=status_text, bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 8, 'italic'),
                  wraplength=260).pack(anchor='w', padx=20, pady=(10, 0))

        tk.Label(win, text='ПК: %s' % PC_NAME, bg=HEADER_BG, fg=TEXT_MUTED, font=('Segoe UI', 8)).pack(
            anchor='w', padx=20, pady=(14, 0))
        tk.Label(win, text='Сервер: %s' % (get_base_url() or 'ищу в сети…'), bg=HEADER_BG, fg=TEXT_MUTED,
                  font=('Segoe UI', 8)).pack(anchor='w', padx=20)

        rounded_pill(win, 'Закрыть', win.destroy, bg=ACCENT, hover=ACCENT_HOVER, fg='#fff').pack(pady=16)
        apply_rounded_corners(win, radius=14)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', False)
        self.root.attributes('-topmost', True)
        self.root.focus_force()
        self.entry.focus_set()
        self.visible = True
        self.focused = True

    def hide(self):
        if self._emoji_panel is not None:
            self._emoji_panel.destroy()
            self._emoji_panel = None
        self.root.withdraw()
        self.visible = False
        self.focused = False

    def _toggle_impl(self):
        self.hide() if self.visible else self.show()

    def toggle(self):
        self.root.after(0, self._toggle_impl)

    def notify(self, text):
        self.root.after(0, self.toast.show, text, 'Админ клуба', self.show)

    def welcome_toast(self):
        self.root.after(2000, self.toast.show,
                          'Нажми Pause / Break, чтобы открыть чат с админом', 'Godji Messenger', None)


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
                mtype = m.get('type')
                if mtype == 'image':
                    try:
                        raw = base64.b64decode(raw_text)
                        img = Image.open(io.BytesIO(raw))
                        win.root.after(0, win.append_image, img, False, m['id'])
                    except Exception:
                        win.root.after(0, win.append_system, 'Не удалось загрузить изображение')
                elif mtype == 'file':
                    try:
                        raw = base64.b64decode(raw_text)
                        fname = m.get('filename', 'файл')
                        win.root.after(0, win.append_file, fname, raw, False, m['id'])
                    except Exception:
                        win.root.after(0, win.append_system, 'Не удалось загрузить файл')
                else:
                    win.root.after(0, win.append_text, raw_text, False, m['id'])

                if not win.visible and not m.get('silent'):
                    if mtype == 'image':
                        preview = '📷 Изображение'
                    elif mtype == 'file':
                        preview = '📄 Файл: ' + m.get('filename', '')
                    else:
                        preview = raw_text
                    win.notify(preview)

            if win.visible and win.focused and _last_msg_id > 0:
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
                win.root.after(0, win.update_read_status, int(r.json().get('readAdmin', 0)))
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
                    win.root.after(0, win.set_status_dot, bool(r2.json().get('active')))
                except Exception:
                    win.root.after(0, win.set_status_dot, False)
        time.sleep(SETTINGS_INTERVAL)


def nickname_loop(win):
    while True:
        base = get_base_url()
        if base and not _manual_nickname[0]:
            try:
                r = _session.get(base + '/nickname', params={'pc': PC_NAME}, timeout=HTTP_TIMEOUT)
                nick = r.json().get('nickname')
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
    wiz.after(150, apply_rounded_corners, wiz)

    tk.Frame(wiz, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(wiz, text='Godji Messenger', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 14, 'bold')).pack(pady=(22, 2))
    tk.Label(wiz, text='Первый запуск — настройка', bg=BG, fg=TEXT_MUTED, font=('Segoe UI', 9)).pack()
    status = tk.Label(wiz, text='Подготовка…', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 9), wraplength=340)
    status.pack(pady=14)
    warn = tk.Label(wiz, text='', bg=BG, fg='#e0a800', font=('Segoe UI', 8), wraplength=340, justify='center')
    warn.pack()

    def step_discover():
        host = discover_once(timeout=8)
        if host:
            _set_admin_host(host)

    def step_firewall():
        add_firewall_rule()
        time.sleep(2.5)
        if not verify_firewall_rule():
            warn.config(text='Не удалось подтвердить правило брандмауэра — если сервер не найдётся,\n'
                              'разреши вручную входящий UDP порт %d.' % BEACON_PORT)

    steps = [
        ('Определяем этот ПК (%s)…' % PC_NAME, lambda: None),
        ('Ищем сервер администратора в сети…', step_discover),
        ('Регистрируем автозапуск…', install_autostart),
        ('Настраиваем брандмауэр (может появиться запрос Windows)…', step_firewall),
    ]
    idx = [0]

    def next_step():
        if idx[0] >= len(steps):
            status.config(text='Готово! Сервер найден.' if get_base_url() else
                           'Готово! Сервер пока не найден — попробую ещё раз в фоне.')
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
