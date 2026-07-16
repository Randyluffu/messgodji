#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godji Messenger — АДМИНСКОЕ приложение.

Первый запуск: сама регистрирует автозапуск, добавляет правило
брандмауэра (один раз спросит подтверждение Windows/UAC), затем
открывает главное окно (видно в панели задач и по Alt-Tab). Повторный
запуск exe, пока приложение уже работает, не плодит вторую копию —
просто поднимает окно уже запущенного экземпляра.

Главное окно: слева сжатый сайдбар (бургер разворачивает его с подписями) —
Недавние чаты, Все чаты, Настройки.

Сервер рассылает по локальной сети UDP-маячок со всех сетевых адаптеров —
клиенты сами находят IP этого ПК. Трафик клиент<->сервер (единственный
участок, идущий по реальной сети клуба) шифруется общим ключом.

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
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winsound
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog

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
CHAT_ACTIVE_TIMEOUT = 5
ERP_ALIVE_TIMEOUT = 20
HISTORY_TTL = 24 * 3600
MAX_ATTACHMENT_B64 = 8 * 1024 * 1024
RADIUS = 14
EMOJI_SET = ['😀','😂','😉','😎','🙂','😅','🥲','😢','😡','🤔',
             '👍','👎','🙏','👏','🔥','💯','❤️','✅','❌','⏰',
             '💰','🎮','🖥️','❓','😴','🥳']

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
MUTEX_NAME = 'Global\\GodjiMessengerAdminMutex'

_notify_target = ['erp']
_show_online_indicator = [True]
_notify_sound = [True]
_open_chats = {}
_erp_last_seen = [0.0]
_show_window_event = threading.Event()
_image_cache = []


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
    if not _notify_sound[0]:
        return
    try:
        winsound.Beep(880, 90)
        winsound.Beep(1175, 110)
    except Exception:
        try:
            winsound.MessageBeep(-1)
        except Exception:
            pass


# ───────────────────────── скруглённые окна (GDI — работает на всех Windows) ─────────────────────────
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


def make_appwindow(win):
    """Заставляет окно без рамки (overrideredirect) всё равно появляться
    в панели задач и по Alt-Tab, как обычное приложение."""
    try:
        win.update_idletasks()
        hwnd = win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        win.withdraw()
        win.after(10, win.deiconify)
    except Exception:
        pass


def setup_ttk_style():
    style = ttk.Style()
    try:
        style.theme_use('clam')
    except Exception:
        pass
    style.configure('Godji.Vertical.TScrollbar', background=PANEL_BORDER, troughcolor=BG,
                     bordercolor=BG, arrowcolor=MUTED, relief='flat', gripcount=0, width=8)
    style.map('Godji.Vertical.TScrollbar', background=[('active', ACCENT), ('!active', PANEL_BORDER)])


def try_acquire_singleton():
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    ERROR_ALREADY_EXISTS = 183
    return mutex, (ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS)


def signal_existing_instance():
    try:
        urllib.request.urlopen('http://127.0.0.1:%d/show_window' % HTTP_PORT, data=b'{}', timeout=2)
    except Exception as e:
        print('[messenger] Не удалось достучаться до уже запущенного экземпляра:', e)


# ───────────────────────── шифрование ─────────────────────────
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
    args = ('advfirewall firewall add rule name="Godji Messenger Admin" '
            'dir=in action=allow protocol=TCP localport=%d program="%s" '
            'enable=yes profile=any') % (HTTP_PORT, exe)
    try:
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'netsh', args, None, 0)
    except Exception as e:
        print('[messenger] Ошибка брандмауэра:', e)


def verify_firewall_rule():
    try:
        r = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule',
                             'name=Godji Messenger Admin'], capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and 'No rules match' not in r.stdout
    except Exception:
        return False


# ───────────────────────── общее хранилище ─────────────────────────
lock = threading.Lock()
last_seen = {}
messages = []
_next_id = 1
read_client = {}
read_admin = {}
chat_active = {}
pc_nicknames = {}
pc_muted_until = {}


def now():
    return time.time()


