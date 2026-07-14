#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godji Messenger — АДМИНСКОЕ приложение.

При первом запуске: сама регистрирует автозапуск, добавляет правило
брандмауэра (один раз спросит подтверждение Windows/UAC) и запускается
в трее. В остальных случаях — сразу обычный запуск в трее.

Сервер рассылает по локальной сети UDP-маячок, по которому клиентские
приложения САМИ находят IP этого ПК — вручную ничего указывать не нужно.

В трее (правая кнопка на значке):
  Настройки        — куда слать уведомления (ERP / эта программа),
                      показывать ли клиентам индикатор "админ на связи",
                      звук уведомлений у клиентов.
  Недавние чаты     — список ПК с перепиской, сортировка по активности,
                      очистка истории по каждому чату.

Сборка в exe: см. build_exe.bat в комплекте.
Зависимости (только на этапе сборки, конечному exe не нужны):
    pip install requests pillow pystray
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
from tkinter import filedialog

from PIL import Image, ImageDraw, ImageGrab, ImageTk

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
HISTORY_TTL = 24 * 3600
MAX_IMAGE_B64 = 3 * 1024 * 1024  # ~3МБ на картинку в base64

CONFIG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'GodjiMessenger')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'admin_config.json')

BG = '#0e0f1c'
PANEL = '#171830'
PANEL_BORDER = '#282a4a'
ACCENT = '#d4172a'
ACCENT_HOVER = '#b01120'
TEXT_LIGHT = '#f2f3f8'
MUTED = '#9195b0'
BUBBLE_ADMIN_BG = '#23253f'
BUBBLE_ME_BG = '#d4172a'
TEXT_READ = '#57c2ff'
SYSTEM_TEXT = '#e0a800'
ENTRY_BG = '#1f2140'

_notify_target = ['erp']            # 'erp' или 'admin_app'
_show_online_indicator = [True]     # показывать ли клиентам зелёную/красную точку
_notify_sound = [True]              # звук уведомлений на клиентской стороне
_open_chats = {}                    # {pc: AdminChatWindow}


# ───────────────────────── DPI (иначе всё размыто/пиксельно на Windows) ─────────────────────────
def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


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


# ───────────────────────── автозапуск ─────────────────────────
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


# ───────────────────────── брандмауэр ─────────────────────────
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
messages = []        # [{id, pc, from, type, text, ts}]
_next_id = 1
read_client = {}     # {pc: id}
read_admin = {}      # {pc: id}
chat_active = {}      # {pc: ts последнего пинга "чат открыт"}


def now():
    return time.time()


def post_message(pc, frm, mtype, text):
    global _next_id
    with lock:
        msg = {'id': _next_id, 'pc': pc, 'from': frm, 'type': mtype, 'text': text, 'ts': now()}
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
    """Список ПК с перепиской, последнее сообщение сверху."""
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
            return self._json(200, get_messages_since(pc, since))

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

        if u.path == '/chat_active':
            pc = (qs.get('pc') or [''])[0]
            return self._json(200, {'active': is_chat_active(pc)})

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

        if u.path == '/send':
            pc = str(data.get('pc', '')).strip()
            frm = data.get('from', 'client')
            mtype = data.get('type', 'text')
            text = data.get('text', '')
            if mtype == 'image':
                if not pc or not text:
                    return self._json(400, {'error': 'pc and image data required'})
                if len(text) > MAX_IMAGE_B64:
                    return self._json(400, {'error': 'image too large'})
            else:
                text = str(text).strip()
                if not pc or not text:
                    return self._json(400, {'error': 'pc and text required'})
            msg = post_message(pc, frm, mtype, text)
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


# ───────────────────────── скруглённые "пузыри" (как в клиенте) ─────────────────────────
def _rounded_points(x1, y1, x2, y2, r):
    return [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2, x2-r,y2, x1+r,y2,
            x1,y2, x1,y2-r, x1,y1+r, x1,y1]


def make_bubble(parent, text, bg, fg, wrap_px=220, font=('Segoe UI', 10), pad_x=12, pad_y=9, radius=14):
    fnt = tkfont.Font(family=font[0], size=font[1])
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


