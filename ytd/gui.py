"""ytd-gui - window version of ytd: paste a YouTube link, pick video or audio and a quality, download.

Same rules as the command line: already-downloaded files are skipped and partial ones resume.
"""
import ctypes
import ctypes.wintypes
import datetime
import functools
import importlib.metadata
import io
import itertools
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import winreg
import winsound
import xml.etree.ElementTree as ET
from multiprocessing.connection import Client, Listener
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pystray
from PIL import Image, ImageTk
from tkinterdnd2 import DND_TEXT, TkinterDnD
from yt_dlp.utils import DownloadCancelled, DownloadError, format_bytes
from yt_dlp.version import __version__ as ytdlp_version

from ytd import cli

SETTINGS_FILE = Path(os.environ['APPDATA'], 'ytd', 'settings.json')  # the uninstaller deletes this folder
QUEUE_FILE = SETTINGS_FILE.with_name('queue.json')  # unfinished downloads, resumed after a restart
SUBS_FILE = SETTINGS_FILE.with_name('subscriptions.json')  # channels whose new videos get queued
FEED = 'https://www.youtube.com/feeds/videos.xml?channel_id={}'  # a channel's latest 15 uploads, about 20 KB
SUBS_HOURS = 6  # subscriptions are checked on start and this often
PIPE = rf'\\.\pipe\YouTubeDownloader-{os.environ.get("USERNAME", "")}'  # later starts hand their link to the open window
# the browser button: a bookmark that opens the page's link as ytd:<link>, which Setup registers for this app
BOOKMARKLET = "javascript:location.href='ytd:'+encodeURIComponent(location.href)"
APP_DIR = Path(sys.prefix).parent.parent  # Setup's install folder; the app's environment is APP_DIR\tool\ytd
UV = next(APP_DIR.glob('uv/*/uv.exe'), None)  # kept by Setup for updates; None when run from source
GUI_EXE = APP_DIR / 'bin' / 'ytd-gui.exe'  # the app as Setup installs it
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'  # apps started with Windows; the uninstaller cleans ours
UPDATE_CHECK_DAYS = 7
RELEASES = 'https://api.github.com/repos/bassiony1/youtube-downloader/releases/latest'  # new app versions
LIMITS = {'No limit': None, **{f'{n} KB/s': n * 1024 for n in (100, 250, 500)},
          **{f'{n} MB/s': n * 1024 ** 2 for n in (1, 2, 5)}}  # speed limit choices, bytes per second
QUALITIES = (2160, 1440, 1080, 720, 480, 360, 240, 144)  # offered when no videos are checked (Subscribe only)
YOUTUBE_LINK = re.compile(r'https?://([\w-]+\.)?(youtube\.com|youtu\.be)/\S+$')
try:
    VERSION = importlib.metadata.version('ytd')
except importlib.metadata.PackageNotFoundError:
    VERSION = 'source'  # run from a checkout


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default  # first run, or a damaged file


def save_json(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix('.tmp').write_text(json.dumps(data, indent=1), encoding='utf-8')
        os.replace(path.with_suffix('.tmp'), path)  # all or nothing, even if Windows shuts down meanwhile
    except OSError:
        pass  # next start does without: default settings, or partial files resumed by downloading again


def link_from(args):
    """The link the app was started with: plain, or ytd:<encoded link> from the browser button. '' if none."""
    link = args[0].strip() if args else ''
    if link[:4].lower() == 'ytd:':
        link = urllib.parse.unquote(link[4:]).removeprefix('//')
    return link


def hand_over(link):
    """Give the link ('' just brings the window to the front) to the app that is open already.
    False if it doesn't answer."""
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: it may come to the front
    for _ in range(25):  # it may still be starting up
        try:
            with Client(PIPE, 'AF_PIPE') as conn:
                conn.send_bytes(link.encode('utf-8'))
            return True
        except OSError:  # not listening (yet)
            time.sleep(0.2)
    return False


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def queue_line(left, speed):
    """What is left of the queue, in words: its size, and the time it takes at this speed."""
    eta = f', about {datetime.timedelta(seconds=int(left / speed))}' if speed and left else ''
    return f'Queue: {format_bytes(left)} left{eta}'


def feed(xml):
    """The videos in a channel's feed (FEED), newest first: [(id, title)]."""
    atom, yt = '{http://www.w3.org/2005/Atom}', '{http://www.youtube.com/xml/schemas/2015}'
    return [(e.findtext(yt + 'videoId'), e.findtext(atom + 'title')) for e in ET.fromstring(xml).iter(atom + 'entry')]


def latest(channel_id):
    """feed() of a channel, fetched. Raises OSError (offline...) or ET.ParseError."""
    with urllib.request.urlopen(FEED.format(channel_id), timeout=30) as r:
        return feed(r.read())


def card(videos):
    """The video card's text, under the title: the channel, then the length, views and upload date
    (for many videos: how many, and their length in all)."""
    channels = {v.get('channel') or v.get('uploader') for v in videos} - {None}
    channel = channels.pop() if len(channels) == 1 else 'Various channels' if channels else ''
    if len(videos) > 1:
        total = sum(v.get('duration') or 0 for v in videos)
        return f'{channel}\n{len(videos)} videos' + (f', {length({"duration": total})} in all' if total else '')
    v = videos[0]
    views = f'{v["view_count"]:,} views' if v.get('view_count') is not None else ''
    date = re.sub(r'(\d{4})(\d\d)(\d\d)', r'\1-\2-\3', v.get('upload_date') or '')
    return f'{channel}\n' + '   ·   '.join(filter(None, (length(v), views, date)))


def thumbnail(video_id, size):
    """A video's thumbnail (YouTube's 320x180 one, about 15 KB) shrunk to fit size, as a Pillow image."""
    with urllib.request.urlopen(f'https://i.ytimg.com/vi/{video_id}/mqdefault.jpg', timeout=30) as r:
        image = Image.open(io.BytesIO(r.read()))
    image.thumbnail(size)
    return image


def starts_with_windows():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, 'YouTube Downloader')
        return True
    except OSError:  # FileNotFoundError: not set
        return False


def start_with_windows(on):
    """Start the app in the tray when Windows starts, or not."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, 'YouTube Downloader', 0, winreg.REG_SZ, f'"{GUI_EXE}" --tray')
        elif starts_with_windows():
            winreg.DeleteValue(key, 'YouTube Downloader')


def keep_awake(on):
    """Keep Windows from sleeping while downloads run (the screen may still turn off). It holds for the thread
    that asks, so ask from the UI thread."""
    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(0x80000000 | on))  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


class FLASHWINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint), ('hwnd', ctypes.wintypes.HWND), ('dwFlags', ctypes.wintypes.DWORD),
                ('uCount', ctypes.c_uint), ('dwTimeout', ctypes.wintypes.DWORD)]


def flash(hwnd):
    """Flash the window's taskbar button until the window is looked at."""
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 3 | 12, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
    ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))


def guid(text):
    g = (ctypes.c_byte * 16)()
    ctypes.oledll.ole32.CLSIDFromString(text, g)
    return g


