#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godji Messenger — АДМИНСКОЕ приложение.

При первом запуске: сама регистрирует автозапуск, добавляет правило
брандмауэра (один раз спросит подтверждение Windows/UAC) и запускается
в трее. В остальных случаях — сразу обычный запуск в трее.

Сервер рассылает по локальной сети UDP-маячок, по которому клиентские
приложения САМИ находят IP этого ПК — вручную ничего указывать не нужно.

В трее (правая кнопка на значке) есть пункт "Настройки" — там можно
выбрать, куда приходят уведомления о новых сообщениях от клиентов:
в ERP (в браузере) или прямо в эту программу (тогда у неё появляется
собственное окошко переписки на каждый ПК).

Сборка в exe: см. build_exe.bat или .github/workflows/build-exe.yml.
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
HISTORY_TTL = 24 * 3600
MAX_IMAGE_B64 = 3 * 1024 * 1024  # ~3МБ на картинку в base64

CONFIG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'GodjiMessenger')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'admin_config.json')

DARK_BG = '#0f1020'
PANEL = '#161729'
ACCENT = '#cc0001'
TEXT_LIGHT = '#eef0f5'
MUTED = '#8b8fa3'
BUBBLE_ADMIN_BG = '#1f2137'
BUBBLE_ME_BG = '#cc0001'
TEXT_READ = '#4fc3f7'
SYSTEM_TEXT = '#e0a800'
ENTRY_BG = '#1c1d33'

_notify_target = ['erp']   # 'erp' или 'admin_app' — куда показывать уведомления
_open_chats = {}           # {pc: AdminChatWindow}


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
    """Путь к самому себе — работает только для собранного exe (PyInstaller)."""
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
    """Добавляет правило через отдельный elevated-процесс netsh —
    поднимать права всему приложению не нужно, UAC спросится один раз."""
    exe = get_self_path() or sys.executable
    args = ('advfirewall firewall add rule name="Godji Messenger Admin" '
            'dir=in action=allow protocol=TCP localport=%d program="%s" '
            'enable=yes profile=any') % (HTTP_PORT, exe)
    try:
        ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'netsh', args, None, 0)
        print('[messenger] Запрос на правило брандмауэра отправлен')
    except Exception as e:
        print('[messenger] Ошибка брандмауэра:', e)


# ───────────────────────── общее хранилище сообщений ─────────────────────────
lock = threading.Lock()
last_seen = {}     # {pc: ts}
messages = []      # [{id, pc, from, type, text, ts}]
_next_id = 1
read_client = {}   # {pc: id} — до какого id клиент прочитал (для галочек у клиента)
read_admin = {}    # {pc: id} — до какого id админ прочитал (для галочек в этой программе/ERP)


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


def mark_read_admin(pc, upto):
    if not upto:
        return
    with lock:
        read_admin[pc] = max(read_admin.get(pc, 0), upto)


def get_read_client(pc):
    with lock:
        return read_client.get(pc, 0)


def online_count():
    with lock:
        t = now()
        return sum(1 for ts in last_seen.values() if (t - ts) <= ONLINE_TIMEOUT)


# ───────────────────────── HTTP сервер (для ERP-скрипта и клиентов) ─────────────────────────
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
            return self._json(200, {'notifyTarget': _notify_target[0]})

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

        self._json(404, {'error': 'not found'})


def run_http_server():
    srv = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), Handler)
    print('[messenger] HTTP сервер запущен на порту %d' % HTTP_PORT)
    srv.serve_forever()