def post_message(pc, frm, mtype, text, silent=False, filename=None):
    global _next_id
    with lock:
        msg = {'id': _next_id, 'pc': pc, 'from': frm, 'type': mtype, 'text': text,
               'ts': now(), 'silent': bool(silent)}
        if filename:
            msg['filename'] = filename
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
    with lock:
        t = now()
        seen_pcs = set(last_seen.keys()) | {m['pc'] for m in messages}
        out = [{'pc': pc, 'online': (t - last_seen.get(pc, 0)) <= ONLINE_TIMEOUT} for pc in seen_pcs]

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
            return
        pc_nicknames[pc] = {'nickname': nickname, 'manual': manual, 'ts': now()}


def display_label(pc):
    nick = get_nickname(pc)
    return ('%s — ПК %s' % (nick, pc)) if nick else ('ПК ' + pc)


def is_pc_muted(pc):
    with lock:
        return now() < pc_muted_until.get(pc, 0)


def mute_pc_for(pc, minutes=None, session=False):
    with lock:
        pc_muted_until[pc] = now() + (24 * 3600 if session else (minutes or 0) * 60)


def unmute_pc(pc):
    with lock:
        pc_muted_until.pop(pc, None)


def mute_all(minutes=None, session=False):
    with lock:
        pcs = set(last_seen.keys()) | {m['pc'] for m in messages}
        until = now() + (24 * 3600 if session else (minutes or 0) * 60)
        for pc in pcs:
            pc_muted_until[pc] = until


def erp_is_alive():
    return (now() - _erp_last_seen[0]) <= ERP_ALIVE_TIMEOUT


def effective_notify_is_admin_app():
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
            })

        if u.path == '/mute_state':
            pc = (qs.get('pc') or [''])[0]
            with lock:
                return self._json(200, {'mutedUntil': pc_muted_until.get(pc, 0)})

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

        if u.path == '/show_window':
            _show_window_event.set()
            return self._json(200, {'ok': True})

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
            filename = data.get('filename')
            silent = bool(data.get('silent', False))
            if data.get('enc'):
                text = dec_text(text)
            if mtype in ('image', 'file'):
                if not pc or not text:
                    return self._json(400, {'error': 'pc and attachment data required'})
                if len(text) > MAX_ATTACHMENT_B64:
                    return self._json(400, {'error': 'attachment too large'})
                if mtype == 'file' and not filename:
                    return self._json(400, {'error': 'filename required for file'})
            else:
                text = str(text).strip()
                if not pc or not text:
                    return self._json(400, {'error': 'pc and text required'})
            msg = post_message(pc, frm, mtype, text, silent=silent, filename=filename)
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

        if u.path == '/mute':
            pc = str(data.get('pc', '')).strip()
            if not pc:
                return self._json(400, {'error': 'pc required'})
            mute_pc_for(pc, minutes=data.get('minutes'), session=bool(data.get('session')))
            return self._json(200, {'ok': True})

        if u.path == '/unmute':
            pc = str(data.get('pc', '')).strip()
            if not pc:
                return self._json(400, {'error': 'pc required'})
            unmute_pc(pc)
            return self._json(200, {'ok': True})

        self._json(404, {'error': 'not found'})


def run_http_server():
    srv = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), Handler)
    print('[messenger] HTTP сервер запущен на порту %d' % HTTP_PORT)
    srv.serve_forever()


def get_local_ipv4_list():
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return [ip for ip in ips if not ip.startswith('127.')]


def beacon_loop():
    payload = json.dumps({'service': 'godji_messenger', 'port': HTTP_PORT}).encode('utf-8')
    while True:
        ips = get_local_ipv4_list() or [None]
        for local_ip in ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if local_ip:
                    try:
                        s.bind((local_ip, 0))
                    except Exception:
                        pass
                    parts = local_ip.split('.')
                    if len(parts) == 4:
                        directed = '.'.join(parts[:3] + ['255'])
                        try:
                            s.sendto(payload, (directed, BEACON_PORT))
                        except Exception:
                            pass
                try:
                    s.sendto(payload, ('255.255.255.255', BEACON_PORT))
                except Exception:
                    pass
                s.close()
            except Exception:
                pass
        time.sleep(BEACON_INTERVAL)