class Taskbar:
    """Progress on the window's taskbar button: Windows' ITaskbarList3, called through its table of methods,
    as ctypes has no COM of its own. Calls raise OSError when Windows refuses them."""
    NONE, NORMAL, PAUSED = 0, 2, 8  # TBPF_ states: no bar, green, yellow

    def __init__(self, hwnd):
        self.hwnd, self.com = hwnd, ctypes.c_void_p()
        ctypes.oledll.ole32.CoInitialize(None)  # this thread's COM; fine if tkdnd started it already
        ctypes.oledll.ole32.CoCreateInstance(guid('{56FDF344-FD6D-11d0-958A-006097C9A090}'), None, 1,  # TaskbarList
                                             guid('{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}'), ctypes.byref(self.com))
        self.method(3)()  # HrInit

    def method(self, index, *argtypes):
        """Method number index (after IUnknown's 3 and ITaskbarList's...), called on this object."""
        address = ctypes.cast(self.com, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[index]
        return functools.partial(ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(address), self.com)

    def show(self, state, fraction=0):
        self.method(10, ctypes.wintypes.HWND, ctypes.c_int)(self.hwnd, state)  # SetProgressState
        if state:
            self.method(9, ctypes.wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong)(  # SetProgressValue
                self.hwnd, int(fraction * 1000), 1000)


def default_folder(last):
    """The folder picked last time if it still exists, else Downloads, else home."""
    return str(next(p for p in (last and Path(last), Path.home() / 'Downloads', Path.home()) if p and p.is_dir()))


def on_screen(geometry):
    """True if a saved window geometry ('WxH+X+Y') starts on a monitor that is still connected."""
    x, y = map(int, re.findall(r'-?\d+', geometry)[2:4])
    return bool(ctypes.windll.user32.MonitorFromPoint(ctypes.wintypes.POINT(x + 50, y + 10), 0))  # 0: none -> NULL


def nearest(labels, wanted):
    """Index of the quality `wanted` in labels (highest first), else the next lower one, else the lowest.
    No wish yet: the highest."""
    if not wanted:
        return 0
    return next((i for i, h in enumerate(labels) if h <= wanted), len(labels) - 1)


def ytdlp_upgrade(dry_run=False):
    """Upgrade yt-dlp in the app's own environment with Setup's uv, downloading only what changed.
    Returns the new version (installed, or available with dry_run), None if this is the newest."""
    env = {**os.environ}
    env.setdefault('UV_CACHE_DIR', str(APP_DIR / 'cache'))  # Setup's download cache
    # same requirement as pyproject.toml: its extras pin the matching yt-dlp-ejs
    args = [UV, 'pip', 'install', '--python', sys.prefix, '--upgrade-package', 'yt-dlp', 'yt-dlp[default,deno]']
    r = subprocess.run(args + ['--dry-run'] * dry_run, capture_output=True, encoding='utf-8', errors='replace',
                       env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        last = (r.stderr.strip() or f'uv exit code {r.returncode}').splitlines()[-1]  # the root cause
        raise RuntimeError(last.strip().removeprefix('Caused by: '))
    m = re.search(r'^ \+ yt-dlp==(\S+)', r.stderr, re.M)
    return m and m[1]


def newer_release(current):
    """(version, download page) of the latest release on GitHub, None if current is the newest."""
    with urllib.request.urlopen(RELEASES, timeout=30) as r:
        release = json.load(r)
    latest = release['tag_name'].removeprefix('v')
    numbers = lambda v: tuple(map(int, v.split('.')))
    return (latest, release['html_url']) if numbers(latest) > numbers(current) else None


def progress_row(parent, row):
    """A full-width progress bar with a text line under it."""
    bar = ttk.Progressbar(parent)
    bar.grid(row=row, column=0, columnspan=3, sticky='ew', pady=(8, 0))
    text = ttk.Label(parent)
    text.grid(row=row + 1, column=0, columnspan=3, sticky='w')
    return bar, text


def menu(widget, *items):
    """Right-click menu for widget. items: (label, shortcut shown, command), or None for a line."""
    m = tk.Menu(widget, tearoff=0)
    for item in items:
        if item:
            m.add_command(label=item[0], accelerator=item[1], command=item[2])
        else:
            m.add_separator()
    widget.bind('<Button-3>', lambda e: m.tk_popup(e.x_root, e.y_root))


def tip(widget, text):
    """Show text under widget while the mouse rests on it."""
    state = {}

    def show():
        box = state['box'] = tk.Toplevel(widget)
        box.wm_overrideredirect(True)
        box.geometry(f'+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 2}')
        tk.Label(box, text=text, justify='left', background='#ffffe1', relief='solid', borderwidth=1,
                 padx=4, pady=2).pack()

    def hide(_):
        if 'job' in state:
            widget.after_cancel(state.pop('job'))
        if 'box' in state:
            state.pop('box').destroy()

    widget.bind('<Enter>', lambda _: state.update(job=widget.after(600, show)), add='+')
    widget.bind('<Leave>', hide, add='+')
    widget.bind('<ButtonPress>', hide, add='+')


def bind_key(widget, key, action):
    """Bind a shortcut like 'Control-o', and its capital, which Tk reports while Caps Lock is on."""
    *mods, letter = key.split('-')
    for k in {letter.lower(), letter.upper()}:
        widget.bind(f'<{"-".join(mods + [k])}>', action)


# the picker's filters: label -> value
FILTERS = {
    'first': {'All': None, **{f'First {n}': n for n in (10, 25, 50, 100, 250)}},
    'uploaded': {'Any time': None, 'Past week': 7, 'Past month': 31, 'Past 3 months': 92, 'Past year': 366},
    'longer': {'Any length': 0, **{f'Over {n} min': n for n in (1, 5, 10, 20, 60)}},
    'shorts': {'With Shorts': None, 'No Shorts': False, 'Only Shorts': True},
}
FILTER_TIPS = {
    'first': 'Channels list their newest videos first',
    'uploaded': 'Upload dates are rough ("3 weeks ago"); videos without one stay',
    'longer': 'Videos with no length shown (Shorts, live) count as short',
    'shorts': 'Shorts are recognised on channels; in playlists use the length filter',
}
TICKS = ('☐', '☑')
WHEN_DONE = ('Do nothing', 'Sleep', 'Shut down')  # after the queue's last download
COUNTDOWN = 60  # seconds to cancel that
STATUS = {'waiting': 'Waiting', 'running': 'Downloading', 'paused': 'Paused', 'done': 'Done'}  # queue jobs' status  # empty and ticked box


def is_short(entry):
    return '/shorts/' in (entry.get('url') or '')


def visible(entries, search='', first=None, uploaded=None, longer=0, shorts=None, now=None):
    """Indices of the entries the picker's filters let through, in order."""
    now = now or time.time()
    words = search.lower().split()
    shown = [i for i, e in enumerate(entries)
             if (shorts is None or is_short(e) == shorts)
             and (not longer or (e.get('duration') or 0) > longer * 60)
             and (not uploaded or (e.get('timestamp') or now) >= now - uploaded * 86400)
             and all(w in (e.get('title') or '').lower() for w in words)]
    return shown[:first]


def length(entry):
    """An entry's length as the picker shows it."""
    status = {'is_upcoming': 'Upcoming', 'is_live': 'Live now'}.get(entry.get('live_status'))
    seconds = int(entry.get('duration') or 0)
    if status or not seconds:
        return status or ('Short' if is_short(entry) else '')
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f'{h}:{m:02}:{s:02}' if h else f'{m}:{s:02}'


class Picker:
    """Modal window to choose which of a playlist's videos to download. done: indices of the ones downloaded
    before, shown grey and, the first time (checked None), not ticked. When it closes:
    result = the chosen entries (None if cancelled), checked = the ticked indices, filters and geometry to remember."""

    def __init__(self, root, title, entries, checked, filters, geometry, done=frozenset()):
        self.entries, self.result, self.job, self.done = entries, None, None, done
        self.checked = set(range(len(entries))) - done if checked is None else set(checked)
        self.filters, self.geometry = filters, None
        w = self.win = tk.Toplevel(root)
        w.title(f'Choose videos - {title}')
        w.transient(root)
        w.geometry(geometry or '800x540')
        w.protocol('WM_DELETE_WINDOW', lambda: self.close(False))

        top = ttk.Frame(w, padding=(10, 10, 10, 6))
        top.pack(fill='x')
        ttk.Label(top, text='Search').pack(side='left')
        self.search = tk.StringVar()
        self.search.trace_add('write', lambda *_: self.show_later())
        search = ttk.Entry(top, textvariable=self.search, width=18)
        search.pack(side='left', padx=(4, 10))
        tip(search, 'Show only titles with all these words')
        self.vars = {}
        for key, choices in FILTERS.items():
            value = filters.get(key)
            var = self.vars[key] = tk.StringVar(value=value if value in choices else next(iter(choices)))
            box = ttk.Combobox(top, textvariable=var, values=list(choices), state='readonly', width=12)
            box.bind('<<ComboboxSelected>>', lambda _: self.show())
            box.pack(side='left', padx=(0, 6))
            tip(box, FILTER_TIPS[key])

        mid = ttk.Frame(w, padding=(10, 0))
        mid.pack(fill='both', expand=True)
        tree = self.tree = ttk.Treeview(mid, columns=('tick', 'title', 'length', 'date'), show='headings')
        for col, text, width, stretch in (('tick', '', 32, False), ('title', 'Title', 420, True),
                                          ('length', 'Length', 80, False), ('date', 'Uploaded', 95, False)):
            tree.heading(col, text=text)
            tree.column(col, width=width, stretch=stretch, anchor='w' if col == 'title' else 'center')
        scroll = ttk.Scrollbar(mid, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        tree.tag_configure('done', foreground='#909090')
        tree.heading('tick', text=TICKS[1], command=lambda: self.tick_all(not all(i in self.checked for i in self.shown)))
        tree.bind('<Button-1>', self.click)
        tree.bind('<space>', lambda _: self.toggle(tree.selection()))  # ticks all selected rows

        bottom = ttk.Frame(w, padding=10)
        bottom.pack(fill='x')
        self.count = ttk.Label(bottom)
        self.count.pack(side='left')
        ttk.Button(bottom, text='Cancel', command=lambda: self.close(False)).pack(side='right')
        self.ok_btn = ttk.Button(bottom, text='OK', command=lambda: self.close(True))
        self.ok_btn.pack(side='right', padx=6)
        ttk.Button(bottom, text='Select none', command=lambda: self.tick_all(False)).pack(side='right', padx=(0, 12))
        ttk.Button(bottom, text='Select all', command=lambda: self.tick_all(True)).pack(side='right')
        w.bind('<Return>', lambda _: self.close(True))
        w.bind('<Escape>', lambda _: self.close(False))

        self.show()
        search.focus()
        w.grab_set()
        w.wait_window()

    def show_later(self):
        """Typing in Search: redraw once the typing pauses."""
        if self.job:
            self.win.after_cancel(self.job)
        self.job = self.win.after(250, self.show)

    def show(self):
        self.job = None
        choice = {key: FILTERS[key][var.get()] for key, var in self.vars.items()}
        self.shown = visible(self.entries, self.search.get(), **choice)
        self.tree.delete(*self.tree.get_children())
        for i in self.shown:
            e = self.entries[i]
            date = time.strftime('%Y-%m-%d', time.localtime(e['timestamp'])) if e.get('timestamp') else ''
            self.tree.insert('', 'end', iid=i, values=(TICKS[i in self.checked], e.get('title') or e['id'], length(e), date),
                             tags=('done',) * (i in self.done))
        self.counted()

    def counted(self):
        ticked = sum(i in self.checked for i in self.shown)
        before = sum(i in self.done for i in self.shown)
        hidden = len(self.entries) - len(self.shown)
        self.count['text'] = (f'{ticked} of {len(self.shown)} selected' + (f', {before} downloaded before (grey)' if before else '')
                              + (f'  ({hidden} hidden by the filters)' if hidden else ''))
        self.ok_btn['state'] = 'normal' if ticked else 'disabled'

    def click(self, event):
        if self.tree.identify_region(event.x, event.y) == 'cell' and self.tree.identify_column(event.x) == '#1':
            self.toggle([self.tree.identify_row(event.y)])
            return 'break'  # a tick, not a selection

    def toggle(self, iids):
        for iid in iids:
            self.checked ^= {int(iid)}
            self.tree.set(iid, 'tick', TICKS[int(iid) in self.checked])
        self.counted()

    def tick_all(self, on):
        for i in self.shown:
            (self.checked.add if on else self.checked.discard)(i)
            self.tree.set(i, 'tick', TICKS[on])
        self.counted()

    def close(self, ok):
        if ok:
            self.result = [self.entries[i] for i in self.shown if i in self.checked]
            if not self.result:
                return  # nothing ticked: OK is disabled, but Enter still gets here
        self.filters = {key: var.get() for key, var in self.vars.items()}
        if self.win.state() == 'normal':
            self.geometry = self.win.geometry()
        self.win.destroy()


OPTIONS = {  # section: (setting, default, label, what it means)
    'Video': [  # cli.video_format()'s rules
        ('compat', False, 'Most compatible video (H.264)',
         'Plays on older TVs, phones and video editors. Bigger files, and 1080p at most.'),
        ('fps60', True, 'Allow 60 fps',
         'Smooth motion for games and sport. Off: 30 fps only, which YouTube often has\n'
         'only up to 480p for 60 fps videos, so the quality list gets shorter.'),
        ('hdr', False, 'Allow HDR',
         'Richer colours on HDR screens, but looks washed out on other screens. Bigger files.'),
    ],
    'Files': [  # cli.files()'s choices
        ('subfolder', True, 'A folder for each playlist or channel', 'Named after it, inside the Save to folder.'),
        ('numbered', True, 'Number the files in playlist order',
         '"01 - Title". Channels list their newest video first, so theirs start with\n'
         'the upload date instead: "2024-05-31 - Title".'),
        ('tags', True, 'Tags and cover art',
         'Title, artist and date, and the thumbnail as cover art, for music players and\n'
         'Explorer. A playlist becomes an album. The cover adds about 0.1 MB per video.'),
        ('split', False, 'A file for each chapter too',
         'For videos with chapters, like an album uploaded as one video: a folder with a\n'
         'file per chapter, next to the whole file. Chapters are always added to the file.'),
    ],
}


class Options:
    """Modal Options window. OK saves into settings and sets changed."""

    def __init__(self, root, settings):
        self.changed = False
        w = tk.Toplevel(root)
        w.title('Options')
        w.transient(root)
        w.resizable(False, False)
        w.geometry(f'+{root.winfo_rootx() + 40}+{root.winfo_rooty() + 40}')
        f = ttk.Frame(w, padding=14)
        f.pack(fill='both')
        choices = {}
        for section, options in OPTIONS.items():
            ttk.Label(f, text=section, font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(14 if choices else 0, 0))
            for key, default, label, meaning in options:
                choices[key] = tk.BooleanVar(value=settings.get(key, default))
                ttk.Checkbutton(f, text=label, variable=choices[key]).pack(anchor='w', pady=(8, 0))
                ttk.Label(f, text=meaning, foreground='#606060').pack(anchor='w', padx=(22, 0))
        row = ttk.Frame(f)
        row.pack(anchor='w', pady=(10, 0))
        ttk.Label(row, text='Subtitles').pack(side='left')
        choices['subs'] = tk.StringVar(value=settings.get('subs', ''))
        ttk.Entry(row, textvariable=choices['subs'], width=16).pack(side='left', padx=6)
        ttk.Label(f, text='Language codes, like "en, ar" for English and Arabic; empty: none. Added to videos, not\n'
                          "audio. YouTube's automatic subtitles are used where there are no others.",
                  foreground='#606060').pack(anchor='w', padx=(22, 0))

        startup = None
        if GUI_EXE.exists():  # installed by Setup, not run from source
            ttk.Label(f, text='Background', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(14, 0))
            startup = tk.BooleanVar(value=starts_with_windows())
            ttk.Checkbutton(f, text='Start with Windows, in the tray', variable=startup).pack(anchor='w', pady=(8, 0))
            ttk.Label(f, text="Checks the subscribed channels for new videos without opening the window.\n"
                              "The tray icon shows the window; right-click it to quit.",
                      foreground='#606060').pack(anchor='w', padx=(22, 0))

        ttk.Label(f, text='Browser button', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(14, 0))
        ttk.Label(f, text='Add a bookmark with this as its address (URL). Clicking it on a YouTube page opens the\n'
                          'page here. The first time, the browser asks: allow it to open YouTube Downloader.',
                  foreground='#606060').pack(anchor='w', pady=(4, 0))
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(4, 0))
        bookmark = ttk.Entry(row)
        bookmark.insert(0, BOOKMARKLET)
        bookmark['state'] = 'readonly'
        bookmark.pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Copy', command=lambda: (w.clipboard_clear(), w.clipboard_append(BOOKMARKLET))).pack(
            side='left', padx=(6, 0))

        def ok():
            settings.update({key: var.get() for key, var in choices.items()})
            if startup is not None:
                start_with_windows(startup.get())
            self.changed = True
            w.destroy()

        buttons = ttk.Frame(f)
        buttons.pack(fill='x', pady=(14, 0))
        ttk.Button(buttons, text='Cancel', command=w.destroy).pack(side='right')
        ttk.Button(buttons, text='OK', command=ok).pack(side='right', padx=6)
        w.bind('<Return>', lambda _: ok())
        w.bind('<Escape>', lambda _: w.destroy())
        w.grab_set()
        w.wait_window()


class Subscriptions:
    """Modal window listing the subscribed channels, to remove them or check them now."""

    def __init__(self, app):
        w = tk.Toplevel(app.root)
        w.title('Subscriptions')
        w.transient(app.root)
        w.geometry(f'+{app.root.winfo_rootx() + 40}+{app.root.winfo_rooty() + 40}')
        f = ttk.Frame(w, padding=14)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=f'New videos on these channels are looked for when the app starts and every {SUBS_HOURS} hours,\n'
                          'and added to the queue, paused: Resume downloads them. To subscribe to a channel,\n'
                          'fetch its link, choose the quality, then click Subscribe.',
                  foreground='#606060').pack(anchor='w')
        tree = ttk.Treeview(f, columns=('title', 'what', 'folder'), show='headings', height=6)
        for col, text, width in (('title', 'Channel', 220), ('what', 'Download', 90), ('folder', 'Save to', 240)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor='center' if col == 'what' else 'w')
        tree.pack(fill='both', expand=True, pady=10)

        def show():
            tree.delete(*tree.get_children())
            for i, sub in enumerate(app.subs):
                tree.insert('', 'end', iid=i, values=(sub['title'], sub['what'], sub['fmt']['paths']['home']))

        def remove():
            chosen = {int(i) for i in tree.selection()}
            app.subs = [s for i, s in enumerate(app.subs) if i not in chosen]
            save_json(SUBS_FILE, app.subs)
            show()

        buttons = ttk.Frame(f)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Close', command=w.destroy).pack(side='right')
        ttk.Button(buttons, text='Check now', command=lambda: (app.check_subs(), w.destroy())).pack(side='right', padx=6)
        ttk.Button(buttons, text='Remove', command=remove).pack(side='left')
        tree.bind('<Delete>', lambda _: remove())
        w.bind('<Escape>', lambda _: w.destroy())
        show()
        w.grab_set()
        w.wait_window()