def beacon_loop():
    """UDP-маячок — чтобы клиенты сами находили IP этого ПК в сети."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = json.dumps({'service': 'godji_messenger', 'port': HTTP_PORT}).encode('utf-8')
    while True:
        try:
            s.sendto(payload, ('255.255.255.255', BEACON_PORT))
        except Exception:
            pass
        time.sleep(BEACON_INTERVAL)


# ───────────────────────── уведомление о новом сообщении (для режима "в этой программе") ─────────────────────────
def show_admin_toast(root, pc, text):
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes('-topmost', True)
    win.configure(bg=PANEL)
    w, h = 300, 92
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry('%dx%d+%d+%d' % (w, h, sw - w - 20, sh - h - 60))

    tk.Frame(win, bg=ACCENT, width=4).pack(side='left', fill='y')
    body = tk.Frame(win, bg=PANEL)
    body.pack(side='left', fill='both', expand=True, padx=10, pady=8)
    tk.Label(body, text='ПК ' + pc, bg=PANEL, fg=ACCENT, font=('Segoe UI', 9, 'bold')).pack(anchor='w')
    tk.Label(body, text=text, bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 9),
              wraplength=250, justify='left').pack(anchor='w', pady=(2, 0))

    def on_click(event=None):
        try:
            win.destroy()
        except Exception:
            pass
        open_chat_for(root, pc)

    win.bind('<Button-1>', on_click)
    for child in body.winfo_children():
        child.bind('<Button-1>', on_click)

    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass

    win.after(7000, lambda: win.destroy() if win.winfo_exists() else None)


def events_poll_loop(root):
    """Следит за новыми сообщениями от клиентов и, если выбран режим
    "уведомления в этой программе", показывает тост / обновляет открытый чат."""
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
                continue  # ERP сама следит через свой /events поллинг
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
        self._own_bubbles = []   # [(id, meta_label)]
        self._image_refs = []
        self._closed = False

        self.win = tk.Toplevel(root)
        self.win.title('ПК %s — Godji Messenger' % pc)
        self.win.configure(bg=DARK_BG)
        self.win.geometry('340x480')
        self.win.protocol('WM_DELETE_WINDOW', self.close)

        header = tk.Frame(self.win, bg=PANEL, height=42)
        header.pack(side='top', fill='x')
        header.pack_propagate(False)
        tk.Label(header, text='●', bg=PANEL, fg=ACCENT, font=('Segoe UI', 11)).pack(side='left', padx=(12, 4))
        tk.Label(header, text='ПК ' + pc, bg=PANEL, fg=TEXT_LIGHT, font=('Segoe UI', 11, 'bold')).pack(side='left')

        outer = tk.Frame(self.win, bg=DARK_BG)
        outer.pack(side='top', fill='both', expand=True)
        self.canvas = tk.Canvas(outer, bg=DARK_BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient='vertical', command=self.canvas.yview)
        self.msg_frame = tk.Frame(self.canvas, bg=DARK_BG)
        self.msg_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self._win_id = self.canvas.create_window((0, 0), window=self.msg_frame, anchor='nw')
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfig(self._win_id, width=e.width))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.msg_frame)

        footer = tk.Frame(self.win, bg=PANEL)
        footer.pack(side='bottom', fill='x')
        inner = tk.Frame(footer, bg=PANEL)
        inner.pack(fill='x', padx=8, pady=8)

        attach = tk.Label(inner, text='📎', bg=PANEL, fg=MUTED, cursor='hand2', font=('Segoe UI', 12))
        attach.pack(side='left', padx=(0, 6))
        attach.bind('<Button-1>', lambda e: self.attach_image())

        self.entry = tk.Entry(inner, bg=ENTRY_BG, fg=TEXT_LIGHT, insertbackground=TEXT_LIGHT,
                               relief='flat', bd=0, highlightthickness=1,
                               highlightbackground='#2a2c42', highlightcolor=ACCENT)
        self.entry.pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 6))
        self.entry.bind('<Return>', self.send_text)
        self.entry.bind('<Control-v>', self._on_paste)

        send_btn = tk.Label(inner, text='➤', bg=ACCENT, fg='#fff', cursor='hand2', width=3,
                              font=('Segoe UI', 11, 'bold'))
        send_btn.pack(side='left')
        send_btn.bind('<Button-1>', self.send_text)

        for m in get_messages_since(pc, 0):
            self._render(m)
        mark_read_admin(pc, self._last_id)

        self._poll_id = self.win.after(1000, self._poll)
        self.entry.focus_set()

    def _bind_wheel(self, widget):
        widget.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

    def _poll(self):
        if self._closed:
            return
        for m in get_messages_since(self.pc, self._last_id):
            self._render(m)
        mark_read_admin(self.pc, self._last_id)
        self._refresh_ticks()
        self._poll_id = self.win.after(1000, self._poll)

    def _render(self, m):
        self._last_id = max(self._last_id, m['id'])
        mine = m['from'] == 'admin'
        row = tk.Frame(self.msg_frame, bg=DARK_BG)
        row.pack(fill='x', padx=10, pady=3)

        if m.get('type') == 'image':
            try:
                raw = base64.b64decode(m['text'])
                full_img = Image.open(io.BytesIO(raw))
                thumb = full_img.copy()
                thumb.thumbnail((220, 220))
                photo = ImageTk.PhotoImage(thumb)
                self._image_refs.append(photo)
                lbl = tk.Label(row, image=photo, bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG, cursor='hand2', bd=0)
                lbl.pack(side='right' if mine else 'left')
                lbl.bind('<Button-1>', lambda e, im=full_img: self._open_full(im))
                self._bind_wheel(lbl)
            except Exception:
                tk.Label(row, text='[изображение не загрузилось]', bg=DARK_BG, fg=SYSTEM_TEXT,
                          font=('Segoe UI', 8, 'italic')).pack(side='left')
        else:
            bubble = tk.Label(row, text=m['text'], bg=BUBBLE_ME_BG if mine else BUBBLE_ADMIN_BG,
                                fg='#fff' if mine else TEXT_LIGHT, font=('Segoe UI', 10),
                                wraplength=210, justify='left', padx=10, pady=7, bd=0)
            bubble.pack(side='right' if mine else 'left')
            self._bind_wheel(bubble)

        meta = tk.Label(row, text=time.strftime('%H:%M', time.localtime(m['ts'])) + (' ✓' if mine else ''),
                          bg=DARK_BG, fg=MUTED, font=('Segoe UI', 7))
        meta.pack(side='right' if mine else 'left', anchor='e' if mine else 'w')
        if mine:
            self._own_bubbles.append((m['id'], meta))

        self._bind_wheel(row)
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def _refresh_ticks(self):
        rc = get_read_client(self.pc)
        ts_now = None
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
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            clip = None
        if isinstance(clip, Image.Image):
            self.send_image_pil(clip)
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


# ───────────────────────── окно настроек ─────────────────────────
def open_settings(root):
    win = tk.Toplevel(root)
    win.title('Настройки — Godji Messenger')
    win.configure(bg=DARK_BG)
    win.geometry('340x230')
    win.attributes('-topmost', True)

    tk.Label(win, text='Куда присылать уведомления\nо новых сообщениях от клиентов:',
              bg=DARK_BG, fg=TEXT_LIGHT, font=('Segoe UI', 10), justify='left').pack(pady=(20, 12), padx=18, anchor='w')

    var = tk.StringVar(value=_notify_target[0])
    tk.Radiobutton(win, text='В ERP (в браузере)', variable=var, value='erp',
                    bg=DARK_BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=DARK_BG,
                    activeforeground=TEXT_LIGHT, font=('Segoe UI', 10)).pack(anchor='w', padx=26, pady=3)
    tk.Radiobutton(win, text='В этой программе', variable=var, value='admin_app',
                    bg=DARK_BG, fg=TEXT_LIGHT, selectcolor=PANEL, activebackground=DARK_BG,
                    activeforeground=TEXT_LIGHT, font=('Segoe UI', 10)).pack(anchor='w', padx=26, pady=3)

    def save():
        _notify_target[0] = var.get()
        cfg = load_config()
        cfg['notify_target'] = var.get()
        save_config(cfg)
        win.destroy()

    tk.Button(win, text='Сохранить', command=save, bg=ACCENT, fg='#fff', relief='flat',
               font=('Segoe UI', 10, 'bold'), padx=16, pady=6, cursor='hand2').pack(pady=18)


# ───────────────────────── трей ─────────────────────────
def make_icon_image():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=16, fill='#cc0001')
    d.ellipse((16, 16, 48, 48), fill='#ffffff')
    d.text((25, 18), 'G', fill='#cc0001')
    return img


def run_tray(root):
    if pystray is None:
        print('[messenger] pystray не установлен — работаю без трея, просто как фоновый процесс.')
        while True:
            time.sleep(3600)

    def label(item):
        return 'Онлайн ПК: %d' % online_count()

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
    wiz.configure(bg=DARK_BG)
    w, h = 380, 190
    sw, sh = wiz.winfo_screenwidth(), wiz.winfo_screenheight()
    wiz.geometry('%dx%d+%d+%d' % (w, h, (sw - w) // 2, (sh - h) // 2))
    wiz.attributes('-topmost', True)

    tk.Frame(wiz, bg=ACCENT, height=3).pack(side='top', fill='x')
    tk.Label(wiz, text='Godji Messenger', bg=DARK_BG, fg=TEXT_LIGHT,
              font=('Segoe UI', 14, 'bold')).pack(pady=(22, 2))
    tk.Label(wiz, text='Первый запуск — настройка сервера', bg=DARK_BG, fg=MUTED,
              font=('Segoe UI', 9)).pack()
    status = tk.Label(wiz, text='Подготовка…', bg=DARK_BG, fg=TEXT_LIGHT, font=('Segoe UI', 9))
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

    root = tk.Tk()
    root.withdraw()  # скрытое корневое окно — держит Tk живым для тостов/чатов/настроек

    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=beacon_loop, daemon=True).start()
    threading.Thread(target=events_poll_loop, args=(root,), daemon=True).start()
    threading.Thread(target=run_tray, args=(root,), daemon=True).start()

    root.mainloop()


def main():
    cfg = load_config()
    if cfg.get('configured'):
        start_services()
    else:
        run_wizard(start_services)


if __name__ == '__main__':
    main()
