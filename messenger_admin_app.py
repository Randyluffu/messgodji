#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godji Messenger — АДМИНСКОЕ приложение.

Первый запуск: сама регистрирует автозапуск, добавляет правило
брандмауэра (один раз спросит подтверждение Windows/UAC), затем
открывает главное окно. В остальных случаях — сразу главное окно
(двойной клик по значку в трее тоже его открывает/поднимает).

Главное окно: слева сжатый сайдбар (бургер разворачивает его с подписями) —
Недавние чаты, Все чаты (сортировка по включённости и номеру), Настройки.

Сервер рассылает по локальной сети UDP-маячок — клиенты сами находят IP
этого ПК. Трафик клиент<->сервер (единственный участок, идущий по реальной
сети клуба) шифруется общим ключом; ERP и это приложение читают то же
самое сообщение уже в расшифрованном виде (они всегда на loopback).

Сборка в exe: build_exe.bat или .github/workflows/build-exe.yml.
Зависимости (только на этапе сборки):
    pip install requests pillow pystray pywin32 cryptography
"""
import base64
import ctypes
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import winsound
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, simpledialog

from PIL import Image, ImageDraw, ImageGrab, ImageTk
from cryptography.fernet import Fernet, InvalidToken

try:
    import win32clipboard
except Exception:
    win32clipboard = None

try:
    import pystray
except Exception:
    pystray = None

APP_NAME = 'GodjiMessengerAdmin'
HTTP_PORT = 6070
BEACON_PORT = 47990
BEACON_INTERVAL = 2
ONLINE_TIMEOUT = 15
CHAT_ACTIVE_TIMEOUT = 6
ERP_ALIVE_TIMEOUT = 20
HISTORY_TTL = 24 * 3600
MAX_IMAGE_B64 = 3 * 1024 * 1024

SHARED_KEY = b'uus8GixjnYZbgjTRaHdUz3RSrHmgxIsoOfUMxL8Cufg='
_fernet = Fernet(SHARED_KEY)

CONFIG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'GodjiMessenger')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'admin_config.json')

BG = '#181113'
PANEL = '#221417'
PANEL2 = '#1c1213'
PANEL_BORDER = '#3a2226'
ACCENT = '#d4172a'
ACCENT_HOVER = '#b01120'
TEXT_LIGHT = '#f5eeee'
MUTED = '#a68d8f'
BUBBLE_ADMIN_BG = '#2b1c1f'
BUBBLE_ME_BG = '#d4172a'
TEXT_READ = '#f2a33c'
SYSTEM_TEXT = '#e0a800'
ENTRY_BG = '#241619'
ONLINE_GREEN = '#3ecf5e'
ONLINE_RED = '#e0393f'

MAX_TOASTS_VISIBLE = 4

_notify_target = ['erp']
_show_online_indicator = [True]
_notify_sound = [True]
_muted_until = [0.0]
_open_chats = {}
_erp_last_seen = [0.0]


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


def make_noactivate(win):
    try:
        hwnd = win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:
        pass


# ───────────────────────── шифрование (участок клиент-ПК <-> сервер) ─────────────────────────
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
    args = ('advfirewall firewall add rule name="Godji Messenger Admin" '
            'dir=in action=allow protocol=TCP localport=%d program="%s" '
            'enable=yes profile=any') % (HTTP_PORT, exe)
    try:
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'netsh', args, None, 0)
        print('[messenger] Запрос на правило брандмауэра отправлен')
    except Exception as e:
        print('[messenger] Ошибка брандмауэра:', e)


# ───────────────────────── общее хранилище ─────────────────────────
lock = threading.Lock()
last_seen = {}       # {pc: ts}
messages = []        # [{id, pc, from, type, text, ts, silent}]
_next_id = 1
read_client = {}
read_admin = {}
chat_active = {}
pc_nicknames = {}     # {pc: {'nickname':str, 'manual':bool, 'ts':float}}


def now():
    return time.time()


def post_message(pc, frm, mtype, text, silent=False):
    global _next_id
    with lock:
        msg = {'id': _next_id, 'pc': pc, 'from': frm, 'type': mtype, 'text': text,
               'ts': now(), 'silent': bool(silent)}
        _next_id += 1
        messages.append(msg)
        cutoff = now() - HISTORY_TTL
        messages[:] = [m for m in messages if m['ts'] > cutoff]
    return msg


def get_messages_since(pc, since):
    with lock:
        return [m for m in messages if m['pc'] == pc and m['id'] > since]


def clear_messages(pc):
    with lock:
        messages[:] = [m for m in messages if m['pc'] != pc]
        read_client.pop(pc, None)
        read_admin.pop(pc, None)


def mark_read_admin(pc, upto):
    if not upto:
        return
    with lock:
        read_admin[pc] = max(read_admin.get(pc, 0), upto)


def get_read_client(pc):
    with lock:
        return read_client.get(pc, 0)


def ping_chat_active(pc):
    with lock:
        chat_active[pc] = now()


def is_chat_active(pc):
    with lock:
        return (now() - chat_active.get(pc, 0)) <= CHAT_ACTIVE_TIMEOUT


def get_recent_chats():
    with lock:
        by_pc = {}
        for m in messages:
            cur = by_pc.get(m['pc'])
            if cur is None or m['ts'] > cur['ts']:
                by_pc[m['pc']] = m
        return sorted(by_pc.values(), key=lambda m: -m['ts'])


def online_count():
    with lock:
        t = now()
        return sum(1 for ts in last_seen.values() if (t - ts) <= ONLINE_TIMEOUT)


def get_all_pcs():
    """Все известные ПК (были онлайн когда-либо в этой сессии сервера), с их
    статусом; используется в разделе 'Все чаты' главного окна."""
    with lock:
        t = now()
        out = []
        seen_pcs = set(last_seen.keys()) | {m['pc'] for m in messages}
        for pc in seen_pcs:
            ts = last_seen.get(pc, 0)
            out.append({'pc': pc, 'online': (t - ts) <= ONLINE_TIMEOUT})
        def sort_key(item):
            try:
                num = int(item['pc'])
            except Exception:
                num = 999
            return (0 if item['online'] else 1, num, item['pc'])
        return sorted(out, key=sort_key)


def get_nickname(pc):
    with lock:
        info = pc_nicknames.get(pc)
        return info['nickname'] if info else None


def set_nickname(pc, nickname, manual=False):
    with lock:
        cur = pc_nicknames.get(pc)
        if cur and cur.get('manual') and not manual:
            return  # ручное имя не даём затирать автоопределением
        pc_nicknames[pc] = {'nickname': nickname, 'manual': manual, 'ts': now()}


def display_label(pc):
    nick = get_nickname(pc)
    return ('%s — ПК %s' % (nick, pc)) if nick else ('ПК ' + pc)


def is_muted():
    return now() < _muted_until[0]


def mute_for(minutes=None, session=False):
    _muted_until[0] = now() + (24 * 3600 if session else (minutes or 0) * 60)
    cfg = load_config()
    cfg['muted_until'] = _muted_until[0]
    save_config(cfg)


def unmute():
    _muted_until[0] = 0
    cfg = load_config()
    cfg['muted_until'] = 0
    save_config(cfg)


def erp_is_alive():
    return (now() - _erp_last_seen[0]) <= ERP_ALIVE_TIMEOUT


def effective_notify_is_admin_app():
    """Если выбран режим 'ERP', но ERP давно не пинговал сервер (вкладка
    закрыта/скрипт не подключён) — самостоятельно переключаемся на
    уведомления в этой программе, чтобы админ не пропускал сообщения."""
    if _notify_target[0] == 'admin_app':
        return True
    return not erp_is_alive()


# ───────────────────────── HTTP сервер ─────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path == '/status':
            with lock:
                t = now()
                out = {pc: {'online': (t - ts) <= ONLINE_TIMEOUT, 'lastSeen': ts} for pc, ts in last_seen.items()}
            return self._json(200, out)

        if u.path == '/messages':
            pc = (qs.get('pc') or [''])[0]
            since = int((qs.get('since') or ['0'])[0])
            want_enc = (qs.get('enc') or ['0'])[0] == '1'
            msgs = get_messages_since(pc, since)
            if want_enc:
                msgs = [dict(m, text=enc_text(m['text']), enc=True) for m in msgs]
            return self._json(200, msgs)

        if u.path == '/events':
            since = int((qs.get('since') or ['0'])[0])
            with lock:
                out = [m for m in messages if m['id'] > since]
            return self._json(200, out)

        if u.path == '/read_state':
            pc = (qs.get('pc') or [''])[0]
            with lock:
                out = {'readClient': read_client.get(pc, 0), 'readAdmin': read_admin.get(pc, 0)}
            return self._json(200, out)

        if u.path == '/settings':
            return self._json(200, {
                'notifyTarget': _notify_target[0],
                'showOnlineIndicator': _show_online_indicator[0],
                'notifySound': _notify_sound[0],
                'mutedUntil': _muted_until[0],
            })

        if u.path == '/chat_active':
            pc = (qs.get('pc') or [''])[0]
            return self._json(200, {'active': is_chat_active(pc)})

        if u.path == '/nickname':
            pc = (qs.get('pc') or [''])[0]
            return self._json(200, {'nickname': get_nickname(pc)})

        self._json(404, {'error': 'not found'})

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            data = {}

        if u.path == '/heartbeat':
            pc = str(data.get('pc', '')).strip()
            if not pc:
                return self._json(400, {'error': 'pc required'})
            with lock:
                last_seen[pc] = now()
            return self._json(200, {'ok': True})

        if u.path == '/erp_heartbeat':
            _erp_last_seen[0] = now()
            return self._json(200, {'ok': True})

        if u.path == '/send':
            pc = str(data.get('pc', '')).strip()
            frm = data.get('from', 'client')
            mtype = data.get('type', 'text')
            text = data.get('text', '')
            silent = bool(data.get('silent', False))
            if data.get('enc'):
                text = dec_text(text)
            if mtype == 'image':
                if not pc or not text:
                    return self._json(400, {'error': 'pc and image data required'})
                if len(text) > MAX_IMAGE_B64:
                    return self._json(400, {'error': 'image too large'})
            else:
                text = str(text).strip()
                if not pc or not text:
                    return self._json(400, {'error': 'pc and text required'})
            msg = post_message(pc, frm, mtype, text, silent=silent)
            return self._json(200, {'ok': True, 'id': msg['id']})

        if u.path == '/read':
            pc = str(data.get('pc', '')).strip()
            side = str(data.get('side', '')).strip()
            upto = int(data.get('upto', 0) or 0)
            if not pc or side not in ('client', 'admin'):
                return self._json(400, {'error': 'pc and side(client|admin) required'})
            with lock:
                store = read_client if side == 'client' else read_admin
                store[pc] = max(store.get(pc, 0), upto)
            return self._json(200, {'ok': True})

        if u.path == '/chat_active':
            pc = str(data.get('pc', '')).strip()
            if not pc:
                return self._json(400, {'error': 'pc required'})
            ping_chat_active(pc)
            return self._json(200, {'ok': True})

        if u.path == '/nickname':
            pc = str(data.get('pc', '')).strip()
            nickname = data.get('nickname')
            manual = bool(data.get('manual', False))
            if not pc or not nickname:
                return self._json(400, {'error': 'pc and nickname required'})
            set_nickname(pc, str(nickname).strip(), manual=manual)
            return self._json(200, {'ok': True})

        self._json(404, {'error': 'not found'})


def run_http_server():
    srv = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), Handler)
    print('[messenger] HTTP сервер запущен на порту %d' % HTTP_PORT)
    srv.serve_forever()


def beacon_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = json.dumps({'service': 'godji_messenger', 'port': HTTP_PORT}).encode('utf-8')
    while True:
        try:
            s.sendto(payload, ('255.255.255.255', BEACON_PORT))
        except Exception:
            pass
        time.sleep(BEACON_INTERVAL)


# ───────────────────────── скруглённые "пузыри" ─────────────────────────
def _rounded_points(x1, y1, x2, y2, r):
    return [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2, x2-r,y2, x1+r,y2,
            x1,y2, x1,y2-r, x1,y1+r, x1,y1]


def make_bubble(parent, text, bg, fg, wrap_px=220, font=('Segoe UI', 10), pad_x=12, pad_y=9, radius=14):
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


def build_message_menu(root, text=None, pil_img=None):
    menu = tk.Menu(root, tearoff=0, bg=PANEL, fg=TEXT_LIGHT, activebackground=ACCENT,
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


# ───────────────────────── стек уведомлений (максимум 4) ─────────────────────────
class ToastStack:
    CARD_H = 88
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

    def show(self, pc, text, on_click=None):
        self._ensure_window()
        card = tk.Frame(self.inner, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER,
                          width=self.WIDTH, height=self.CARD_H)
        card.pack_propagate(False)
        card.pack(fill='x', pady=(0, self.GAP))
        tk.Frame(card, bg=ACCENT, width=4).pack(side='left', fill='y')
        body = tk.Frame(card, bg=PANEL)
        body.pack(side='left', fill='both', expand=True, padx=10, pady=8)
        tk.Label(body, text='ПК ' + pc, bg=PANEL, fg=ACCENT, font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        tk.Label(body, text=text, bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 9),
                  wraplength=250, justify='left').pack(anchor='w', pady=(2, 0))

        def close(*_):
            try:
                card.destroy()
            except Exception:
                pass
            self._cards[:] = [c for c in self._cards if c[0] is not card]
            if self._cards:
                self._rebuild()
            else:
                self.win.destroy()
                self.win = None

        def click(*_):
            close()
            if on_click:
                on_click()

        card.bind('<Button-1>', click)
        for w_ in (body,) + tuple(body.winfo_children()):
            w_.bind('<Button-1>', click)

        after_id = card.after(7000, close)
        self._cards.append((card, after_id))
        self._rebuild()

        if _notify_sound[0] and not is_muted():
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass


_toast_stack = None


def show_admin_toast(root, pc, text):
    global _toast_stack
    if _toast_stack is None:
        _toast_stack = ToastStack(root)
    _toast_stack.show(pc, text, on_click=lambda: open_chat_for(root, pc))


def events_poll_loop(root):
    last_id = 0
    while True:
        with lock:
            new_msgs = [m for m in messages if m['id'] > last_id]
            if new_msgs:
                last_id = max(m['id'] for m in new_msgs)
        for m in new_msgs:
            if m['from'] != 'client':
                continue
            if not effective_notify_is_admin_app():
                continue
            if m.get('silent'):
                continue
            pc = m['pc']
            if pc in _open_chats:
                root.after(0, _open_chats[pc].on_new_message, m)
            else:
                preview = '📷 Изображение' if m.get('type') == 'image' else m['text']
                root.after(0, show_admin_toast, root, pc, preview)
        time.sleep(1)


# ───────────────────────── окно переписки с конкретным ПК ─────────────────────────
class AdminChatWindow:
    def __init__(self, root, pc):
        self.root = root
        self.pc = pc
        self._last_id = 0
        self._own_bubbles = []
        self._image_refs = []
        self._closed = False
        self._drag = {'x': 0, 'y': 0}

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg=BG)
        self.win.geometry('360x540+260+120')
        self.win.attributes('-topmost', False)

        border = tk.Frame(self.win, bg=PANEL_BORDER)
        border.pack(fill='both', expand=True, padx=1, pady=1)
        content = tk.Frame(border, bg=BG)
        content.pack(fill='both', expand=True)

        header = tk.Frame(content, bg=PANEL, height=48)
        header.pack(side='top', fill='x')
        header.pack_propagate(False)
        left = tk.Frame(header, bg=PANEL)
        left.pack(side='left', padx=14)
        tk.Label(left, text='●', bg=PANEL, fg=ACCENT, font=('Segoe UI', 11)).pack(side='left', padx=(0, 6))
        self.title_lbl = tk.Label(left, text=display_label(pc), bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 11, 'bold'))
        self.title_lbl.pack(side='left')
        pencil = tk.Label(left, text='✎', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 10))
        pencil.pack(side='left', padx=(8, 0))
        pencil.bind('<Button-1>', lambda e: self.rename())

        right = tk.Frame(header, bg=PANEL)
        right.pack(side='right', padx=10)
        trash = tk.Label(right, text='🗑', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 11))
        trash.pack(side='left', padx=6)
        trash.bind('<Button-1>', lambda e: self.clear_history())
        close_btn = tk.Label(right, text='—', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 13))
        close_btn.pack(side='left', padx=6)
        close_btn.bind('<Button-1>', lambda e: self.close())

        for w_ in (header, left):
            w_.bind('<Button-1>', self._drag_start)
            w_.bind('<B1-Motion>', self._drag_move)

        outer = tk.Frame(content, bg=BG)
        outer.pack(side='top', fill='both', expand=True)
        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient='vertical', command=self.canvas.yview)
        self.msg_frame = tk.Frame(self.canvas, bg=BG)
        self.msg_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self._win_id = self.canvas.create_window((0, 0), window=self.msg_frame, anchor='nw')
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfig(self._win_id, width=e.width))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.msg_frame)
        self._bind_wheel(outer)

        footer = tk.Frame(content, bg=PANEL)
        footer.pack(side='bottom', fill='x')
        tk.Frame(footer, bg=PANEL_BORDER, height=1).pack(side='top', fill='x')
        inner = tk.Frame(footer, bg=PANEL)
        inner.pack(fill='x', padx=10, pady=10)

        emoji_btn = tk.Label(inner, text='㋡', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 13))
        emoji_btn.pack(side='left', padx=(0, 4))
        emoji_btn.bind('<Button-1>', lambda e: self.open_emoji_panel())

        attach = tk.Label(inner, text='📎', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 12))
        attach.pack(side='left', padx=(0, 6))
        attach.bind('<Button-1>', lambda e: self.attach_image())

        entry_wrap = tk.Frame(inner, bg=ENTRY_BG, highlightthickness=1,
                                highlightbackground=PANEL_BORDER, highlightcolor=ACCENT)
        entry_wrap.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.entry = tk.Entry(entry_wrap, bg=ENTRY_BG, fg=TEXT_LIGHT, insertbackground=TEXT_LIGHT,
                               relief='flat', bd=0, font=('Segoe UI', 10))
        self.entry.pack(fill='x', ipady=7, padx=8)
        self.entry.bind('<Return>', self.send_text)
        self.entry.bind('<Control-v>', self._on_paste)

        send_btn = tk.Label(inner, text='➤', bg=ACCENT, fg='#fff', cursor='hand2', width=3,
                              font=('Segoe UI', 11, 'bold'))
        send_btn.pack(side='left')
        send_btn.bind('<Button-1>', self.send_text)
        send_btn.bind('<Button-3>', self._send_btn_menu)
        send_btn.bind('<Enter>', lambda e: send_btn.config(bg=ACCENT_HOVER))
        send_btn.bind('<Leave>', lambda e: send_btn.config(bg=ACCENT))

        for m in get_messages_since(pc, 0):
            self._render(m)
        mark_read_admin(pc, self._last_id)
        ping_chat_active(pc)

        self._poll_id = self.win.after(1000, self._poll)
        self.entry.focus_set()
        self.win.after(200, apply_rounded_corners, self.win)

    def _drag_start(self, event):
        self._drag['x'] = event.x
        self._drag['y'] = event.y

    def _drag_move(self, event):
        x = self.win.winfo_pointerx() - self._drag['x']
        y = self.win.winfo_pointery() - self._drag['y']
        self.win.geometry('+%d+%d' % (x, y))

    def _bind_wheel(self, widget):
        widget.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

    def _bind_menu(self, widget, text=None, pil_img=None):
        def show(e):
            menu = build_message_menu(self.win, text=text, pil_img=pil_img)
            menu.tk_popup(e.x_root, e.y_root)
        widget.bind('<Button-3>', show)

    def open_emoji_panel(self):
        try:
            self.entry.focus_force()
            VK_LWIN, VK_OEM_PERIOD = 0x5B, 0xBE
            def fire():
                ctypes.windll.user32.SetForegroundWindow(self.win.winfo_id())
                ctypes.windll.user32.keybd_event(VK_LWIN, 0, 0, 0)
                ctypes.windll.user32.keybd_event(VK_OEM_PERIOD, 0, 0, 0)
                ctypes.windll.user32.keybd_event(VK_OEM_PERIOD, 0, 2, 0)
                ctypes.windll.user32.keybd_event(VK_LWIN, 0, 2, 0)
            self.win.after(80, fire)
        except Exception:
            pass

    def _send_btn_menu(self, event):
        menu = tk.Menu(self.win, tearoff=0, bg=PANEL, fg=TEXT_LIGHT, activebackground=ACCENT,
                        activeforeground='#fff', bd=0, relief='flat')
        menu.add_command(label='🔇  Без звука у клиента', command=lambda: self.send_text(silent=True))
        menu.tk_popup(event.x_root, event.y_root)

    def rename(self):
        new_name = simpledialog.askstring('Переименовать', 'Ник для ПК %s:' % self.pc, parent=self.win)
        if new_name:
            set_nickname(self.pc, new_name.strip(), manual=True)
            self.title_lbl.config(text=display_label(self.pc))

    def _poll(self):
        if self._closed:
            return
        for m in get_messages_since(self.pc, self._last_id):
            self._render(m)
        mark_read_admin(self.pc, self._last_id)
        ping_chat_active(self.pc)
        self._refresh_ticks()
        self.title_lbl.config(text=display_label(self.pc))
        self._poll_id = self.win.after(1000, self._poll)

    def _render(self, m):
        self._last_id = max(self._last_id, m['id'])
        mine = m['from'] == 'admin'
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', padx=12, pady=4)

        if m.get('type') == 'image':
            try:
                raw = base64.b64decode(m['text'])
                full_img = Image.open(io.BytesIO(raw))
                thumb = full_img.copy()
                thumb.thumbnail((230, 230))
                photo = ImageTk.PhotoImage(thumb)
                self._image_refs.append(photo)
                wrap = tk.Frame(row, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG)
                wrap.pack(side='right' if mine else 'left')
                lbl = tk.Label(wrap, image=photo, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG, cursor='hand2', bd=0)
                lbl.pack(padx=4, pady=4)
                lbl.bind('<Button-1>', lambda e, im=full_img: self._open_full(im))
                self._bind_menu(lbl, pil_img=full_img)
                self._bind_wheel(wrap); self._bind_wheel(lbl)
            except Exception:
                tk.Label(row, text='[изображение не загрузилось]', bg=BG, fg=SYSTEM_TEXT,
                          font=('Segoe UI', 8, 'italic')).pack(side='left')
        else:
            bubble = make_bubble(row, m['text'], BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG,
                                   '#fff' if mine else TEXT_LIGHT)
            bubble.pack(side='right' if mine else 'left')
            self._bind_menu(bubble, text=m['text'])
            self._bind_wheel(bubble)

        meta = tk.Label(row, text=time.strftime('%H:%M', time.localtime(m['ts'])) + (' ✓' if mine else ''),
                          bg=BG, fg=MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w', pady=(2, 0))
        if mine:
            self._own_bubbles.append((m['id'], meta))

        self._bind_wheel(row)
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def _refresh_ticks(self):
        rc = get_read_client(self.pc)
        for mid, lbl in self._own_bubbles:
            try:
                base_ts = lbl.cget('text').split(' ')[0]
                if mid <= rc:
                    lbl.config(text=base_ts + ' ✓✓', fg=TEXT_READ)
                else:
                    lbl.config(text=base_ts + ' ✓', fg=MUTED)
            except Exception:
                pass

    def send_text(self, event=None, silent=False):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, 'end')
        m = post_message(self.pc, 'admin', 'text', text, silent=silent)
        self._render(m)

    def send_image_pil(self, img):
        img = img.convert('RGB')
        img.thumbnail((900, 900))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=78)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        m = post_message(self.pc, 'admin', 'image', b64)
        self._render(m)

    def attach_image(self):
        path = filedialog.askopenfilename(
            title='Выбери изображение',
            filetypes=[('Изображения', '*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp')])
        if not path:
            return
        try:
            self.send_image_pil(Image.open(path))
        except Exception:
            pass

    def _on_paste(self, event=None):
        clip = None
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            pass
        if isinstance(clip, Image.Image):
            self.send_image_pil(clip)
            return 'break'
        if isinstance(clip, list) and clip:
            for path in clip:
                try:
                    self.send_image_pil(Image.open(path))
                    return 'break'
                except Exception:
                    continue
        return None

    def _open_full(self, img):
        try:
            fd, path = tempfile.mkstemp(suffix='.png')
            os.close(fd)
            img.save(path)
            os.startfile(path)
        except Exception:
            pass

    def clear_history(self):
        clear_messages(self.pc)
        for w in self.msg_frame.winfo_children():
            w.destroy()
        self._own_bubbles = []
        self._last_id = 0

    def on_new_message(self, m):
        self._render(m)
        mark_read_admin(self.pc, self._last_id)

    def raise_(self):
        self.win.deiconify()
        self.win.lift()
        self.win.attributes('-topmost', True)
        self.win.attributes('-topmost', False)
        self.win.focus_force()

    def close(self):
        self._closed = True
        try:
            self.win.after_cancel(self._poll_id)
        except Exception:
            pass
        _open_chats.pop(self.pc, None)
        self.win.destroy()


def open_chat_for(root, pc):
    if pc in _open_chats:
        _open_chats[pc].raise_()
    else:
        _open_chats[pc] = AdminChatWindow(root, pc)


# ───────────────────────── ГЛАВНОЕ ОКНО ─────────────────────────
class MainWindow:
    SIDEBAR_COLLAPSED = 56
    SIDEBAR_EXPANDED = 220

    def __init__(self, root):
        self.root = root
        self.expanded = False
        self.win = root
        self.win.overrideredirect(True)
        self.win.configure(bg=BG)
        w, h = 780, 560
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
        self.win.attributes('-topmost', False)
        self._drag = {'x': 0, 'y': 0}

        border = tk.Frame(self.win, bg=PANEL_BORDER)
        border.pack(fill='both', expand=True, padx=1, pady=1)
        root_content = tk.Frame(border, bg=BG)
        root_content.pack(fill='both', expand=True)

        titlebar = tk.Frame(root_content, bg=PANEL, height=44)
        titlebar.pack(side='top', fill='x')
        titlebar.pack_propagate(False)
        tk.Label(titlebar, text='Godji Messenger — Администратор', bg=PANEL, fg=TEXT_LIGHT,
                  font=('Segoe UI', 10, 'bold')).pack(side='left', padx=16)
        close_btn = tk.Label(titlebar, text='—', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 13), padx=14)
        close_btn.pack(side='right')
        close_btn.bind('<Button-1>', lambda e: self.win.withdraw())
        titlebar.bind('<Button-1>', self._drag_start)
        titlebar.bind('<B1-Motion>', self._drag_move)

        body = tk.Frame(root_content, bg=BG)
        body.pack(side='top', fill='both', expand=True)

        self.sidebar = tk.Frame(body, bg=PANEL2, width=self.SIDEBAR_COLLAPSED)
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)

        burger = tk.Label(self.sidebar, text='☰', bg=PANEL2, fg=TEXT_LIGHT, font=('Segoe UI', 14), cursor='hand2')
        burger.pack(side='top', pady=14)
        burger.bind('<Button-1>', lambda e: self.toggle_sidebar())

        self.nav_buttons = []
        self._add_nav('🕒', 'Недавние чаты', self.show_recent)
        self._add_nav('💬', 'Все чаты', self.show_all_chats)

        bottom = tk.Frame(self.sidebar, bg=PANEL2)
        bottom.pack(side='bottom', fill='x', pady=10)
        self._settings_btn = self._add_nav('⚙', 'Настройки', self.show_settings, parent=bottom, pack_side='bottom')

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(side='left', fill='both', expand=True)

        self.show_recent()
        self.win.after(200, apply_rounded_corners, self.win)

    def _drag_start(self, event):
        self._drag['x'] = event.x
        self._drag['y'] = event.y

    def _drag_move(self, event):
        x = self.win.winfo_pointerx() - self._drag['x']
        y = self.win.winfo_pointery() - self._drag['y']
        self.win.geometry('+%d+%d' % (x, y))

    def _add_nav(self, icon, label, cmd, parent=None, pack_side='top'):
        parent = parent or self.sidebar
        row = tk.Frame(parent, bg=PANEL2, cursor='hand2')
        row.pack(side=pack_side, fill='x', pady=2)
        ic = tk.Label(row, text=icon, bg=PANEL2, fg=TEXT_LIGHT, font=('Segoe UI', 13), width=3)
        ic.pack(side='left', pady=8)
        lbl = tk.Label(row, text=label, bg=PANEL2, fg=TEXT_LIGHT, font=('Segoe UI', 10))
        if self.expanded:
            lbl.pack(side='left')
        for w_ in (row, ic, lbl):
            w_.bind('<Button-1>', lambda e: cmd())
            w_.bind('<Enter>', lambda e: row.config(bg='#2c1a1d'))
            w_.bind('<Leave>', lambda e: row.config(bg=PANEL2))
        row._label_widget = lbl
        self.nav_buttons.append(row)
        return row

    def toggle_sidebar(self):
        self.expanded = not self.expanded
        self.sidebar.config(width=self.SIDEBAR_EXPANDED if self.expanded else self.SIDEBAR_COLLAPSED)
        for row in self.nav_buttons:
            lbl = row._label_widget
            if self.expanded:
                lbl.pack(side='left')
            else:
                lbl.pack_forget()

    def _clear_content(self):
        for w in self.content.winfo_children():
            w.destroy()

    def show_recent(self):
        self._clear_content()
        header = tk.Frame(self.content, bg=BG)
        header.pack(fill='x', padx=18, pady=(16, 8))
        tk.Label(header, text='Недавние чаты', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 13, 'bold')).pack(side='left')

        listwrap = tk.Frame(self.content, bg=BG)
        listwrap.pack(fill='both', expand=True, padx=12, pady=4)
        canvas = tk.Canvas(listwrap, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(listwrap, orient='vertical', command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        win_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        canvas.bind('<MouseWheel>', lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

        recent = get_recent_chats()
        if not recent:
            tk.Label(inner, text='Пока нет переписки', bg=BG, fg=MUTED, font=('Segoe UI', 9, 'italic')).pack(pady=24)
        for m in recent:
            pc = m['pc']
            row = tk.Frame(inner, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER, cursor='hand2')
            row.pack(fill='x', pady=5)
            rin = tk.Frame(row, bg=PANEL)
            rin.pack(fill='x', padx=14, pady=10)
            top = tk.Frame(rin, bg=PANEL)
            top.pack(fill='x')
            tk.Label(top, text=display_label(pc), bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 10, 'bold')).pack(side='left')
            tk.Label(top, text=time.strftime('%d.%m %H:%M', time.localtime(m['ts'])),
                      bg=PANEL, fg=MUTED, font=('Segoe UI', 8)).pack(side='right')
            preview = '📷 Изображение' if m.get('type') == 'image' else m['text']
            prefix = 'Вы: ' if m['from'] == 'admin' else ''
            tk.Label(rin, text=(prefix + preview)[:70], bg=PANEL, fg=MUTED, font=('Segoe UI', 9),
                      anchor='w').pack(fill='x', pady=(3, 0))
            for w_ in (row, rin, top):
                w_.bind('<Button-1>', lambda e, pc=pc: open_chat_for(self.root, pc))

    def show_all_chats(self):
        self._clear_content()
        header = tk.Frame(self.content, bg=BG)
        header.pack(fill='x', padx=18, pady=(16, 8))
        tk.Label(header, text='Все чаты', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 13, 'bold')).pack(side='left')

        listwrap = tk.Frame(self.content, bg=BG)
        listwrap.pack(fill='both', expand=True, padx=12, pady=4)
        canvas = tk.Canvas(listwrap, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(listwrap, orient='vertical', command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        win_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        canvas.bind('<MouseWheel>', lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

        pcs = get_all_pcs()
        if not pcs:
            tk.Label(inner, text='Пока ни один ПК не выходил на связь', bg=BG, fg=MUTED,
                      font=('Segoe UI', 9, 'italic')).pack(pady=24)
        for item in pcs:
            pc = item['pc']
            row = tk.Frame(inner, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER, cursor='hand2')
            row.pack(fill='x', pady=4)
            rin = tk.Frame(row, bg=PANEL)
            rin.pack(fill='x', padx=14, pady=9)
            dot = tk.Label(rin, text='●', bg=PANEL, fg=ONLINE_GREEN if item['online'] else MUTED, font=('Segoe UI', 9))
            dot.pack(side='left', padx=(0, 8))
            tk.Label(rin, text=display_label(pc), bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 10)).pack(side='left')
            tk.Label(rin, text='онлайн' if item['online'] else 'офлайн', bg=PANEL,
                      fg=ONLINE_GREEN if item['online'] else MUTED, font=('Segoe UI', 8)).pack(side='right')
            for w_ in (row, rin, dot):
                w_.bind('<Button-1>', lambda e, pc=pc: open_chat_for(self.root, pc))

    def show_settings(self):
        self._clear_content()
        header = tk.Frame(self.content, bg=BG)
        header.pack(fill='x', padx=18, pady=(16, 8))
        tk.Label(header, text='Настройки', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 13, 'bold')).pack(side='left')

        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill='both', expand=True, padx=22, pady=6)

        tk.Label(wrap, text='Уведомления о новых сообщениях от клиентов:', bg=BG, fg=TEXT_LIGHT,
                  font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(6, 6))
        var_target = tk.StringVar(value=_notify_target[0])

        def on_target_change():
            _notify_target[0] = var_target.get()
            cfg = load_config(); cfg['notify_target'] = var_target.get(); save_config(cfg)

        tk.Radiobutton(wrap, text='В ERP (в браузере)', variable=var_target, value='erp', command=on_target_change,
                        bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT_LIGHT,
                        font=('Segoe UI', 10)).pack(anchor='w', padx=14, pady=2)
        tk.Radiobutton(wrap, text='В этой программе', variable=var_target, value='admin_app', command=on_target_change,
                        bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT_LIGHT,
                        font=('Segoe UI', 10)).pack(anchor='w', padx=14, pady=2)
        tk.Label(wrap, text='Если выбран ERP, но вкладка браузера закрыта — уведомления временно\nсами переключаются сюда, чтобы ничего не потерять.',
                  bg=BG, fg=MUTED, font=('Segoe UI', 8)).pack(anchor='w', padx=14, pady=(2, 0))

        tk.Frame(wrap, bg=PANEL_BORDER, height=1).pack(fill='x', pady=16)

        tk.Label(wrap, text='На клиентской стороне:', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 10, 'bold')).pack(anchor='w')

        var_online = tk.BooleanVar(value=_show_online_indicator[0])
        def on_online_change():
            _show_online_indicator[0] = var_online.get()
            cfg = load_config(); cfg['show_online_indicator'] = var_online.get(); save_config(cfg)
        tk.Checkbutton(wrap, text='Показывать индикатор "админ на связи"', variable=var_online, command=on_online_change,
                        bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT_LIGHT,
                        font=('Segoe UI', 9)).pack(anchor='w', padx=14, pady=(8, 2))

        var_sound = tk.BooleanVar(value=_notify_sound[0])
        def on_sound_change():
            _notify_sound[0] = var_sound.get()
            cfg = load_config(); cfg['notify_sound'] = var_sound.get(); save_config(cfg)
        tk.Checkbutton(wrap, text='Звук уведомлений', variable=var_sound, command=on_sound_change,
                        bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT_LIGHT,
                        font=('Segoe UI', 9)).pack(anchor='w', padx=14, pady=2)

        tk.Frame(wrap, bg=PANEL_BORDER, height=1).pack(fill='x', pady=16)

        tk.Label(wrap, text='Временно отключить звук:', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        mute_row = tk.Frame(wrap, bg=BG)
        mute_row.pack(anchor='w', padx=14, pady=8)

        self._mute_status = tk.Label(wrap, text='', bg=BG, fg=MUTED, font=('Segoe UI', 8, 'italic'))
        self._mute_status.pack(anchor='w', padx=14)

        def do_mute(minutes=None, session=False):
            mute_for(minutes=minutes, session=session)
            self._update_mute_status()

        for mins in (5, 10, 15, 30):
            b = tk.Label(mute_row, text='%d мин' % mins, bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 9),
                          cursor='hand2', padx=10, pady=5)
            b.pack(side='left', padx=4)
            b.bind('<Button-1>', lambda e, m=mins: do_mute(minutes=m))
        b_session = tk.Label(mute_row, text='на сеанс', bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 9),
                               cursor='hand2', padx=10, pady=5)
        b_session.pack(side='left', padx=4)
        b_session.bind('<Button-1>', lambda e: do_mute(session=True))
        b_unmute = tk.Label(mute_row, text='включить обратно', bg=ACCENT, fg='#fff', font=('Segoe UI', 9),
                              cursor='hand2', padx=10, pady=5)
        b_unmute.pack(side='left', padx=4)
        b_unmute.bind('<Button-1>', lambda e: (unmute(), self._update_mute_status()))

        self._update_mute_status()

    def _update_mute_status(self):
        if not hasattr(self, '_mute_status'):
            return
        if is_muted():
            remain = int(_muted_until[0] - now())
            mins = max(1, remain // 60)
            self._mute_status.config(text='Звук выключен ещё ~%d мин.' % mins)
        else:
            self._mute_status.config(text='Звук включён.')

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()


# ───────────────────────── трей ─────────────────────────
def make_icon_image():
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 6, 60, 46), radius=13, fill='#d4172a')
    d.polygon([(16, 44), (16, 58), (32, 44)], fill='#d4172a')
    for cx in (20, 32, 44):
        d.ellipse((cx - 4, 22, cx + 4, 30), fill='#ffffff')
    return img


def run_tray(root, main_win):
    if pystray is None:
        print('[messenger] pystray не установлен — трея не будет, но остальное работает.')
        while True:
            time.sleep(3600)

    def on_show(icon, item=None):
        root.after(0, main_win.show)

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    icon = pystray.Icon(APP_NAME, make_icon_image(), 'Godji Messenger — сервер запущен', menu=pystray.Menu(
        pystray.MenuItem('Открыть', on_show, default=True),
        pystray.MenuItem('Выход', on_quit),
    ))
    icon.run()


# ───────────────────────── мастер первого запуска ─────────────────────────
def run_wizard(on_done):
    wiz = tk.Tk()
    wiz.overrideredirect(True)
    wiz.configure(bg=BG)
    w, h = 380, 190
    sw, sh = wiz.winfo_screenwidth(), wiz.winfo_screenheight()
    wiz.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
    wiz.attributes('-topmost', True)
    wiz.after(200, apply_rounded_corners, wiz)

    tk.Frame(wiz, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(wiz, text='Godji Messenger', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 14, 'bold')).pack(pady=(22, 2))
    tk.Label(wiz, text='Первый запуск — настройка сервера', bg=BG, fg=MUTED, font=('Segoe UI', 9)).pack()
    status = tk.Label(wiz, text='Подготовка…', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 9))
    status.pack(pady=18)

    steps = [
        ('Регистрируем автозапуск…', install_autostart),
        ('Настраиваем брандмауэр (может появиться запрос Windows)…', add_firewall_rule),
    ]
    idx = [0]

    def next_step():
        if idx[0] >= len(steps):
            status.config(text='Готово! Сервер запускается…')
            wiz.update()
            cfg = load_config()
            cfg['configured'] = True
            save_config(cfg)
            wiz.after(900, lambda: (wiz.destroy(), on_done()))
            return
        text, fn = steps[idx[0]]
        idx[0] += 1
        status.config(text=text)
        wiz.update()
        try:
            fn()
        except Exception as e:
            print('[messenger] Ошибка шага настройки:', e)
        wiz.after(1300, next_step)

    wiz.after(500, next_step)
    wiz.mainloop()


def start_services():
    cfg = load_config()
    _notify_target[0] = cfg.get('notify_target', 'erp')
    _show_online_indicator[0] = cfg.get('show_online_indicator', True)
    _notify_sound[0] = cfg.get('notify_sound', True)
    _muted_until[0] = float(cfg.get('muted_until', 0) or 0)

    root = tk.Tk()
    main_win = MainWindow(root)

    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=beacon_loop, daemon=True).start()
    threading.Thread(target=events_poll_loop, args=(root,), daemon=True).start()
    threading.Thread(target=run_tray, args=(root, main_win), daemon=True).start()

    root.mainloop()


def main():
    enable_dpi_awareness()
    cfg = load_config()
    if cfg.get('configured'):
        start_services()
    else:
        run_wizard(start_services)


if __name__ == '__main__':
    main()