class App:
    def __init__(self, root, link='', hidden=False):
        """hidden: start in the tray (started with Windows)."""
        self.root = root
        self.settings = load_json(SETTINGS_FILE, {})
        self.subs = [s for s in load_json(SUBS_FILE, [])  # see subscribe()
                     if isinstance(s, dict) and {'channel_id', 'title', 'what', 'fmt', 'seen'} <= s.keys()]
        self.events = queue.Queue()  # UI updates from worker threads; tkinter must only be touched here
        self.cancel = threading.Event()  # stops fetching
        self.pause = threading.Event()  # stops the downloading job at its next progress update
        self.busy = False  # fetching or updating yt-dlp
        self.job_ids = itertools.count(1)
        self.queue = []  # download jobs, oldest first: see download()
        for job in load_json(QUEUE_FILE, []):  # left from last time; they wait for Resume
            try:
                self.queue.append({key: job[key] for key in ('title', 'what', 'fmt', 'videos', 'total')}
                                  | {'sizes': job.get('sizes') or [0] * len(job['videos']),
                                     'id': str(next(self.job_ids)), 'status': 'paused'})
            except (KeyError, TypeError, AttributeError):
                pass  # damaged
        self.running = None  # the job downloading now
        self.run_done = 0  # bytes downloaded since the queue last started, for its progress
        self.streams_done = 0  # bytes of the video's streams downloaded already (its video, while its audio comes)
        self.fraction = 0  # how far the queue is
        self.taskbar = None  # made once the window is on the taskbar
        self.is_mini, self.full_geometry = False, None
        self.upgrading = False  # updating yt-dlp: can't be cancelled midway
        self.videos, self.labels = [], []
        self.plan = None  # (todo, sizes, fmt) for the current choice; None while it's being worked out
        self.plan_id = self.measure_id = 0  # the latest plan / size listing; older ones' results are dropped
        self.fetched_url = self.offered = None  # the link fetched last; the copied link put in the box last
        self.list_title, self.entries, self.channel = None, [], False  # what the link listed
        self.last_file = None  # path of the last downloaded file
        self.thumb_id = None  # the video whose thumbnail the card shows
        self.ydl = None  # the downloader while downloading, so a new speed limit applies right away
        self.links = 0  # file links in the log so far
        self.log_later = lambda msg, tag=None: self.ui(self.log, msg, tag)
        cli.BASE_OPTS['logger'] = cli.Log(lambda msg: self.log_later(msg, 'error'))

        root.title('YouTube Downloader')
        self.icon = tk.PhotoImage(file=Path(__file__).with_name('icon.png'))  # attribute: Tk drops unreferenced images
        root.iconphoto(True, self.icon)
        f = self.full_frame = ttk.Frame(root, padding=12)
        f.pack(fill='both', expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text='Link').grid(row=0, column=0, sticky='w')
        self.url = tk.StringVar()
        self.url_entry = url_entry = ttk.Entry(f, textvariable=self.url, width=60)
        url_entry.grid(row=0, column=1, sticky='ew', padx=6)
        url_entry.bind('<Return>', lambda _: self.fetch())
        url_entry.bind('<Control-Return>', lambda _: self.press(self.download_btn))  # else its Enter also fires
        bind_key(url_entry, 'Control-a', lambda _: self.focus_link())  # Tk's own Ctrl+A jumps to the line start
        url_entry.focus()
        menu(url_entry,
             ('Cut', 'Ctrl+X', lambda: url_entry.event_generate('<<Cut>>')),
             ('Copy', 'Ctrl+C', lambda: url_entry.event_generate('<<Copy>>')),
             ('Paste', 'Ctrl+V', lambda: url_entry.event_generate('<<Paste>>')),
             None,
             ('Paste and Fetch', 'Ctrl+Shift+V', self.paste_fetch),
             ('Select all', 'Ctrl+A', self.focus_link))
        tip(url_entry, 'A YouTube video, playlist or channel link.\n'
                       'Right-click for Paste and Fetch (Ctrl+Shift+V). Ctrl+L jumps here.')
        self.fetch_btn = ttk.Button(f, text='Fetch', command=self.fetch)
        self.fetch_btn.grid(row=0, column=2, sticky='ew')
        tip(self.fetch_btn, 'Look up the videos and their qualities (Enter)')

        ttk.Label(f, text='Save to').grid(row=1, column=0, sticky='w', pady=6)
        self.folder = tk.StringVar(value=default_folder(self.settings.get('folder')))
        ttk.Entry(f, textvariable=self.folder, state='readonly').grid(row=1, column=1, sticky='ew', padx=6)
        self.browse_btn = ttk.Button(f, text='Browse...', command=self.browse)
        self.browse_btn.grid(row=1, column=2, sticky='ew')

        self.title = ttk.Label(f, text='Paste a video or playlist link and press Fetch.', font=('Segoe UI', 10, 'bold'))
        self.title.grid(row=2, column=0, columnspan=2, sticky='w', pady=(10, 4))
        self.choose_btn = ttk.Button(f, text='Choose videos...', command=self.choose)
        self.choose_btn.grid(row=2, column=2, sticky='ew', pady=(10, 4))
        tip(self.choose_btn, "Pick which of the playlist's videos to download")

        choice = ttk.Frame(f)
        choice.grid(row=3, column=0, columnspan=3, sticky='ew')
        self.audio = tk.BooleanVar(value=self.settings.get('audio', False))
        self.video_rb = ttk.Radiobutton(choice, text='Video', variable=self.audio, value=False, command=self.chosen)
        self.res = ttk.Combobox(choice, state='readonly', width=19)
        self.res.bind('<<ComboboxSelected>>', lambda _: self.chosen())
        tip(self.res, 'Each quality shows how much the download would be')
        self.audio_rb = ttk.Radiobutton(choice, text='Audio only', variable=self.audio, value=True, command=self.chosen)
        audio_format = self.settings.get('audio_format')
        self.audio_format = tk.StringVar(value=audio_format if audio_format in cli.AUDIO_FORMATS else 'M4A')
        self.audio_box = ttk.Combobox(choice, textvariable=self.audio_format, values=list(cli.AUDIO_FORMATS),
                                      state='readonly', width=12)
        self.audio_box.bind('<<ComboboxSelected>>', lambda _: self.chosen())
        tip(self.audio_box, "M4A and Opus: YouTube's own audio, as it is.\n"
                            'MP3: plays everywhere (car stereos, old players), but is converted:\n'
                            'slower, and a little lower quality.')
        self.video_rb.pack(side='left')
        self.res.pack(side='left', padx=(4, 16))
        self.audio_rb.pack(side='left')
        self.audio_box.pack(side='left', padx=4)
        speed = self.settings.get('speed')
        self.speed = tk.StringVar(value=speed if speed in LIMITS else 'No limit')
        speed_box = ttk.Combobox(choice, textvariable=self.speed, values=list(LIMITS), state='readonly', width=9)
        speed_box.bind('<<ComboboxSelected>>', lambda _: self.speed_changed())
        speed_box.pack(side='right')
        ttk.Label(choice, text='Speed limit').pack(side='right', padx=(0, 4))
        tip(speed_box, 'Leaves room for browsing while downloading. A change applies right away.')

        about = ttk.Frame(f)  # the video card: thumbnail, then channel, length... and what would be downloaded
        about.grid(row=4, column=0, columnspan=3, sticky='w', pady=8)
        self.thumb = ttk.Label(about)
        self.thumb.pack(side='left', padx=(0, 10))
        self.card = ttk.Label(about, justify='left', foreground='#606060')
        self.card.pack(anchor='w')
        self.summary = ttk.Label(about, justify='left')
        self.summary.pack(anchor='w', pady=(4, 0))

        buttons = ttk.Frame(f)
        buttons.grid(row=5, column=0, columnspan=3, sticky='ew')
        self.download_btn = ttk.Button(buttons, text='Download', command=self.download)
        self.download_btn.pack(side='left')
        tip(self.download_btn, 'Add to the queue below (Ctrl+Enter). Its downloads run one after another,\n'
                               'so meanwhile you can fetch and add the next link.')
        self.subscribe_btn = ttk.Button(buttons, text='Subscribe', command=self.subscribe)
        self.subscribe_btn.pack(side='left', padx=(6, 0))
        tip(self.subscribe_btn, "For channels: download the videos the channel uploads from now on, as chosen here.\n"
                                "No need to choose videos: Cancel the video list, pick the quality, click Subscribe.\n"
                                f'New videos are looked for when the app starts and every {SUBS_HOURS} hours, and queued paused.')
        self.cancel_btn = ttk.Button(buttons, text='Cancel', command=self.stop)
        self.cancel_btn.pack(side='left', padx=6)
        tip(self.cancel_btn, 'Stop fetching (Esc)')
        self.folder_btn = ttk.Button(buttons, text='Open folder', command=self.open_folder)
        self.folder_btn.pack(side='left')
        tip(self.folder_btn, 'The download folder, with the last download selected (Ctrl+O)')
        self.file_btn = ttk.Button(buttons, text='Open file', command=lambda: self.open_file(self.last_file))
        self.file_btn.pack(side='left', padx=6)
        tip(self.file_btn, 'Play the last downloaded file')
        self.upgrade_btn = ttk.Button(buttons, text='Update yt-dlp', command=self.upgrade)
        if UV:
            self.upgrade_btn.pack(side='right')
        options_btn = ttk.Button(buttons, text='Options...', command=self.options)
        options_btn.pack(side='right', padx=6)
        subs_btn = ttk.Button(buttons, text='Subscriptions...', command=lambda: Subscriptions(self))
        subs_btn.pack(side='right', padx=(6, 0))
        tip(subs_btn, 'The channels whose new videos are downloaded')
        mini_btn = ttk.Button(buttons, text='Mini window', command=lambda: self.mini(True))
        mini_btn.pack(side='right')
        tip(mini_btn, "A small window that stays on top of the others, with just the downloads' progress (Ctrl+M)")
        tip(options_btn, 'Video compatibility, 60 fps and HDR; file names, tags, chapters and subtitles')
        tip(self.upgrade_btn, 'Get the newest yt-dlp, which does the downloading.\nTry this when downloads stop working.')

        self.total_bar, self.total_text = progress_row(f, 6)  # fetching

        head = ttk.Frame(f)
        head.grid(row=8, column=0, columnspan=3, sticky='ew', pady=(12, 4))
        ttk.Label(head, text='Queue', font=('Segoe UI', 10, 'bold')).pack(side='left')
        ttk.Label(head, text='When done').pack(side='left', padx=(18, 4))
        self.when_done = tk.StringVar(value=WHEN_DONE[0])
        when_box = ttk.Combobox(head, textvariable=self.when_done, values=WHEN_DONE, state='readonly', width=11)
        when_box.pack(side='left')
        tip(when_box, 'After the last download: sleep or shut down the PC, with a minute to cancel.\n'
                      'Applies once; the app always starts with Do nothing.')
        self.clear_btn = ttk.Button(head, text='Clear finished', command=self.clear_finished)
        self.remove_btn = ttk.Button(head, text='Remove', command=self.remove_jobs)
        self.resume_btn = ttk.Button(head, text='Resume', command=self.resume)
        self.pause_btn = ttk.Button(head, text='Pause', command=self.pause_all)
        for button in (self.clear_btn, self.remove_btn, self.resume_btn, self.pause_btn):
            button.pack(side='right', padx=(6, 0))
        tip(self.pause_btn, 'Stop downloading. Partial files are kept, even if you close the app:\n'
                            'Resume goes on from there, also after a restart.')
        tip(self.resume_btn, 'Go on with the paused downloads, and try the failed ones again')
        tip(self.remove_btn, 'Take the selected jobs off the queue (Delete). Files already saved stay.')
        box = ttk.Frame(f)
        box.grid(row=9, column=0, columnspan=3, sticky='ew')
        self.jobs = ttk.Treeview(box, columns=('title', 'what', 'videos', 'status'), show='headings', height=4)
        for col, text, width, stretch in (('title', 'Title', 330, True), ('what', 'Download', 110, False),
                                          ('videos', 'Videos', 80, False), ('status', 'Status', 100, False)):
            self.jobs.heading(col, text=text)
            self.jobs.column(col, width=width, stretch=stretch, anchor='w' if col == 'title' else 'center')
        scroll = ttk.Scrollbar(box, command=self.jobs.yview)
        self.jobs.configure(yscrollcommand=scroll.set)
        self.jobs.pack(side='left', fill='x', expand=True)
        scroll.pack(side='right', fill='y')
        self.jobs.bind('<<TreeviewSelect>>', lambda _: self.refresh())
        self.jobs.bind('<Delete>', lambda _: self.press(self.remove_btn))
        self.bar, self.bar_text = progress_row(f, 10)  # the file downloading now

        self.log_box = tk.Text(f, height=8, state='disabled', wrap='word', font='TkDefaultFont')
        self.log_box.grid(row=12, column=0, columnspan=3, sticky='nsew', pady=(10, 0))
        f.rowconfigure(12, weight=1)
        self.log_box.tag_configure('error', foreground='#c42b1c')
        self.log_box.tag_configure('done', foreground='#0f7b0f')
        self.log_box.tag_configure('file', foreground='#0563c1', underline=True)
        self.log_box.tag_bind('file', '<Enter>', lambda _: self.log_box.configure(cursor='hand2'))
        self.log_box.tag_bind('file', '<Leave>', lambda _: self.log_box.configure(cursor=''))
        menu(self.log_box,
             ('Copy', 'Ctrl+C', lambda: self.log_box.event_generate('<<Copy>>')),
             ('Copy all, for a bug report', '', self.copy_log),
             None,
             ('Clear', '', self.clear_log))

        m = self.mini_frame = ttk.Frame(root, padding=10)  # instead of f in mini mode
        self.mini_title = ttk.Label(m, text='Nothing downloading.', width=46)
        self.mini_title.pack(anchor='w')
        self.mini_bar = ttk.Progressbar(m, maximum=1000)
        self.mini_bar.pack(fill='x', pady=6)
        self.mini_text = ttk.Label(m)
        self.mini_text.pack(anchor='w')
        row = ttk.Frame(m)
        row.pack(fill='x', pady=(8, 0))
        self.mini_pause = ttk.Button(row, command=lambda: self.pause_all() if self.can_pause() else self.resume())
        self.mini_pause.pack(side='left')
        ttk.Button(row, text='Full window', command=lambda: self.mini(False)).pack(side='right')

        root.bind('<Control-Return>', lambda _: self.press(self.download_btn))
        bind_key(root, 'Control-m', lambda _: self.mini(not self.is_mini))
        root.bind('<Escape>', lambda _: self.press(self.cancel_btn))
        bind_key(root, 'Control-o', lambda _: self.press(self.folder_btn))
        bind_key(root, 'Control-l', lambda _: self.focus_link())
        bind_key(root, 'Control-Shift-v', lambda _: self.paste_fetch())
        self.away = True  # focus is in another app (as far as we know): coming back offers a copied link
        root.bind('<FocusIn>', self.activated)
        root.bind('<FocusOut>', lambda e: e.widget is root and root.after(50, self.left))

        root.withdraw()  # while its size is worked out: no flicker
        root.update_idletasks()
        # no narrower than its controls: a width saved by an older version, with fewer buttons, would cut some off
        self.min_width = self.full_frame.winfo_reqwidth()
        root.minsize(self.min_width, 1)
        geometry = self.settings.get('geometry')
        if geometry:
            root.geometry(geometry if on_screen(geometry) else geometry.split('+')[0])  # size only: its monitor is gone
        root.protocol('WM_DELETE_WINDOW', self.hide_or_close)
        # ponytail: a daemon thread rather than run_detached()'s, so a crash can't leave the process running
        self.tray = pystray.Icon('YouTubeDownloader', Image.open(Path(__file__).with_name('icon.png')), 'YouTube Downloader',
                                 pystray.Menu(pystray.MenuItem('Show', lambda: self.ui(self.received, ''), default=True),
                                              # not close() from inside poll(): poll reschedules itself on the window
                                              pystray.MenuItem('Quit', lambda: self.ui(root.after_idle, self.close))))
        threading.Thread(target=self.tray.run, daemon=True).start()  # clicks arrive on its thread
        try:
            TkinterDnD._require(root)  # loads tkdnd into this window, as TkinterDnD.Tk() would
        except (RuntimeError, tk.TclError):
            pass  # no drag and drop
        else:
            for widget in descendants(root):  # Windows only drops on the widget under the mouse
                if not isinstance(widget, tk.Menu):
                    widget.drop_target_register(DND_TEXT)
                    widget.dnd_bind('<<Drop>>', self.dropped)
        threading.Thread(target=self.listen, daemon=True).start()
        self.show_queue()
        self.poll()
        if self.queue:
            self.log(f'{len(self.queue)} unfinished download{"s" * (len(self.queue) > 1)} from last time: '
                     'Resume goes on with them.')
        if not hidden:  # else it stays in the tray until shown
            root.deiconify()
        if link:
            root.after_idle(lambda: self.received(link))
        root.after(1000, self.watch)
        if UV and time.time() - self.settings.get('update_checked', 0) > UPDATE_CHECK_DAYS * 86400:
            self.run(lambda: ytdlp_upgrade(dry_run=True), self.upgrade_checked)
        if UV:  # installed by Setup; errors (offline?) mean: look again next start
            self.run(lambda: newer_release(VERSION), lambda new, _: new and self.log(
                f'YouTube Downloader {new[0]} is available (you have {VERSION}). Double-click here to download it.',
                path=new[1]))

    # --- plumbing between the UI thread and worker threads

    def ui(self, fn, *args):
        """Run fn(*args) on the UI thread. Safe to call from any thread."""
        self.events.put(lambda: fn(*args))

    def poll(self):
        try:
            while not self.events.empty():
                self.events.get()()
        finally:
            self.root.after(50, self.poll)  # a bug in one update must not freeze the window

    def run(self, work, done):
        """Run work() on a background thread, then done(result, error) on the UI thread."""
        def target():
            try:
                result, error = work(), None
            except Exception as e:
                result, error = None, e
            self.ui(done, result, error)
        threading.Thread(target=target, daemon=True).start()

    # --- UI helpers

    def refresh(self):
        """Enable only the controls that make sense right now."""
        def enable(widget, on, on_state='normal'):
            widget['state'] = on_state if on else 'disabled'
        ready = not self.busy and bool(self.videos or self.labels)  # labels only: a channel with no videos chosen
        enable(self.fetch_btn, not self.busy)
        enable(self.browse_btn, not self.busy)
        enable(self.video_rb, ready and bool(self.labels))
        enable(self.audio_rb, ready)
        enable(self.res, ready and not self.audio.get(), 'readonly')
        enable(self.audio_box, ready and self.audio.get(), 'readonly')
        enable(self.download_btn, not self.busy and self.plan is not None and bool(self.plan[0]))
        enable(self.subscribe_btn, ready and bool(self.channel))
        enable(self.cancel_btn, self.busy and not self.upgrading)
        enable(self.upgrade_btn, not self.busy and not self.running)
        enable(self.file_btn, bool(self.last_file))
        enable(self.choose_btn, not self.busy and len(self.entries) > 1)
        states = {job['status'] for job in self.queue}
        enable(self.pause_btn, self.can_pause())
        enable(self.resume_btn, bool(states & {'paused', 'failed'}))
        self.mini_pause['text'] = 'Pause' if self.can_pause() else 'Resume'
        enable(self.mini_pause, self.can_pause() or bool(states & {'paused', 'failed'}))
        enable(self.remove_btn, bool(self.jobs.selection()))
        enable(self.clear_btn, 'done' in states)

    def can_pause(self):
        return any(job['status'] in ('waiting', 'running') for job in self.queue)

    def log(self, msg, tag=None, path=None):
        """Add a line to the log: tag 'error' or 'done' colours it, a path (a file or https:// page) opens on double-click."""
        tags = [tag] if tag else []
        if path:
            self.links += 1
            self.log_box.tag_bind(f'link{self.links}', '<Double-Button-1>',  # a file, or a web page
                                  lambda _: os.startfile(path) if path.startswith('https://') else self.open_file(path))
            tags += ['file', f'link{self.links}']
        self.log_box['state'] = 'normal'
        self.log_box.insert('end', msg + '\n', tags)
        self.log_box.see('end')
        self.log_box['state'] = 'disabled'

    def clear_log(self):
        self.log_box['state'] = 'normal'
        self.log_box.delete('1.0', 'end')
        self.log_box['state'] = 'disabled'

    def copy_log(self):
        """The whole log headed with the versions, ready to paste into a bug report."""
        self.root.clipboard_clear()
        self.root.clipboard_append(f'YouTube Downloader {VERSION}, yt-dlp {ytdlp_version}, Windows {platform.version()}'
                                   f'\n\n{self.log_box.get("1.0", "end").strip()}')

    def press(self, button):
        """A keyboard shortcut for button: does what a click would, if it's enabled."""
        if button.instate(['!disabled']):
            button.invoke()
        return 'break'  # handled: no other binding for these keys

    def focus_link(self):
        self.url_entry.focus()
        self.url_entry.select_range(0, 'end')
        self.url_entry.icursor('end')
        return 'break'

    def received(self, link):
        """A link from outside: a ytd: link, a later start of the app, or dropped on the window. Only YouTube
        links are taken (any web page can make ytd: links); fetched unless a fetch or a dialog is on."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        link = link.strip()
        if not link:
            return  # the app was just started again: coming to the front is all
        if not YOUTUBE_LINK.match(link):
            self.log(f'Not a YouTube link: {link[:200]}', 'error')
            return
        self.url.set(link)
        self.focus_link()
        if not self.busy and self.root.grab_current() is None:
            self.fetch()

    def dropped(self, event):
        self.received(event.data)
        return event.action

    def listen(self):
        """Links handed over by later starts of the app. Runs on a thread for good."""
        try:
            listener = Listener(PIPE, 'AF_PIPE')
        except OSError:
            return  # another window listens already (it didn't answer the hand-over in time)
        while True:
            try:
                with listener.accept() as conn:
                    link = conn.recv_bytes(4096).decode('utf-8', 'replace')  # bytes, never pickles: safe to accept
            except (OSError, EOFError):
                continue  # a caller that gave up
            self.ui(self.received, link)

    def paste_fetch(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            return 'break'  # nothing, or no text, on the clipboard
        self.fetch()
        return 'break'

    def activated(self, event):
        """The window came to the front: put a YouTube link copied meanwhile in the Link box, without fetching it.
        Only once per link, and never over something typed in the box."""
        if event.widget is not self.root or not self.away:
            return  # focus moving between the window's own widgets, or back from its own dialogs
        self.away = False
        try:
            clip = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if clip != self.offered and YOUTUBE_LINK.match(clip) and self.url.get().strip() in ('', self.fetched_url):
            self.offered = clip
            self.url.set(clip)
            self.focus_link()  # Enter fetches it

    def left(self):
        """Focus left the main window: note whether it went to another app or to one of ours (picker, dialog)."""
        try:
            self.away = self.root.focus_get() is None
        except KeyError:  # tkinter can't name a combobox's drop-down list: that's ours
            self.away = False

    def open_file(self, path, select=False):
        """Open a downloaded file with its app, or show it selected in its folder."""
        if not os.path.exists(path):
            self.log(f'{os.path.basename(path)} was moved or deleted.', 'error')
            return
        try:
            if select:
                subprocess.Popen(f'explorer /select,"{path}"')
            else:
                os.startfile(path)
        except OSError as e:  # e.g. no app installed for this file type
            self.log(f'Could not open {os.path.basename(path)}: {e.strerror}', 'error')

    def open_folder(self):
        """The download folder, or the last downloaded file selected in its folder when it's in there."""
        if self.last_file and Path(self.last_file).is_relative_to(self.folder.get()) and os.path.exists(self.last_file):
            self.open_file(self.last_file, select=True)
        else:
            os.startfile(self.folder.get())

    def speed_changed(self):
        self.settings['speed'] = self.speed.get()
        if self.ydl:  # yt-dlp reads the limit as it downloads
            self.ydl.params['ratelimit'] = LIMITS[self.speed.get()]

    @staticmethod
    def show(bar, text, value, maximum, message):
        bar['maximum'] = maximum or 1  # unknown size: Tk refuses a maximum of 0
        bar['value'] = value if maximum else 0
        text['text'] = message

    def start_fetch(self):
        self.busy = True
        self.cancel.clear()
        self.show(self.total_bar, self.total_text, 0, 0, '')
        self.refresh()

    def report(self, error, cancelled_msg):
        """Log a job's failure. yt-dlp already logged its own DownloadErrors."""
        if isinstance(error, DownloadCancelled):
            self.log(cancelled_msg)
        elif not isinstance(error, DownloadError):
            self.log(f'Error: {error}', 'error')

    # --- actions

    def browse(self):
        folder = filedialog.askdirectory(initialdir=self.folder.get(), mustexist=True)
        if folder:
            self.folder.set(os.path.normpath(folder))
            self.settings['folder'] = self.folder.get()
            save_json(SETTINGS_FILE, self.settings)
            self.replan()  # "already downloaded" depends on the folder
            self.measure()

    def fetch(self):
        url = self.url.get().strip()
        if not url or self.busy:
            return
        self.videos, self.labels, self.plan, self.entries = [], [], None, []
        self.measure_id += 1  # sizes still coming for the old videos' qualities are dropped
        self.fetched_url = url
        self.title['text'] = 'Fetching info...'
        self.summary['text'] = self.card['text'] = self.thumb['image'] = ''
        self.thumb_id = None
        self.start_fetch()
        self.run(lambda: cli.listing(url, self.listing_progress), self.listed)

    def listing_progress(self, count):
        """cli.listing() progress; runs on the fetch thread. Cancel keeps the videos listed so far."""
        if count % 25 == 0:
            self.ui(self.show, self.total_bar, self.total_text, 0, 0,
                    f'Listing videos: {count} so far. Cancel stops the list here.')
        return not self.cancel.is_set()

    def failed(self, error, cancelled_msg):
        self.title['text'] = 'Cancelled.' if isinstance(error, DownloadCancelled) else 'Could not fetch that link.'
        self.report(error, cancelled_msg)
        self.refresh()

    def listed(self, result, error):
        self.busy = False
        if error:
            return self.failed(error, 'Fetch cancelled.')
        self.list_title, self.entries, complete, self.channel = result
        self.checked, self.full = None, {}  # ticked in the picker (None: all); video id -> full info
        if not complete:
            self.log(f'Stopped listing at {len(self.entries)} videos.')
        if len(self.entries) > 1:
            self.choose()
        else:
            self.check_videos(self.entries)

    def choose(self):
        """Let the user pick which listed videos to download."""
        done = cli.downloaded(self.folder.get())
        picker = Picker(self.root, self.list_title, self.entries, self.checked, self.settings.get('filters', {}),
                        self.settings.get('picker_geometry'), {i for i, e in enumerate(self.entries) if e.get('id') in done})
        self.settings['filters'] = picker.filters
        if picker.geometry:
            self.settings['picker_geometry'] = picker.geometry
        if picker.result is not None:
            self.checked = picker.checked
            self.check_videos(picker.result)
        elif not self.videos and self.channel:
            self.title['text'] = f'{self.list_title}  ({len(self.entries)} videos listed, none chosen)'
            self.offer()  # the usual qualities, for Subscribe
        elif not self.videos:
            self.title['text'] = 'Cancelled.'
            self.refresh()

    def check_videos(self, picked):
        """Get the full info of the picked entries (each only once), then show what they'd download."""
        self.picked = picked
        missing = [e for e in picked if e['id'] not in self.full]
        self.videos, self.plan = [], None
        self.title['text'] = f'Checking {len(missing)} video{"s" if len(missing) != 1 else ""}...'
        self.summary['text'] = ''
        self.start_fetch()
        self.run(lambda: dict(zip([e['id'] for e in missing], cli.check(missing, self.track))), self.checked_videos)

    def track(self, results, total):
        """cli.check() progress; runs on the fetch thread."""
        for i, video in enumerate(results, 1):
            if self.cancel.is_set():
                raise DownloadCancelled()
            self.ui(self.show, self.total_bar, self.total_text, i, total, f'Checked {i} of {total} video{"s" * (total != 1)}')
            yield video

    def checked_videos(self, result, error):
        self.busy = False
        if error:
            return self.failed(error, 'Check cancelled.')
        self.full.update(result)  # None for unavailable videos: not asked about again
        self.videos = [cli.place(self.full[e['id']], e, self.list_title, self.channel) for e in self.picked if self.full[e['id']]]
        count, listed, title = len(self.videos), len(self.entries), str(self.list_title)
        title = title if len(title) <= 60 else title[:59] + '…'  # room for the Choose videos button
        self.title['text'] = f'{title}  ({count}{f" of {listed}" if listed > 1 else ""} video{"s" if listed != 1 else ""})'
        if not self.videos:
            self.summary['text'] = 'None of these videos can be downloaded.'
            self.refresh()
            return
        self.show_card()
        self.offer()

    def show_card(self):
        """The channel, length, views and date of the videos, and the first one's thumbnail (fetched on a thread)."""
        self.card['text'] = card(self.videos)
        first, scale = self.videos[0], self.root.winfo_fpixels('1i') / 96  # Windows' display scaling
        if first.get('id') != self.thumb_id:  # else it's shown already (e.g. the picker kept the first video)
            self.thumb['image'], self.thumb_id = '', first.get('id')
            self.run(lambda: thumbnail(first['id'], (int(160 * scale), int(90 * scale))),
                     lambda image, error: self.thumbnailed(first['id'], image))

    def thumbnailed(self, video_id, image):
        if image and self.videos and self.videos[0].get('id') == video_id:  # no error, and still the video shown
            self.thumb_image = ImageTk.PhotoImage(image)  # attribute: Tk drops unreferenced images
            self.thumb['image'] = self.thumb_image

    def rules(self, section='Video'):
        """The choices made in one section of Options. Video's decide which video versions may be picked."""
        return {key: self.settings.get(key, default) for key, default, *_ in OPTIONS[section]}

    def job_options(self, fmt, audio):
        """fmt plus the Files options (names, tags, chapters, subtitles) for the listed videos."""
        subs = [] if audio else re.findall(r'[\w-]+', self.settings.get('subs', ''))
        title = self.list_title if len(self.entries) > 1 or self.channel else None  # None: a single video
        return {**cli.files(fmt, title, len(self.entries), self.channel, subs=subs, **self.rules('Files')),
                'paths': {'home': self.folder.get()}}  # a job keeps its folder when another is chosen

    def offer(self):
        """Fill the quality list for the videos under the current options, preselect the usual choice, re-plan.
        A channel with no videos chosen gets the usual qualities, for Subscribe."""
        if self.videos:
            self.labels = cli.heights((f for v in self.videos for f in v.get('formats') or ()), **self.rules())
        else:
            self.labels = [h for h in QUALITIES if h <= 1080 or not self.rules()['compat']]  # H.264: 1080p at most
            self.summary['text'] = ('No videos chosen. Subscribe downloads the videos this channel uploads from now on,\n'
                                    'as chosen above.')
            self.summary['foreground'] = ''
        self.res['values'] = [f'{h}p' for h in self.labels]
        self.audio.set(self.settings.get('audio', False) or not self.labels)  # no labels: nothing but audio on offer
        if self.labels:
            self.res.current(nearest(self.labels, self.settings.get('quality')))
        self.replan()
        if self.videos:
            self.measure()

    def measure(self):
        """Add each quality's download size to the list, one by one on a thread."""
        self.measure_id += 1
        measure_id, videos = self.measure_id, self.videos
        fmts = [self.job_options(cli.video_format(self.labels[i:], **self.rules()), False) for i in range(len(self.labels))]

        def work():
            for i, fmt in enumerate(fmts):
                if measure_id != self.measure_id:
                    return  # the videos or options changed meanwhile
                _, sizes, _ = cli.plan(videos, fmt)
                self.ui(self.measured, measure_id, i, sum(sizes))
        threading.Thread(target=work, daemon=True).start()

    def measured(self, measure_id, i, total):
        if measure_id != self.measure_id:
            return
        values = list(self.res['values'])
        values[i] = f'{self.labels[i]}p  ({format_bytes(total)})' if total else f'{self.labels[i]}p'
        current = self.res.current()
        self.res['values'] = values
        self.res.current(current)  # the box shows the old text until reselected

    def chosen(self):
        """The user picked Video/Audio, a quality or an audio format: remember it for next time."""
        self.settings['audio'] = self.audio.get()
        self.settings['audio_format'] = self.audio_format.get()
        if not self.audio.get():
            self.settings['quality'] = self.labels[self.res.current()]
        self.replan()

    def options(self):
        if Options(self.root, self.settings).changed and (self.videos or self.labels):
            self.offer()  # the qualities, and which files already exist, may differ now

    def choice(self):
        """yt-dlp options for the current choice: the audio format, or the quality (the lower ones as fallbacks)."""
        if self.audio.get():
            return self.job_options(cli.AUDIO_FORMATS[self.audio_format.get()], True)
        return self.job_options(cli.video_format(self.labels[self.res.current():], **self.rules()), False)

    def replan(self):
        """Work out what the current choice would download. Runs on a thread; the latest choice wins."""
        if not self.videos:
            return self.refresh()  # e.g. a channel with none chosen: only Subscribe
        self.plan_id += 1
        plan_id, self.plan, videos, fmt = self.plan_id, None, self.videos, self.choice()
        self.summary['text'] = 'Working out the size...'
        self.refresh()
        self.run(lambda: cli.plan(videos, fmt), lambda result, error: self.planned(plan_id, fmt, result, error))

    def planned(self, plan_id, fmt, result, error):
        if plan_id != self.plan_id:
            return  # the choice changed while this one was being worked out
        if error:
            self.summary['text'] = f'Could not work out the size: {error}'
            return
        todo, sizes, picked = result
        self.plan = (todo, sizes, fmt)
        free = shutil.disk_usage(self.folder.get()).free
        self.summary['text'] = cli.summary(len(self.videos), todo, sizes, picked, free)
        self.summary['foreground'] = '#c42b1c' if cli.needed(sizes) > free else ''  # red: not enough disk space
        self.refresh()

    def download(self):
        """Add the chosen videos to the queue. Its jobs download one at a time, oldest first."""
        todo, sizes, fmt = self.plan
        self.queue.append({'id': str(next(self.job_ids)), 'title': self.list_title, 'fmt': fmt,
                           'what': self.audio_format.get() if self.audio.get() else self.res.get().split()[0],
                           'videos': list(todo), 'sizes': list(sizes), 'total': len(todo), 'status': 'waiting'})
        self.plan = None  # no second job by a double click; changing the choice offers Download again
        self.summary['text'] += '\nAdded to the queue.'
        self.save_queue()
        self.next_job()

    def subscribe(self):
        """Download the fetched channel's new videos from now on, as chosen now (quality, folder, Files options).
        A subscription: the channel's id and title, the choice (what, fmt) and the feed's videos seen already."""
        what = self.audio_format.get() if self.audio.get() else self.res.get().split()[0]
        old = next((s for s in self.subs if s['channel_id'] == self.channel), None)
        sub = {'channel_id': self.channel, 'title': self.list_title, 'what': what, 'fmt': self.choice(),
               'seen': old and old['seen']}  # None: the first check notes what's there now, without queueing it
        self.subs = [s for s in self.subs if s is not old] + [sub]
        save_json(SUBS_FILE, self.subs)
        self.log(f'{"Changed the subscription to" if old else "Subscribed to"} {self.list_title}: its new videos will be '
                 f'downloaded as {what}.', 'done')
        if not old:
            self.check_subs([sub])

    def watch(self):
        """Check the subscriptions now and every SUBS_HOURS."""
        if self.subs:
            self.check_subs()
        self.root.after(SUBS_HOURS * 3600 * 1000, self.watch)

    def check_subs(self, subs=None):
        """Look for new videos on the subscribed channels, on a thread."""
        subs = list(subs or self.subs)

        def work():
            found = []
            for sub in subs:
                try:
                    found.append((sub, latest(sub['channel_id'])))
                except (OSError, ET.ParseError) as e:  # offline, or YouTube sent something else
                    found.append((sub, e))
            return found
        self.run(work, lambda found, _: self.subs_checked(found))

    def subs_checked(self, found):
        """Queue each channel's new videos, paused: videos not in its feed before, nor downloaded before."""
        new_ones = failed = 0
        for sub, videos in found:
            if not any(s is sub for s in self.subs):
                continue  # unsubscribed meanwhile
            if isinstance(videos, Exception):
                failed += 1
                continue
            ids = [video_id for video_id, _ in videos]
            if sub['seen'] is not None:
                done = cli.downloaded(sub['fmt']['paths']['home'])
                new = [(i, t) for i, t in reversed(videos) if i not in sub['seen'] and i not in done]  # oldest first
                if new:
                    self.queue_new(sub, new)
                    new_ones += len(new)
            sub['seen'] = list(dict.fromkeys(ids + (sub['seen'] or [])))[:200]  # newest first
        save_json(SUBS_FILE, self.subs)
        if failed:
            self.log(f'Could not check {failed} subscription{"s" * (failed > 1)} (no internet?). '
                     f'Trying again in {SUBS_HOURS} hours.', 'error')
        elif not new_ones:
            self.log('Subscriptions checked: no new videos.')

    def queue_new(self, sub, new):
        """Add a channel's new videos to the queue as a paused job, and say so."""
        videos = [cli.place({'webpage_url': f'https://www.youtube.com/watch?v={i}', 'id': i, 'title': t},  # stubs
                            {'playlist_index': n}, sub['title'], True) for n, (i, t) in enumerate(new, 1)]
        self.queue.append({'id': str(next(self.job_ids)), 'title': sub['title'], 'what': sub['what'], 'fmt': sub['fmt'],
                           'videos': videos, 'sizes': [0] * len(videos), 'total': len(videos), 'status': 'paused'})
        self.save_queue()
        self.show_queue()
        message = f'{len(new)} new video{"s" * (len(new) > 1)} from {sub["title"]}: added to the queue, paused.'
        self.log(message + ' Resume downloads them.')
        self.tray.notify(message, 'YouTube Downloader')

    def hide_or_close(self):
        """The window's X: keep running in the tray while there's downloading to do or channels to watch."""
        if not (self.can_pause() or self.subs):
            return self.close()
        self.root.withdraw()
        if not self.settings.get('tray_told'):
            self.settings['tray_told'] = True
            self.tray.notify('Still running here, for the downloads and subscriptions. Right-click to quit.',
                             'YouTube Downloader')

    def next_job(self):
        """Start the first waiting job, unless one is downloading."""
        job = next((j for j in self.queue if j['status'] == 'waiting'), None)
        if job and not self.running and not self.upgrading:
            self.running, job['status'] = job, 'running'
            self.pause.clear()
            keep_awake(True)
            self.show(self.bar, self.bar_text, 0, 0, '')
            self.taskbar_show(Taskbar.NORMAL, self.fraction)
            fmt = {**job['fmt'], 'ratelimit': LIMITS[self.speed.get()]}
            self.run(lambda: self.download_all(job, fmt), lambda _, error: self.job_ended(job, error))
        self.show_queue()

    def download_all(self, job, fmt):
        """One pass over a job's videos; runs on the download thread."""
        videos, home = list(job['videos']), fmt['paths']['home']
        with cli.downloader(fmt, self.progress, log=self.log_later, saved=lambda path: self.ui(self.saved, path, home)) as ydl:
            self.ydl = ydl
            for i, video in enumerate(videos, 1):
                if self.pause.is_set():
                    raise DownloadCancelled()
                self.streams_done = 0
                self.log_later(f'[{i}/{len(videos)}] {video.get("title")}')
                self.ui(lambda title=video.get('title'): self.mini_title.configure(text=title))
                if cli.download_one(ydl, video, log=self.log_later):
                    self.ui(self.video_done, job, video)

    def video_done(self, job, video):
        i = next(i for i, v in enumerate(job['videos']) if v is video)
        job['videos'].pop(i)
        self.run_done += job['sizes'].pop(i)
        self.save_queue()
        self.show_queue()

    def job_ended(self, job, error):
        self.running = self.ydl = None
        if not any(j is job for j in self.queue):
            self.log('Removed from the queue. Partial files were kept.')
        elif isinstance(error, DownloadCancelled):
            job['status'] = 'paused'
            self.log('Paused. Partial files were kept: Resume goes on from there.')
        else:
            if error:
                self.report(error, '')
            job['status'] = 'failed' if job['videos'] else 'done'
            if job['videos']:
                self.log(f'{len(job["videos"])} video(s) failed: Resume tries them again.', 'error')
            else:
                self.log(f'Done: {job["title"]}', 'done')
        self.save_queue()
        self.next_job()
        if not self.running:
            keep_awake(False)
            self.run_done = 0
            done = all(j['status'] == 'done' for j in self.queue)
            paused = any(j['status'] == 'paused' for j in self.queue)
            self.show(self.bar, self.bar_text, done, 1, 'All done.' if done else '')
            self.taskbar_show(Taskbar.PAUSED if paused else Taskbar.NONE, self.fraction)
            self.mini_title['text'] = 'Paused.' if paused else 'All done.' if done else 'Nothing downloading.'
            self.mini_text['text'] = ''
            self.tray.title = 'YouTube Downloader'
            if not isinstance(error, DownloadCancelled):  # the queue ran out, rather than Pause or Remove stopping it
                self.finished(failed=any(j['status'] == 'failed' for j in self.queue))
        self.replan()  # the folder's contents changed
        self.measure()

    def pause_all(self):
        """Stop downloading: the running job at its next progress update, and the waiting ones stay."""
        for job in self.queue:
            if job['status'] == 'waiting':
                job['status'] = 'paused'
        self.pause.set()
        self.show_queue()

    def resume(self):
        """Go on with the paused jobs, and try failed videos again."""
        for job in self.queue:
            if job['status'] in ('paused', 'failed'):
                job['status'] = 'waiting'
        self.pause.clear()  # pressed right after Pause: the running job goes on
        self.next_job()

    def remove_jobs(self):
        """Take the selected jobs off the queue; a downloading one stops first."""
        chosen = set(self.jobs.selection())
        if self.running and self.running['id'] in chosen:
            self.pause.set()  # its job_ended() starts the next one
        self.queue = [j for j in self.queue if j['id'] not in chosen]
        self.save_queue()
        self.show_queue()

    def clear_finished(self):
        self.queue = [j for j in self.queue if j['status'] != 'done']
        self.show_queue()

    def show_queue(self):
        chosen = self.jobs.selection()
        self.jobs.delete(*self.jobs.get_children())
        for job in self.queue:
            status = STATUS.get(job['status']) or f'{len(job["videos"])} failed'
            self.jobs.insert('', 'end', iid=job['id'], values=(
                job['title'], job['what'], f'{job["total"] - len(job["videos"])} of {job["total"]}', status))
        self.jobs.selection_set([i for i in chosen if self.jobs.exists(i)])
        self.refresh()

    def save_queue(self):
        """The unfinished jobs, for resuming after a restart. Their videos as stubs: stream links expire anyway."""
        save_json(QUEUE_FILE, [{key: job[key] for key in ('title', 'what', 'fmt', 'sizes', 'total')}
                               | {'videos': [cli.stub(v) for v in job['videos']]} for job in self.queue if job['videos']])

    def progress(self, d):
        """yt-dlp progress hook; runs on the download thread."""
        if self.pause.is_set():
            raise DownloadCancelled()  # stops yt-dlp mid-stream; the .part file stays for resuming
        if d['status'] == 'finished':  # one of the video's streams
            self.streams_done += d.get('total_bytes') or d.get('downloaded_bytes') or 0
        elif d['status'] == 'downloading':
            self.ui(self.progressed, d['info_dict'].get('vcodec') == 'none', d['downloaded_bytes'],
                    d.get('total_bytes') or d.get('total_bytes_estimate'), d.get('speed'), self.streams_done)

    def progressed(self, audio, done, total, speed, before):
        """Progress of the stream downloading now (done of total bytes) and of the whole queue.
        before: the bytes of this video's streams downloaded already."""
        left = max(sum(sum(job['sizes']) for job in self.queue if job['status'] in ('running', 'waiting'))
                   - before - done, (total or 0) - done, 0)  # sizes are 0 for subscriptions' videos: at least this file
        finished = self.run_done + before + done
        self.fraction = finished / (finished + left) if finished + left else 0
        file = f'{"Audio" if audio else "Video"}: {f"{done / total:.0%}  " if total else ""}{format_bytes(done)} of {format_bytes(total)}'
        if speed:
            file += f'  at {format_bytes(speed)}/s'
        self.show(self.bar, self.bar_text, done, total, f'{file}      {queue_line(left, speed)}')
        self.mini_bar['value'] = self.fraction * 1000
        self.mini_text['text'] = queue_line(left, speed)
        self.taskbar_show(Taskbar.NORMAL, self.fraction)
        self.tray.title = f'YouTube Downloader: {self.fraction:.0%} downloaded'  # its tooltip; set only when changed

    def taskbar_show(self, state, fraction=0):
        try:
            self.taskbar = self.taskbar or Taskbar(int(self.root.wm_frame(), 16))
            self.taskbar.show(state, fraction)
        except OSError:
            pass  # not on the taskbar yet: the next update shows it

    def finished(self, failed):
        """The queue ran out: a sound and a flashing taskbar button, then what When done says."""
        winsound.MessageBeep(winsound.MB_ICONHAND if failed else winsound.MB_ICONASTERISK)
        flash(int(self.root.wm_frame(), 16))
        if self.root.state() == 'withdrawn':  # in the tray: no taskbar button to flash
            self.tray.notify('Some downloads failed: Resume tries them again.' if failed else 'The downloads are done.',
                             'YouTube Downloader')
        action = self.when_done.get()
        if action != WHEN_DONE[0]:
            self.when_done.set(WHEN_DONE[0])  # once: not again after later downloads
            self.countdown(action)

    def countdown(self, action):
        """Sleep or shut down the PC in a minute, unless Cancel is clicked."""
        w = tk.Toplevel(self.root)
        w.title(action)
        w.attributes('-topmost', True)
        w.resizable(False, False)
        text = ttk.Label(w, padding=(20, 16, 20, 8))
        text.pack()
        ttk.Button(w, text='Cancel', command=w.destroy).pack(pady=(0, 14))

        def tick(left):
            if not w.winfo_exists():
                return  # cancelled
            if left:
                text['text'] = f'The downloads are done. {action} in {left} seconds.'
                self.root.after(1000, tick, left - 1)
            else:
                w.destroy()
                self.power(action)
        tick(COUNTDOWN)

    def power(self, action):
        if action == 'Shut down':
            subprocess.Popen(['shutdown', '/s', '/t', '0'], creationflags=subprocess.CREATE_NO_WINDOW)
            self.close()  # saves the settings and the queue
        else:  # sleep, not hibernate; SetSuspendState needs the shutdown privilege switched on first
            ctypes.windll.ntdll.RtlAdjustPrivilege(19, True, False, ctypes.byref(ctypes.c_bool()))  # 19: SeShutdown
            ctypes.windll.powrprof.SetSuspendState(False, False, False)

    def mini(self, on):
        """Switch to a small window that stays on top, with just the downloads' progress, or back."""
        if on == self.is_mini:
            return
        self.is_mini, root = on, self.root
        if root.state() == 'zoomed':
            root.state('normal')  # its normal size is the one to come back to
        root.attributes('-topmost', on)  # before moving: after, Windows puts it back where it was
        if on:
            self.full_geometry = root.geometry()
            root.minsize(1, 1)
            self.full_frame.pack_forget()
            self.mini_frame.pack(fill='both', expand=True)
            saved = self.settings.get('mini_geometry')
            root.geometry('')  # the mini frame's own size
            root.geometry('+' + saved.split('+', 1)[1] if saved and on_screen(saved)
                          else f'+{root.winfo_screenwidth() - 480}+80')
        else:
            self.settings['mini_geometry'] = root.geometry()
            self.mini_frame.pack_forget()
            self.full_frame.pack(fill='both', expand=True)
            root.minsize(self.min_width, 1)
            root.geometry(self.full_geometry)

    def saved(self, path, home):
        self.last_file = path
        self.log(f'Saved: {os.path.relpath(path, home)}', path=path)
        self.refresh()

    def upgrade_checked(self, new, error):
        """Result of the weekly look for a newer yt-dlp. Errors (offline?) mean: look again next start."""
        if not error:
            self.settings['update_checked'] = time.time()
        if new:
            self.log(f'yt-dlp {new} is available (you have {ytdlp_version}). Click "Update yt-dlp" to get it.')

    def upgrade(self):
        self.busy = self.upgrading = True
        self.refresh()
        self.log('Updating yt-dlp...')
        self.run(ytdlp_upgrade, self.upgraded)

    def upgraded(self, new, error):
        self.busy = self.upgrading = False
        self.next_job()  # one may have been resumed meanwhile
        if error:
            self.log(f'Could not update yt-dlp: {error}', 'error')
        elif not new:
            self.log(f'yt-dlp is up to date ({ytdlp_version}).', 'done')
        else:
            self.settings['update_checked'] = time.time()
            self.log(f'yt-dlp updated to {new} - restart the app to use it.', 'done')
            if messagebox.askyesno('Update yt-dlp', f'yt-dlp was updated to {new}.\n\nRestart the app now to use it?'):
                self.root.after_idle(self.restart)  # not from inside poll(): it reschedules itself on the window

    def restart(self):
        self.close()
        subprocess.Popen([GUI_EXE])

    def stop(self):
        self.cancel.set()
        self.cancel_btn['state'] = 'disabled'  # feedback; the job notices at its next update

    def close(self):
        if self.is_mini:
            self.settings['mini_geometry'], self.settings['geometry'] = self.root.geometry(), self.full_geometry
        elif self.root.state() == 'normal':  # not minimized or maximized: the size to reopen at
            self.settings['geometry'] = self.root.geometry()
        save_json(SETTINGS_FILE, self.settings)
        self.save_queue()
        # A running download stops at its next progress update; its .part file stays resumable.
        self.cancel.set()
        self.pause.set()
        self.tray.stop()  # takes the icon off the tray
        self.root.destroy()


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays instead of blurry
    args = sys.argv[1:]
    hidden = '--tray' in args  # started with Windows (Options: Start with Windows)
    link = link_from([a for a in args if a != '--tray'])
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW(None, False, 'YouTubeDownloader')  # also Setup's AppMutex: it asks to close the app first
    if ctypes.get_last_error() == 183 and (hidden or hand_over(link)):  # ERROR_ALREADY_EXISTS: the app is open already
        return
    root = tk.Tk()
    App(root, link, hidden)
    root.mainloop()


if __name__ == '__main__':
    main()