# ═══════════════ ЕДИНАЯ СИСТЕМА ДИЗАЙНА ═══════════════
def rounded_rect_photo(w, h, radius, color, scale=4):
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
                     w=40, h=34, font=('Segoe UI', 12, 'bold'), radius=RADIUS):
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
    def __init__(self, parent):
        self.parent = parent
        self.win = None
        self.items = []

    def add_command(self, label, command, enabled=True):
        self.items.append(('cmd', label, command, enabled))

    def add_separator(self):
        self.items.append(('sep', None, None, None))

    def popup(self, x, y):
        win = tk.Toplevel(self.parent)
        self.win = win
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=PANEL)
        frame = tk.Frame(win, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER)
        frame.pack()
        for kind, label, cmd, enabled in self.items:
            if kind == 'sep':
                tk.Frame(frame, bg=PANEL_BORDER, height=1).pack(fill='x', padx=8, pady=4)
                continue
            row = tk.Label(frame, text=label, bg=PANEL, fg=(TEXT_LIGHT if enabled else MUTED),
                            font=('Segoe UI', 9), anchor='w', padx=14, pady=7,
                            cursor='hand2' if enabled else 'arrow')
            row.pack(fill='x')
            if enabled:
                row.bind('<Button-1>', lambda e, c=cmd: (self.close(), c()))
                row.bind('<Enter>', lambda e, w_=row: w_.config(bg=ACCENT))
                row.bind('<Leave>', lambda e, w_=row: w_.config(bg=PANEL))
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


def show_menu(parent, x, y, items):
    m = CustomMenu(parent)
    for it in items:
        if it is None:
            m.add_separator()
        else:
            m.add_command(*it)
    m.popup(x, y)
    return m


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

    return outer, inner, canvas


# ───────────────────────── стек уведомлений — ПРАВЫЙ ВЕРХНИЙ УГОЛ ─────────────────────────
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
        h = max(min(len(self._cards), MAX_TOASTS_VISIBLE), 1) * (self.CARD_H + self.GAP)
        self.win.geometry('%dx%d+%d+%d' % (self.WIDTH, h, sw - self.WIDTH - 20, 20))
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

        after_id = card.after(7000, close)
        self._cards.append((card, after_id))
        self._rebuild()

        if not is_pc_muted(pc):
            play_chime()


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
                if m.get('type') == 'image':
                    preview = '📷 Изображение'
                elif m.get('type') == 'file':
                    preview = '📄 Файл: ' + m.get('filename', '')
                else:
                    preview = m['text']
                root.after(0, show_admin_toast, root, pc, preview)
        time.sleep(0.5)