# ───────────────────────── уведомление о новом сообщении ─────────────────────────
def show_admin_toast(root, pc, text):
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes('-topmost', True)
    win.configure(bg=PANEL)
    w, h = 320, 100
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry('%dx%d+%d+%d' % (w, h, sw - w - 22, sh - h - 64))

    card = tk.Frame(win, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER)
    card.pack(fill='both', expand=True)
    tk.Frame(card, bg=ACCENT, width=4).pack(side='left', fill='y')
    body = tk.Frame(card, bg=PANEL)
    body.pack(side='left', fill='both', expand=True, padx=12, pady=10)
    tk.Label(body, text='ПК ' + pc, bg=PANEL, fg=ACCENT, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
    tk.Label(body, text=text, bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 9.5),
              wraplength=260, justify='left').pack(anchor='w', pady=(3, 0))

    def on_click(event=None):
        try:
            win.destroy()
        except Exception:
            pass
        open_chat_for(root, pc)

    win.bind('<Button-1>', on_click)
    for child in body.winfo_children():
        child.bind('<Button-1>', on_click)

    if _notify_sound[0]:
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    win.after(7000, lambda: win.destroy() if win.winfo_exists() else None)


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
            if _notify_target[0] != 'admin_app':
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

        self.win = tk.Toplevel(root)
        self.win.title('ПК %s — Godji Messenger' % pc)
        self.win.configure(bg=BG)
        self.win.geometry('360x520')
        self.win.protocol('WM_DELETE_WINDOW', self.close)

        header = tk.Frame(self.win, bg=PANEL, height=46)
        header.pack(side='top', fill='x')
        header.pack_propagate(False)
        tk.Label(header, text='●', bg=PANEL, fg=ACCENT, font=('Segoe UI', 11)).pack(side='left', padx=(14, 6))
        tk.Label(header, text='ПК ' + pc, bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 11, 'bold')).pack(side='left')
        trash = tk.Label(header, text='🗑', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 11))
        trash.pack(side='right', padx=14)
        trash.bind('<Button-1>', lambda e: self.clear_history())
        trash.bind('<Enter>', lambda e: trash.config(fg=TEXT_LIGHT))
        trash.bind('<Leave>', lambda e: trash.config(fg=MUTED))

        outer = tk.Frame(self.win, bg=BG)
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

        footer = tk.Frame(self.win, bg=PANEL)
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
                               relief='flat', bd=0, font=('Segoe UI', 10.5))
        self.entry.pack(fill='x', ipady=7, padx=8)
        self.entry.bind('<Return>', self.send_text)
        self.entry.bind('<Control-v>', self._on_paste)

        send_btn = tk.Label(inner, text='➤', bg=ACCENT, fg='#fff', cursor='hand2', width=3,
                              font=('Segoe UI', 11, 'bold'))
        send_btn.pack(side='left')
        send_btn.bind('<Button-1>', self.send_text)
        send_btn.bind('<Enter>', lambda e: send_btn.config(bg=ACCENT_HOVER))
        send_btn.bind('<Leave>', lambda e: send_btn.config(bg=ACCENT))

        for m in get_messages_since(pc, 0):
            self._render(m)
        mark_read_admin(pc, self._last_id)
        ping_chat_active(pc)

        self._poll_id = self.win.after(1000, self._poll)
        self.entry.focus_set()

    def _bind_wheel(self, widget):
        widget.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

    def open_emoji_panel(self):
        try:
            self.entry.focus_force()
            VK_LWIN, VK_OEM_PERIOD, KEYEVENTF_KEYUP = 0x5B, 0xBE, 0x0002
            def fire():
                user32 = ctypes.windll.user32
                user32.keybd_event(VK_LWIN, 0, 0, 0)
                user32.keybd_event(VK_OEM_PERIOD, 0, 0, 0)
                user32.keybd_event(VK_OEM_PERIOD, 0, KEYEVENTF_KEYUP, 0)
                user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
            self.win.after(60, fire)
        except Exception:
            pass

    def _poll(self):
        if self._closed:
            return
        for m in get_messages_since(self.pc, self._last_id):
            self._render(m)
        mark_read_admin(self.pc, self._last_id)
        ping_chat_active(self.pc)
        self._refresh_ticks()
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
                self._bind_wheel(wrap); self._bind_wheel(lbl)
            except Exception:
                tk.Label(row, text='[изображение не загрузилось]', bg=BG, fg=SYSTEM_TEXT,
                          font=('Segoe UI', 8, 'italic')).pack(side='left')
        else:
            bubble = make_bubble(row, m['text'], BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG,
                                   '#fff' if mine else TEXT_LIGHT)
            bubble.pack(side='right' if mine else 'left')
            self._bind_wheel(bubble)

        meta = tk.Label(row, text=time.strftime('%H:%M', time.localtime(m['ts'])) + (' ✓' if mine else ''),
                          bg=BG, fg=MUTED, font=('Segoe UI', 7.5))
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

    def send_text(self, event=None):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, 'end')
        m = post_message(self.pc, 'admin', 'text', text)
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