# ───────────────────────── окно переписки с конкретным ПК ─────────────────────────
class AdminChatWindow:
    def __init__(self, root, pc):
        self.root = root
        self.pc = pc
        self._last_id = 0
        self._own_bubbles = []
        self._closed = False
        self._focused = True
        self._drag = {'x': 0, 'y': 0}
        self._emoji_panel = None

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg=BG)
        self.win.geometry('360x540+260+120')
        self.win.attributes('-topmost', False)
        self.win.bind('<FocusIn>', self._on_focus_in)
        self.win.bind('<FocusOut>', self._on_focus_out)

        header = tk.Frame(self.win, bg=PANEL, height=48)
        header.pack(side='top', fill='x')
        header.pack_propagate(False)
        header.bind('<Button-3>', self._mute_menu)
        left = tk.Frame(header, bg=PANEL)
        left.pack(side='left', padx=14)
        tk.Label(left, text='●', bg=PANEL, fg=ACCENT, font=('Segoe UI', 11)).pack(side='left', padx=(0, 6))
        self.title_lbl = tk.Label(left, text=display_label(pc), bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 11, 'bold'))
        self.title_lbl.pack(side='left')
        pencil = tk.Label(left, text='✎', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 10))
        pencil.pack(side='left', padx=(8, 0))
        pencil.bind('<Button-1>', lambda e: self.rename())
        self.mute_dot = tk.Label(left, text='🔇', bg=PANEL, fg=MUTED, font=('Segoe UI', 9))
        self.mute_dot.pack(side='left', padx=(8, 0))
        self._refresh_mute_dot()

        right = tk.Frame(header, bg=PANEL)
        right.pack(side='right', padx=10)
        export_btn = tk.Label(right, text='⬇', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 11))
        export_btn.pack(side='left', padx=6)
        export_btn.bind('<Button-1>', lambda e: self.export_history())
        trash = tk.Label(right, text='🗑', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 11))
        trash.pack(side='left', padx=6)
        trash.bind('<Button-1>', lambda e: self.clear_history())
        close_btn = tk.Label(right, text='—', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 13))
        close_btn.pack(side='left', padx=6)
        close_btn.bind('<Button-1>', lambda e: self.close())

        for w_ in (header, left):
            w_.bind('<Button-1>', self._drag_start)
            w_.bind('<B1-Motion>', self._drag_move)

        scroll_outer, self.msg_frame, self.canvas = make_scroll_area(self.win, bg=BG)
        scroll_outer.pack(side='top', fill='both', expand=True)
        scroll_outer.bind('<Button-3>', self._mute_menu)

        footer = tk.Frame(self.win, bg=PANEL)
        footer.pack(side='bottom', fill='x')
        tk.Frame(footer, bg=PANEL_BORDER, height=1).pack(side='top', fill='x')
        inner = tk.Frame(footer, bg=PANEL)
        inner.pack(fill='x', padx=10, pady=10)

        emoji_btn = tk.Label(inner, text='☺', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 14))
        emoji_btn.pack(side='left', padx=(0, 4))
        emoji_btn.bind('<Button-1>', lambda e: self.toggle_emoji_panel())

        attach = tk.Label(inner, text='📎', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 12))
        attach.pack(side='left', padx=(0, 6))
        attach.bind('<Button-1>', lambda e: self.attach_file())

        entry_wrap = tk.Frame(inner, bg=ENTRY_BG, highlightthickness=1,
                                highlightbackground=PANEL_BORDER, highlightcolor=ACCENT)
        entry_wrap.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.entry = tk.Entry(entry_wrap, bg=ENTRY_BG, fg=TEXT_LIGHT, insertbackground=TEXT_LIGHT,
                               relief='flat', bd=0, font=('Segoe UI', 10))
        self.entry.pack(fill='x', ipady=7, padx=8)
        self.entry.bind('<Return>', self.send_text)
        self.entry.bind('<Control-v>', self._on_paste)

        send_btn = rounded_button(inner, '➤', lambda: self.send_text())
        send_btn.pack(side='left')
        send_btn.bind('<Button-3>', self._send_btn_menu)

        for m in get_messages_since(pc, 0):
            self._render(m)
        mark_read_admin(pc, self._last_id)
        ping_chat_active(pc)

        self._poll_id = self.win.after(700, self._poll)
        self.entry.focus_set()
        self.win.after(150, apply_rounded_corners, self.win)

    def _on_focus_in(self, event=None):
        if event is None or event.widget == self.win:
            self._focused = True

    def _on_focus_out(self, event=None):
        if event is None or event.widget == self.win:
            self._focused = False

    def _drag_start(self, event):
        self._drag['x'] = event.x
        self._drag['y'] = event.y

    def _drag_move(self, event):
        x = self.win.winfo_pointerx() - self._drag['x']
        y = self.win.winfo_pointery() - self._drag['y']
        self.win.geometry('+%d+%d' % (x, y))

    def _bind_menu(self, widget, text=None, pil_img=None):
        widget.bind('<Button-3>', lambda e: build_message_menu(self.win, e.x_root, e.y_root, text=text, pil_img=pil_img))

    def toggle_emoji_panel(self):
        if self._emoji_panel is not None:
            self._emoji_panel.destroy()
            self._emoji_panel = None
            return
        panel = tk.Frame(self.win, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER)
        self._emoji_panel = panel
        panel.place(x=10, rely=1.0, y=-64, anchor='sw')
        grid = tk.Frame(panel, bg=PANEL)
        grid.pack(padx=6, pady=6)
        cols = 7
        for i, em in enumerate(EMOJI_SET):
            b = tk.Label(grid, text=em, bg=PANEL, font=('Segoe UI Emoji', 15), cursor='hand2', padx=5, pady=4)
            b.grid(row=i // cols, column=i % cols)
            b.bind('<Button-1>', lambda e, ch=em: self._insert_emoji(ch))
            b.bind('<Enter>', lambda e, w_=b: w_.config(bg=BUBBLE_ADMIN_BG))
            b.bind('<Leave>', lambda e, w_=b: w_.config(bg=PANEL))

    def _insert_emoji(self, ch):
        self.entry.insert('insert', ch)
        self.entry.focus_set()

    def _send_btn_menu(self, event):
        show_menu(self.win, event.x_root, event.y_root,
                   [('🔇  Без звука у клиента', lambda: self.send_text(silent=True))])

    def _refresh_mute_dot(self):
        self.mute_dot.config(fg=ACCENT if is_pc_muted(self.pc) else MUTED)

    def _mute_menu(self, event):
        items = [('Заглушить на %d мин' % m, (lambda m=m: (mute_pc_for(self.pc, minutes=m), self._refresh_mute_dot())))
                 for m in (5, 10, 15, 30)]
        items.append(('Заглушить на весь сеанс', lambda: (mute_pc_for(self.pc, session=True), self._refresh_mute_dot())))
        items.append(None)
        items.append(('Включить звук обратно', lambda: (unmute_pc(self.pc), self._refresh_mute_dot())))
        show_menu(self.win, event.x_root, event.y_root, items)

    def rename(self):
        from_val = self.title_lbl.cget('text').split(' — ')[0]
        new_name = ask_string_dialog(self.win, 'Переименовать', 'Ник для ПК %s:' % self.pc, initial=from_val)
        if new_name:
            set_nickname(self.pc, new_name.strip(), manual=True)
            self.title_lbl.config(text=display_label(self.pc))

    def export_history(self):
        path = filedialog.asksaveasfilename(defaultextension='.txt',
                                              initialfile='chat_%s.txt' % self.pc,
                                              filetypes=[('Текст', '*.txt')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                for m in get_messages_since(self.pc, 0):
                    who = 'Админ' if m['from'] == 'admin' else display_label(self.pc)
                    ts = time.strftime('%d.%m.%Y %H:%M', time.localtime(m['ts']))
                    if m.get('type') == 'image':
                        content = '[изображение]'
                    elif m.get('type') == 'file':
                        content = '[файл: %s]' % m.get('filename', '')
                    else:
                        content = m['text']
                    f.write('[%s] %s: %s\n' % (ts, who, content))
        except Exception:
            pass

    def _poll(self):
        if self._closed:
            return
        for m in get_messages_since(self.pc, self._last_id):
            self._render(m)
        if self._focused:
            mark_read_admin(self.pc, self._last_id)
            ping_chat_active(self.pc)
        self._refresh_ticks()
        self.title_lbl.config(text=display_label(self.pc))
        self._poll_id = self.win.after(700, self._poll)

    def _render(self, m):
        self._last_id = max(self._last_id, m['id'])
        mine = m['from'] == 'admin'
        row = tk.Frame(self.msg_frame, bg=BG)
        row.pack(fill='x', padx=12, pady=4)

        mtype = m.get('type')
        if mtype == 'image':
            try:
                raw = base64.b64decode(m['text'])
                full_img = Image.open(io.BytesIO(raw))
                thumb = full_img.copy()
                thumb.thumbnail((230, 230))
                photo = ImageTk.PhotoImage(thumb)
                _image_cache.append(photo)
                wrap = tk.Frame(row, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG)
                wrap.pack(side='right' if mine else 'left')
                lbl = tk.Label(wrap, image=photo, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG, cursor='hand2', bd=0)
                lbl.pack(padx=4, pady=4)
                lbl.bind('<Button-1>', lambda e, im=full_img: self._open_full(im))
                self._bind_menu(lbl, pil_img=full_img)
            except Exception:
                tk.Label(row, text='[изображение не загрузилось]', bg=BG, fg=SYSTEM_TEXT,
                          font=('Segoe UI', 8, 'italic')).pack(side='left')
        elif mtype == 'file':
            try:
                raw = base64.b64decode(m['text'])
                filename = m.get('filename', 'файл')
                self._render_file_card(row, filename, raw, mine)
            except Exception:
                tk.Label(row, text='[файл не загрузился]', bg=BG, fg=SYSTEM_TEXT,
                          font=('Segoe UI', 8, 'italic')).pack(side='left')
        else:
            bubble = make_bubble(row, m['text'], BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG,
                                   '#fff' if mine else TEXT_LIGHT)
            bubble.pack(side='right' if mine else 'left')
            self._bind_menu(bubble, text=m['text'])

        meta = tk.Label(row, text=time.strftime('%H:%M', time.localtime(m['ts'])) + (' ✓' if mine else ''),
                          bg=BG, fg=MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w', pady=(2, 0))
        if mine:
            self._own_bubbles.append((m['id'], meta))

        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def _render_file_card(self, row, filename, raw_bytes, mine):
        card_bg = BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG
        card_fg = '#fff' if mine else TEXT_LIGHT
        photo = rounded_rect_photo(220, 46, RADIUS, card_bg)
        c = tk.Canvas(row, width=220, height=46, bg=row['bg'], highlightthickness=0, bd=0, cursor='hand2')
        c.create_image(0, 0, image=photo, anchor='nw')
        c.create_text(14, 23, text='📄', font=('Segoe UI', 15), anchor='w')
        short = filename if len(filename) <= 26 else filename[:23] + '…'
        c.create_text(40, 23, text=short, fill=card_fg, font=('Segoe UI', 9), anchor='w')
        c.pack(side='right' if mine else 'left')

        def save_as():
            path = filedialog.asksaveasfilename(initialfile=filename)
            if path:
                try:
                    with open(path, 'wb') as f:
                        f.write(raw_bytes)
                except Exception:
                    pass

        c.bind('<Button-1>', lambda e: save_as())
        c.bind('<Button-3>', lambda e: show_menu(self.win, e.x_root, e.y_root, [('Сохранить как…', save_as)]))

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

    def send_file(self, path):
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except Exception:
            return
        if len(raw) > MAX_ATTACHMENT_B64:
            return
        filename = os.path.basename(path)
        b64 = base64.b64encode(raw).decode('ascii')
        m = post_message(self.pc, 'admin', 'file', b64, filename=filename)
        self._render(m)

    def attach_file(self):
        path = filedialog.askopenfilename(title='Выбери файл')
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
            try:
                self.send_image_pil(Image.open(path))
                return
            except Exception:
                pass
        self.send_file(path)

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
                    self.send_file(path)
                    return 'break'
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
        if self._focused:
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


def ask_string_dialog(parent, title, prompt, initial=''):
    result = {'value': None}
    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes('-topmost', True)
    win.configure(bg=PANEL)
    w, h = 320, 150
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))

    tk.Frame(win, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(win, text=title, bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 11, 'bold')).pack(pady=(14, 2))
    tk.Label(win, text=prompt, bg=PANEL, fg=MUTED, font=('Segoe UI', 9)).pack()

    entry_wrap = tk.Frame(win, bg=ENTRY_BG, highlightthickness=1, highlightbackground=PANEL_BORDER)
    entry_wrap.pack(padx=20, pady=12, fill='x')
    entry = tk.Entry(entry_wrap, bg=ENTRY_BG, fg=TEXT_LIGHT, insertbackground=TEXT_LIGHT, relief='flat', bd=0,
                      font=('Segoe UI', 10))
    entry.insert(0, initial)
    entry.pack(fill='x', ipady=6, padx=8)
    entry.focus_set()
    entry.select_range(0, 'end')

    btns = tk.Frame(win, bg=PANEL)
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
        w, h = 820, 580
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
        self.win.attributes('-topmost', False)
        self._drag = {'x': 0, 'y': 0}

        titlebar = tk.Frame(self.win, bg=PANEL, height=44)
        titlebar.pack(side='top', fill='x')
        titlebar.pack_propagate(False)
        tk.Label(titlebar, text='Godji Messenger — Администратор', bg=PANEL, fg=TEXT_LIGHT,
                  font=('Segoe UI', 10, 'bold')).pack(side='left', padx=16)
        close_btn = tk.Label(titlebar, text='—', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 13), padx=14)
        close_btn.pack(side='right')
        close_btn.bind('<Button-1>', lambda e: self.win.withdraw())
        titlebar.bind('<Button-1>', self._drag_start)
        titlebar.bind('<B1-Motion>', self._drag_move)

        body = tk.Frame(self.win, bg=BG)
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
        self._add_nav('⚙', 'Настройки', self.show_settings, parent=bottom, pack_side='bottom')

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(side='left', fill='both', expand=True)

        self.show_recent()
        self.win.after(150, apply_rounded_corners, self.win)
        self.win.after(250, make_appwindow, self.win)

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

        def on_enter(e):
            row.config(bg='#2c1a1d'); ic.config(bg='#2c1a1d'); lbl.config(bg='#2c1a1d')
        def on_leave(e):
            row.config(bg=PANEL2); ic.config(bg=PANEL2); lbl.config(bg=PANEL2)

        for w_ in (row, ic, lbl):
            w_.bind('<Button-1>', lambda e: cmd())
            w_.bind('<Enter>', on_enter)
            w_.bind('<Leave>', on_leave)
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

        scroll_outer, inner, _ = make_scroll_area(self.content)
        scroll_outer.pack(fill='both', expand=True, padx=12, pady=4)

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
            if m.get('type') == 'image':
                preview = '📷 Изображение'
            elif m.get('type') == 'file':
                preview = '📄 ' + m.get('filename', 'файл')
            else:
                preview = m['text']
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

        scroll_outer, inner, _ = make_scroll_area(self.content)
        scroll_outer.pack(fill='both', expand=True, padx=12, pady=4)

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

        scroll_outer, wrap, _ = make_scroll_area(self.content)
        scroll_outer.pack(fill='both', expand=True, padx=10, pady=4)
        wrap.configure(padx=12)

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
        tk.Label(wrap, text='Если выбран ERP, но вкладка браузера закрыта — уведомления временно\nсами переключаются сюда.',
                  bg=BG, fg=MUTED, font=('Segoe UI', 8)).pack(anchor='w', padx=14, pady=(2, 0))

        tk.Frame(wrap, bg=PANEL_BORDER, height=1).pack(fill='x', pady=16)

        tk.Label(wrap, text='На клиентской стороне:', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 10, 'bold')).pack(anchor='w')

        var_online = tk.BooleanVar(value=_show_online_indicator[0])
        def on_online_change():
            _show_online_indicator[0] = var_online.get()
            cfg = load_config(); cfg['show_online_indicator'] = var_online.get(); save_config(cfg)
        tk.Checkbutton(wrap, text='Индикатор онлайна (виден клиенту в шапке чата)', variable=var_online,
                        command=on_online_change, bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG,
                        activeforeground=TEXT_LIGHT, font=('Segoe UI', 9)).pack(anchor='w', padx=14, pady=(8, 2))

        var_sound = tk.BooleanVar(value=_notify_sound[0])
        def on_sound_change():
            _notify_sound[0] = var_sound.get()
            cfg = load_config(); cfg['notify_sound'] = var_sound.get(); save_config(cfg)
        tk.Checkbutton(wrap, text='Звук уведомлений (общий выключатель)', variable=var_sound, command=on_sound_change,
                        bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT_LIGHT,
                        font=('Segoe UI', 9)).pack(anchor='w', padx=14, pady=2)

        var_auto = tk.BooleanVar(value=(load_config().get('autostart', True)))
        def on_auto_change():
            if var_auto.get():
                install_autostart()
            else:
                remove_autostart()
            cfg = load_config(); cfg['autostart'] = var_auto.get(); save_config(cfg)
        tk.Checkbutton(wrap, text='Автозапуск с Windows', variable=var_auto, command=on_auto_change,
                        bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT_LIGHT,
                        font=('Segoe UI', 9)).pack(anchor='w', padx=14, pady=2)

        tk.Label(wrap, text='Звук по конкретному ПК — правая кнопка мыши внутри открытого\nчата с этим ПК.',
                  bg=BG, fg=MUTED, font=('Segoe UI', 8), justify='left').pack(anchor='w', padx=14, pady=(14, 0))

        tk.Frame(wrap, bg=PANEL_BORDER, height=1).pack(fill='x', pady=16)

        tk.Label(wrap, text='Заглушить все чаты сразу:', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        mute_row = tk.Frame(wrap, bg=BG)
        mute_row.pack(anchor='w', padx=14, pady=8)
        for mins in (15, 30, 60):
            rounded_pill(mute_row, '%d мин' % mins, lambda m=mins: mute_all(minutes=m)).pack(side='left', padx=4)
        rounded_pill(mute_row, 'на сеанс', lambda: mute_all(session=True)).pack(side='left', padx=4)

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
    setup_ttk_style()
    wiz.overrideredirect(True)
    wiz.configure(bg=BG)
    w, h = 400, 200
    sw, sh = wiz.winfo_screenwidth(), wiz.winfo_screenheight()
    wiz.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
    wiz.attributes('-topmost', True)
    wiz.after(150, apply_rounded_corners, wiz)

    tk.Frame(wiz, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(wiz, text='Godji Messenger', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 14, 'bold')).pack(pady=(22, 2))
    tk.Label(wiz, text='Первый запуск — настройка сервера', bg=BG, fg=MUTED, font=('Segoe UI', 9)).pack()
    status = tk.Label(wiz, text='Подготовка…', bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 9))
    status.pack(pady=14)
    warn = tk.Label(wiz, text='', bg=BG, fg='#e0a800', font=('Segoe UI', 8), wraplength=360, justify='center')
    warn.pack()

    def step_firewall():
        add_firewall_rule()
        time.sleep(2.5)
        if not verify_firewall_rule():
            warn.config(text='Не удалось подтвердить правило брандмауэра — если клиенты не будут\n'
                              'находить сервер, разреши вручную входящий TCP порт %d.' % HTTP_PORT)

    steps = [
        ('Регистрируем автозапуск…', install_autostart),
        ('Настраиваем брандмауэр (может появиться запрос Windows)…', step_firewall),
    ]
    idx = [0]

    def next_step():
        if idx[0] >= len(steps):
            status.config(text='Готово! Сервер запускается…')
            wiz.update()
            cfg = load_config()
            cfg['configured'] = True
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
        wiz.after(1300, next_step)

    wiz.after(500, next_step)
    wiz.mainloop()


def start_services():
    cfg = load_config()
    _notify_target[0] = cfg.get('notify_target', 'erp')
    _show_online_indicator[0] = cfg.get('show_online_indicator', True)
    _notify_sound[0] = cfg.get('notify_sound', True)

    root = tk.Tk()
    setup_ttk_style()
    main_win = MainWindow(root)

    def poll_show_event():
        if _show_window_event.is_set():
            _show_window_event.clear()
            main_win.show()
        root.after(250, poll_show_event)
    root.after(250, poll_show_event)

    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=beacon_loop, daemon=True).start()
    threading.Thread(target=events_poll_loop, args=(root,), daemon=True).start()
    threading.Thread(target=run_tray, args=(root, main_win), daemon=True).start()

    root.mainloop()


def main():
    enable_dpi_awareness()

    mutex, already_running = try_acquire_singleton()
    if already_running:
        signal_existing_instance()
        return

    cfg = load_config()
    if cfg.get('configured'):
        start_services()
    else:
        run_wizard(start_services)


if __name__ == '__main__':
    main()