# ───────────────────────── окно "Недавние чаты" ─────────────────────────
def open_recent_chats(root):
    win = tk.Toplevel(root)
    win.title('Недавние чаты — Godji Messenger')
    win.configure(bg=BG)
    win.geometry('360x460')
    win.attributes('-topmost', True)

    header = tk.Frame(win, bg=PANEL, height=44)
    header.pack(side='top', fill='x')
    header.pack_propagate(False)
    tk.Label(header, text='Недавние чаты', bg=PANEL, fg=TEXT_LIGHT,
              font=('Segoe UI', 11, 'bold')).pack(side='left', padx=14)

    outer = tk.Frame(win, bg=BG)
    outer.pack(fill='both', expand=True)
    canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
    vsb = tk.Scrollbar(outer, orient='vertical', command=canvas.yview)
    list_frame = tk.Frame(canvas, bg=BG)
    list_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    win_id = canvas.create_window((0, 0), window=list_frame, anchor='nw')
    canvas.bind('<Configure>', lambda e: canvas.itemconfig(win_id, width=e.width))
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side='left', fill='both', expand=True)
    vsb.pack(side='right', fill='y')
    canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

    def refresh():
        for w in list_frame.winfo_children():
            w.destroy()
        recent = get_recent_chats()
        if not recent:
            tk.Label(list_frame, text='Пока нет переписки', bg=BG, fg=MUTED,
                      font=('Segoe UI', 9, 'italic')).pack(pady=20)
            return
        for m in recent:
            pc = m['pc']
            row = tk.Frame(list_frame, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER)
            row.pack(fill='x', padx=10, pady=5)
            inner = tk.Frame(row, bg=PANEL, cursor='hand2')
            inner.pack(fill='x', padx=12, pady=10)
            top_line = tk.Frame(inner, bg=PANEL)
            top_line.pack(fill='x')
            tk.Label(top_line, text='ПК ' + pc, bg=PANEL, fg=TEXT_LIGHT,
                      font=('Segoe UI', 10, 'bold')).pack(side='left')
            tk.Label(top_line, text=time.strftime('%d.%m %H:%M', time.localtime(m['ts'])),
                      bg=PANEL, fg=MUTED, font=('Segoe UI', 8)).pack(side='right')
            preview = '📷 Изображение' if m.get('type') == 'image' else m['text']
            fromlbl = 'Вы: ' if m['from'] == 'admin' else ''
            tk.Label(inner, text=(fromlbl + preview)[:60], bg=PANEL, fg=MUTED,
                      font=('Segoe UI', 9), anchor='w', justify='left').pack(fill='x', pady=(3, 0))

            def open_it(e, pc=pc):
                win.destroy()
                open_chat_for(root, pc)

            def clear_it(e, pc=pc):
                clear_messages(pc)
                if pc in _open_chats:
                    _open_chats[pc].clear_history()
                refresh()

            for w in (row, inner, top_line):
                w.bind('<Button-1>', open_it)

            clear_btn = tk.Label(inner, text='Очистить историю', bg=PANEL, fg='#e0393f',
                                    font=('Segoe UI', 7.5, 'underline'), cursor='hand2')
            clear_btn.pack(anchor='e', pady=(4, 0))
            clear_btn.bind('<Button-1>', clear_it)

    refresh()
    win.after(3000, lambda: (refresh(), None) if win.winfo_exists() else None)


# ───────────────────────── окно настроек ─────────────────────────
def open_settings(root):
    win = tk.Toplevel(root)
    win.title('Настройки — Godji Messenger')
    win.configure(bg=BG)
    win.geometry('360x360')
    win.attributes('-topmost', True)

    tk.Label(win, text='Уведомления о новых сообщениях от клиентов:',
              bg=BG, fg=TEXT_LIGHT, font=('Segoe UI', 10, 'bold'), justify='left').pack(pady=(18, 8), padx=18, anchor='w')

    var_target = tk.StringVar(value=_notify_target[0])
    tk.Radiobutton(win, text='В ERP (в браузере)', variable=var_target, value='erp',
                    bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG,
                    activeforeground=TEXT_LIGHT, font=('Segoe UI', 10)).pack(anchor='w', padx=26, pady=2)
    tk.Radiobutton(win, text='В этой программе', variable=var_target, value='admin_app',
                    bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG,
                    activeforeground=TEXT_LIGHT, font=('Segoe UI', 10)).pack(anchor='w', padx=26, pady=2)

    tk.Frame(win, bg=PANEL_BORDER, height=1).pack(fill='x', padx=18, pady=14)

    tk.Label(win, text='На клиентской стороне:', bg=BG, fg=TEXT_LIGHT,
              font=('Segoe UI', 10, 'bold')).pack(padx=18, anchor='w')

    var_online = tk.BooleanVar(value=_show_online_indicator[0])
    tk.Checkbutton(win, text='Показывать индикатор "админ на связи"', variable=var_online,
                    bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG,
                    activeforeground=TEXT_LIGHT, font=('Segoe UI', 9.5)).pack(anchor='w', padx=26, pady=(8, 2))

    var_sound = tk.BooleanVar(value=_notify_sound[0])
    tk.Checkbutton(win, text='Звук уведомлений', variable=var_sound,
                    bg=BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=BG,
                    activeforeground=TEXT_LIGHT, font=('Segoe UI', 9.5)).pack(anchor='w', padx=26, pady=2)

    def save():
        _notify_target[0] = var_target.get()
        _show_online_indicator[0] = var_online.get()
        _notify_sound[0] = var_sound.get()
        cfg = load_config()
        cfg['notify_target'] = var_target.get()
        cfg['show_online_indicator'] = var_online.get()
        cfg['notify_sound'] = var_sound.get()
        save_config(cfg)
        win.destroy()

    tk.Button(win, text='Сохранить', command=save, bg=ACCENT, fg='#fff', relief='flat',
               font=('Segoe UI', 10, 'bold'), padx=16, pady=7, cursor='hand2',
               activebackground=ACCENT_HOVER, activeforeground='#fff').pack(pady=20)


# ───────────────────────── трей ─────────────────────────
def make_icon_image():
    """Иконка трея — красный пузырь сообщения на прозрачном фоне."""
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 6, 60, 46), radius=13, fill='#d4172a')
    d.polygon([(16, 44), (16, 58), (32, 44)], fill='#d4172a')
    for cx in (20, 32, 44):
        d.ellipse((cx-4, 22, cx+4, 30), fill='#ffffff')
    return img


def run_tray(root):
    if pystray is None:
        print('[messenger] pystray не установлен — работаю без трея, просто как фоновый процесс.')
        while True:
            time.sleep(3600)

    def label(item):
        return 'Онлайн ПК: %d' % online_count()

    def on_recent(icon, item):
        root.after(0, open_recent_chats, root)

    def on_settings(icon, item):
        root.after(0, open_settings, root)

    def on_open_folder(icon, item):
        try:
            os.startfile(CONFIG_DIR)
        except Exception:
            pass

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    icon = pystray.Icon(APP_NAME, make_icon_image(), 'Godji Messenger — сервер запущен')
    icon.menu = pystray.Menu(
        pystray.MenuItem(label, None, enabled=False),
        pystray.MenuItem('Недавние чаты', on_recent),
        pystray.MenuItem('Настройки', on_settings),
        pystray.MenuItem('Открыть папку данных', on_open_folder),
        pystray.MenuItem('Выход', on_quit),
    )

    def refresh_loop():
        while True:
            time.sleep(5)
            try:
                icon.update_menu()
            except Exception:
                pass

    threading.Thread(target=refresh_loop, daemon=True).start()
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

    tk.Frame(wiz, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(wiz, text='Godji Messenger', bg=BG, fg=TEXT_LIGHT,
              font=('Segoe UI', 14, 'bold')).pack(pady=(22, 2))
    tk.Label(wiz, text='Первый запуск — настройка сервера', bg=BG, fg=MUTED,
              font=('Segoe UI', 9)).pack()
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

    root = tk.Tk()
    root.withdraw()

    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=beacon_loop, daemon=True).start()
    threading.Thread(target=events_poll_loop, args=(root,), daemon=True).start()
    threading.Thread(target=run_tray, args=(root,), daemon=True).start()

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
