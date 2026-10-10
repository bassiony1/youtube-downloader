"""ytd - download a YouTube video or playlist into the current folder.

Usage: ytd <youtube_url>

Already-downloaded files are skipped and partial (.part) files are resumed,
so after an interruption just run the same command again. Playlist and channel
videos in the download history (shared with the app) are skipped too.
"""
import copy
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg
import mutagen
from tqdm import tqdm
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, format_bytes, replace_extension

# (words in a yt-dlp error, what it means in plain words); first match wins
FRIENDLY = [
    (r'video subtitles', 'Could not get the subtitles (YouTube limits them), so this video comes without them.'),
    (r'Private video', 'This video is private.'),
    (r'confirm your age|age.restricted', 'This video is age-restricted: YouTube only shows it to signed-in adults.'),
    (r'not a bot', 'YouTube suspects automated downloading. Wait an hour or so, then try again.'),
    (r'members.only|Join this channel', 'This video is for channel members only.'),
    (r'in your country', 'This video is blocked in your country.'),
    (r'Premieres in|live event will begin', 'This video is not out yet (a premiere or an upcoming live stream).'),
    (r'HTTP Error 429|Too Many Requests', 'YouTube is limiting downloads from your connection. Wait a while, then try again.'),
    (r'HTTP Error 403', 'YouTube refused the download. Try again; if it keeps happening, update yt-dlp.'),
    (r'No space left|Errno 28', 'The disk is full.'),
    (r'getaddrinfo|Failed to resolve|No such host|timed out|Network is unreachable|Connection (refused|reset|aborted)'
     r'|RemoteDisconnected', 'Could not reach YouTube. Check the internet connection and try again.'),
    (r'is not a valid URL', 'That is not a link. Paste the full address of a YouTube video or playlist.'),
    (r'Unsupported URL', "Can't download from that link."),
    (r'Video unavailable|is unavailable|has been removed|terminated',
     'This video is unavailable (removed, or the link is wrong).'),
]


def friendly(msg):
    """A yt-dlp error in plain words, naming the video. Unknown errors stay as they are."""
    msg = msg.removeprefix('ERROR: ')
    video = re.match(r'\[[\w:]+\] ([^:\s]+): ', msg)  # "[youtube] jNQXAC9IVRw: Private video..."
    for pattern, words in FRIENDLY:
        if re.search(pattern, msg, re.I):
            return f'{video[1]}: {words}' if video else words
    return msg


class Log:
    """yt-dlp logger: its errors go to write(text, 'error') in plain words, everything else is dropped.
    note() is for this app's own messages."""

    def __init__(self, write=lambda text, tag=None: print(text, file=sys.stderr)):
        self.write = write

    def error(self, msg):
        self.write(friendly(msg), 'error')

    def note(self, msg):
        self.write(msg, None)

    def debug(self, msg):
        pass

    info = warning = debug


QUIET = Log(lambda text, tag=None: None)
BASE_OPTS = {
    'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
    'js_runtimes': {'deno': {}, 'node': {}, 'bun': {}},  # yt-dlp uses whichever is installed
    'outtmpl': '%(title)s [%(id)s].%(ext)s',
    'quiet': True,
    'no_warnings': True,
    'color': 'no_color',  # plain text for Log
    'logger': Log(),
    'extractor_args': {'youtubetab': {'approximate_date': ['']}},  # list entries get dates from "3 weeks ago"
}


def to_audio(codec, kbps=None):
    """Options to keep only the audio, as codec (converted if it isn't that already)."""
    return {'format': 'bestaudio/best', 'final_ext': codec,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': codec, 'preferredquality': kbps}]}


AUDIO_FORMATS = {  # name -> yt-dlp options. M4A and Opus are YouTube's own audio as is; MP3 is converted
    'M4A': {'format': 'bestaudio[ext=m4a]/bestaudio/best'},
    'Opus': {**to_audio('opus'), 'format': 'bestaudio[acodec=opus]/bestaudio/best'},
    **{f'MP3 {kbps} kbps': to_audio('mp3', kbps) for kbps in (320, 192, 128)},
}
ARCHIVE = Path(os.environ['APPDATA'], 'ytd', 'archive.txt')  # download history, as yt-dlp's: 'youtube <id>' lines
PLACE = ('playlist', 'playlist_index', 'album', 'track_number')  # what place() adds to an info
OLD_NAME = re.compile(r'\[([\w-]{11})\]\.\w+$')  # 'Title [id].mp4': how version 0.5 and older named files
CHECK_WORKERS = 8  # videos checked at once, until YouTube pushes back
# errors that mean "not now" rather than "never": YouTube pushing back, or the connection
PUSHBACK = re.compile(r'not a bot|HTTP Error 429|Too Many Requests|timed out|getaddrinfo|Failed to resolve'
                      r'|No such host|Network is unreachable|Connection (refused|reset|aborted)|RemoteDisconnected', re.I)
WAITS = (10, 30, 90)  # seconds before each retry of a check that got pushed back
PAUSE = 2  # seconds between checks once YouTube pushed back


def video_format(labels, compat=False, fps60=True, hdr=False):
    """Best video labelled labels[0] (e.g. 1080p), else the next lower label the video has, saved as mp4.
    Nothing that low at all: the nearest resolution above.

    compat: only H.264 video, which every player handles (YouTube has it up to 1080p).
    fps60, hdr: whether 60 fps and HDR versions may be picked. Same rules as allowed().
    """
    rules = ('[vcodec^=avc1]' if compat else '') + ('' if fps60 else '[fps<=?30]') + ('' if hdr else '[format_note!*=HDR]')
    tries = [f'bv*[format_note^={q}p]{rules}+ba' for q in labels]
    return {'format': '/'.join(tries + ['bv*+ba/b']), 'format_sort': [f'res:{labels[0]}', 'ext:mp4:m4a'],
            'merge_output_format': 'mp4'}


def allowed(f, compat=False, fps60=True, hdr=False):
    """Whether a video format passes video_format()'s rules."""
    return ((not compat or (f.get('vcodec') or '').startswith('avc1'))
            and (fps60 or (f.get('fps') or 0) <= 30)
            and (hdr or 'HDR' not in (f.get('format_note') or '')))


def choose(prompt, labels):
    """Show a numbered menu and return the index of the picked label."""
    print(prompt)
    for i, label in enumerate(labels, 1):
        print(f'  [{i}] {label}')
    while True:
        answer = input('> ').strip()
        if answer.isdigit() and 1 <= int(answer) <= len(labels):
            return int(answer) - 1
        print(f'Enter a number from 1 to {len(labels)}.')


def quality(f):
    """YouTube's quality label: 1080 for '1080p60 HDR'. Pixel sizes mislead: a 1920x804 film and
    a 1080x1920 Short are both 1080p. None for audio-only formats and storyboards."""
    m = re.match(r'(\d+)p', f.get('format_note') or '')
    return int(m[1]) if m else None


def heights(formats, **rules):
    """Distinct quality labels of the formats allowed() by the rules, highest first."""
    return sorted({quality(f) for f in formats if allowed(f, **rules)} - {None}, reverse=True)


def checking_bar(results, total):
    return tqdm(results, total=total, desc='Checking videos', unit='video')


def every_100(count):
    """listing() progress for the command line."""
    if count % 100 == 0:
        print(f'  {count} videos listed...')
    return True


def listing(url, more=every_100):
    """What a link points to, cheaply: (title, entries, complete, channel).

    A playlist's or channel's entries are as YouTube lists them: title, length, rough date, no formats yet,
    plus their 'playlist_index'; check() gets the rest for the ones wanted. A single video's only entry is
    its full info already. more(count) runs after each entry: False stops reading further pages (complete
    is then False). channel: the channel's id if the link is a channel's, whose list starts with the newest
    video, else None.
    """
    # A Mix (list=RD...) is YouTube's endless radio of similar videos, which links to videos from the home page and
    # search carry: just the video. Other lists stay whole: a playlist in search results links to watch?v=..&list=PL..
    mix = re.search(r'[?&]list=RD', url) is not None
    with YoutubeDL({**BASE_OPTS, 'noplaylist': mix}) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
        while info.get('_type') in ('url', 'url_transparent'):  # e.g. watch?v=..&list=.. points to the playlist
            info = ydl.extract_info(info['url'], download=False, process=False)
        if 'entries' not in info:
            return info.get('title'), [info], True, None
        # A channel's link (@name, channel/..., c/..., user/..., any tab): its info has the channel's id, but a channel
        # with several tabs (Videos, Shorts...) is listed under the link's name, so its own id isn't that. A playlist
        # also has its owner's channel id, but it's listed from a list= link.
        channel = info.get('channel_id') if 'list=' not in url else None
        entries = []
        for entry in flatten(info['entries']):  # lazy pages, fetched through ydl, so read them before it closes
            entries.append({**entry, 'playlist_index': len(entries) + 1})
            if not more(len(entries)):
                return info.get('title'), entries, False, channel
    return info.get('title'), entries, True, channel


def flatten(entries):
    """The videos in a playlist's entries. A channel link nests its Videos, Shorts and Live tabs as playlists."""
    for entry in entries:
        if entry.get('_type') == 'playlist':
            yield from flatten(entry.get('entries') or ())
        else:
            yield entry


class Pace:
    """Checks go in parallel until YouTube pushes back; after that one at a time, with a pause between."""

    def __init__(self):
        self.slow = threading.Event()
        self.one = threading.Lock()
        self.stop = threading.Event()  # nobody waits for the results any more: checks pausing to retry give up


def check(entries, track=checking_bar):
    """Full raw info of each listed entry, in order; None for the unavailable ones.
    `track` wraps the results as they arrive.

    Raw (unprocessed) infos can go through format selection many times: once per size
    estimate and once more for the real download, so nothing is fetched twice.
    """
    pace = Pace()
    pool = ThreadPoolExecutor(CHECK_WORKERS)
    try:
        # pool.map keeps playlist order; cancel_futures lets Ctrl+C skip the checks not started yet
        return list(track(pool.map(lambda entry: fetch(entry, pace), entries), len(entries)))
    finally:
        pace.stop.set()
        pool.shutdown(cancel_futures=True)


def fetch(entry, pace):
    """Raw info of one entry (it is its own if it has formats already), None if it's unavailable.
    If YouTube pushes back, waits and tries again, and from then on all checks go one at a time."""
    if 'formats' in entry:
        return entry
    for wait in (*WAITS, None):
        try:
            with YoutubeDL({**BASE_OPTS, 'logger': QUIET}) as ydl:  # one per call: not documented as thread-safe
                if not pace.slow.is_set():
                    return ydl.extract_info(entry['url'], download=False, process=False)
                with pace.one:
                    time.sleep(PAUSE)
                    return ydl.extract_info(entry['url'], download=False, process=False)
        except DownloadError as e:
            if wait is None or pace.stop.is_set() or not PUSHBACK.search(str(e)):
                BASE_OPTS['logger'].error(str(e))  # private, deleted, still pushed back...: say why and skip it
                return None
            if not pace.slow.is_set():
                pace.slow.set()
                BASE_OPTS['logger'].note('YouTube is pushing back, so videos are now checked one at a time, '
                                         'with pauses. This can take a few minutes.')
            if pace.stop.wait(wait):
                return None


def place(raw, entry, title, channel):
    """raw plus its entry's place in the listed playlist or channel, which files() names it by. A playlist's
    videos are also tagged as an album of numbered tracks, unless YouTube names their album. A single video's
    entry has no place: raw stays as it is."""
    if 'playlist_index' not in entry:
        return raw
    extra = {'playlist': title, 'playlist_index': entry['playlist_index']}
    if not (channel or raw.get('album')):
        extra.update(album=title, track_number=entry['playlist_index'])
    return {**raw, **extra}


def files(fmt, title=None, count=1, channel=False, subfolder=True, numbered=True, tags=True, split=False, subs=()):
    """fmt plus how the files come out: names, tags, chapters, subtitles.

    title: the listed playlist's or channel's (None: a single video), count: how many videos it listed;
    their infos need place(). subfolder: save them in a folder named title. numbered: names start with the
    playlist position, or with the upload date for a channel (its positions shift with every upload).
    Chapters are always added: YouTube lists them, nothing to download. tags: title, artist, date... and the
    thumbnail as cover art. split: also a file per chapter, in a folder named like the file.
    subs: subtitle languages ('en', 'ar'...) to add, YouTube's automatic ones where there are no others.
    """
    name = '%(title)s'
    if title and numbered:
        name = ('%(upload_date>%Y-%m-%d)s - ' if channel else f'%(playlist_index)0{max(2, len(str(count)))}d - ') + name
    if title and subfolder:
        name = '%(playlist)s/' + name
    opts = {'outtmpl': {'default': f'{name}.%(ext)s', 'chapter': f'{name}/%(section_number)02d - %(section_title)s.%(ext)s'}}
    pps = [*fmt.get('postprocessors', ()), {'key': 'FFmpegMetadata', 'add_chapters': True, 'add_metadata': tags}]
    if subs:
        opts.update(writesubtitles=True, writeautomaticsub=True, subtitleslangs=list(subs))
        pps.insert(-1, {'key': 'FFmpegEmbedSubtitle'})  # yt-dlp's own order: before FFmpegMetadata
    if tags:
        opts['writethumbnail'] = True
        # JPG before the download: smaller than the PNG that EmbedThumbnail makes of YouTube's WebP
        pps += [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg', 'when': 'before_dl'}, {'key': 'EmbedThumbnail'}]
    if split:
        pps.append({'key': 'FFmpegSplitChapters'})  # keeps the whole file
    return {**fmt, **opts, 'postprocessors': pps}


def downloaded(folder='.'):
    """Ids of the videos downloaded before: in the history, or named the old way in folder (if it exists)."""
    try:
        text = ARCHIVE.read_text(encoding='utf-8')
    except OSError:
        text = ''  # nothing downloaded yet
    ids = {line.removeprefix('youtube ') for line in text.splitlines() if line.startswith('youtube ')}
    names = os.listdir(folder) if os.path.isdir(folder) else ()  # e.g. a subscription's folder on an unplugged drive
    return ids | {m[1] for name in names if (m := OLD_NAME.search(name))}


def tag_chapters(info):
    """Tag the files split off a video's chapters as the tracks of an album named after the video
    (yt-dlp copies the whole video's tags into each)."""
    for n, chapter in enumerate(info.get('chapters') or (), 1):
        if not chapter.get('filepath'):
            continue  # not split off
        try:
            f = mutagen.File(chapter['filepath'], easy=True)
            if f is not None:  # None: a type mutagen doesn't know. Not `if f`: a file without tags is falsy
                f.update(title=chapter.get('title') or f'Chapter {n}', tracknumber=str(n), album=info.get('title') or '')
                f.save()
        except (mutagen.MutagenError, OSError):
            pass  # only tags; the file is fine


def remember(video_id):
    """Add a downloaded video to the history."""
    try:
        ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        with ARCHIVE.open('a', encoding='utf-8') as f:
            f.write(f'youtube {video_id}\n')
    except OSError:
        pass  # ponytail: the history only saves re-checking; the file itself is saved


def saved_as(ydl, info):
    """The file the video of a processed info is saved as. Converted ones (MP3) count by their final name."""
    path = ydl.prepare_filename(info)
    return replace_extension(path, ydl.params['final_ext']) if ydl.params.get('final_ext') else path


def stub(raw):
    """What of a video to keep for downloading it later (after a restart): its link, title and place()."""
    return {key: raw[key] for key in ('webpage_url', 'id', 'title', *PLACE) if key in raw}


def refetch(ydl, video):
    """Fresh raw info of a video or stub (stream links expire), in the same place()."""
    return {**ydl.extract_info(video['webpage_url'], download=False, process=False),
            **{key: video[key] for key in PLACE if key in video}}


def plan(videos, fmt):
    """Dry-run yt-dlp's format selection.

    Returns (videos not downloaded yet, each one's size in bytes, Counter of the qualities actually picked).
    """
    todo, sizes, picked = [], [], Counter()
    with YoutubeDL({**BASE_OPTS, **fmt}) as ydl:
        for raw in videos:
            info = ydl.process_ie_result(copy.deepcopy(raw), download=False)
            if os.path.exists(saved_as(ydl, info)):
                continue
            todo.append(raw)
            streams = info.get('requested_formats') or [info]
            sizes.append(sum(f.get('filesize') or f.get('filesize_approx') or 0 for f in streams))
            video = streams[0]
            picked['audio' if video.get('vcodec') == 'none' else f'{quality(video) or "?"}p'] += 1
    return todo, sizes, picked


def needed(sizes):
    """Disk space a download needs: every file, plus room to merge the biggest one's video and audio."""
    return sum(sizes) + max(sizes, default=0)  # ponytail: audio needs no merge room, so this over-asks a little


def summary(count, todo, sizes, picked, free=None):
    """plan()'s result in words; `count` is how many videos were checked, `free` the disk's free bytes."""
    lines = [f'{count - len(todo)} already downloaded - will be skipped.'] if len(todo) < count else []
    if not todo:
        return '\n'.join(lines + ['Nothing left to download.'])
    many = len(todo) > 1
    qualities = ', '.join(f'{q} x{n}' if many else q for q, n in picked.most_common())
    lines.append(f"Will download {f'{len(todo)} videos: ' if many else ''}{qualities}, about {format_bytes(sum(sizes))}")
    if free is not None and needed(sizes) > free:
        lines.append(f'Not enough disk space: about {format_bytes(needed(sizes))} needed, {format_bytes(free)} free.')
    return '\n'.join(lines)


class ProgressBar:
    """yt-dlp progress hook: one tqdm bar per downloaded stream (video, then audio)."""

    def __init__(self):
        self.bar = None

    def __call__(self, d):
        if d['status'] != 'downloading':  # finished or error
            if self.bar:
                self.bar.close()
                self.bar = None
            return
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        if self.bar is None:
            kind = 'audio' if d['info_dict'].get('vcodec') == 'none' else 'video'
            # initial= makes a resumed download start at its offset without faking the speed
            self.bar = tqdm(desc=f'  {kind}', total=total, initial=d['downloaded_bytes'],
                            unit='B', unit_scale=True, unit_divisor=1024, dynamic_ncols=True)
        self.bar.total = total  # fragmented streams refine their estimate as they go
        self.bar.update(d['downloaded_bytes'] - self.bar.n)


def downloader(fmt, progress_hook, log=print, saved=None):
    """A YoutubeDL that downloads with these format options, reporting through the hook and log,
    and each finished file's full path to saved()."""
    def on_postprocess(d):
        if d['status'] == 'started' and d['postprocessor'] == 'Merger':
            log('  merging video + audio...')
        if d['status'] == 'finished' and d['postprocessor'] == 'MoveFiles':  # the last step, once per video
            remember(d['info_dict']['id'])
            tag_chapters(d['info_dict'])
            if saved:
                saved(os.path.abspath(d['info_dict']['filepath']))
    return YoutubeDL({**BASE_OPTS, **fmt, 'noprogress': True,
                      'progress_hooks': [progress_hook], 'postprocessor_hooks': [on_postprocess]})


def download_one(ydl, video, log=print):
    """Download one video from its raw info, or from its stub() (fetched fresh first).
    Skips it if the file is there already (e.g. an earlier job got it). False if it failed (yt-dlp said why)."""
    try:
        raw = video if 'formats' in video else refetch(ydl, video)
        if os.path.exists(saved_as(ydl, ydl.process_ie_result(copy.deepcopy(raw), download=False))):
            log('  already downloaded')
            return True
        ydl.process_ie_result(copy.deepcopy(raw), download=True)  # copy: raw stays reusable
        return True
    except DownloadError as e:
        if 'video subtitles' in str(e):  # extras, and YouTube often refuses them (HTTP 429): get the video anyway
            log('  downloading without subtitles...')
            subs = ydl.params.pop('writesubtitles', False), ydl.params.pop('writeautomaticsub', False)
            try:
                return download_one(ydl, video, log)
            finally:
                ydl.params['writesubtitles'], ydl.params['writeautomaticsub'] = subs
        # Stream links fetched earlier can be refused (403) or expire; like yt-dlp's
        # --load-info-json, retry once with fresh ones. The .part file is resumed.
        log('  retrying with fresh links...')
        try:
            ydl.process_ie_result(refetch(ydl, video), download=True)
            return True
        except DownloadError:
            return False


def download(videos, fmt):
    failed = 0
    with downloader(fmt, ProgressBar()) as ydl:
        for i, raw in enumerate(videos, 1):
            print(f"[{i}/{len(videos)}] {raw.get('title')}")
            failed += not download_one(ydl, raw)
    if failed:
        sys.exit(f'{failed} video(s) failed - run the same command again to retry them.')
    print('Done.')


def main():
    if len(sys.argv) != 2:
        sys.exit('Usage: ytd <youtube_url>')
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')  # emoji titles crash cp125x pipes (e.g. Git Bash)

    try:
        print('Fetching info...', flush=True)
        title, entries, _, channel = listing(sys.argv[1])
        many = len(entries) > 1
        done = downloaded() if many else set()  # a single video is downloaded again if asked for
        new = [e for e in entries if e.get('id') not in done]
        if len(new) < len(entries):
            print(f'{len(entries) - len(new)} downloaded before - skipped (the history is {ARCHIVE}).')
        if not new:
            return
        videos = [place(v, e, title, channel) for e, v in zip(new, check(new)) if v]
        print(f'{title} ({len(videos)} video{"s" if len(videos) != 1 else ""})')
        if not videos:
            return
        available = heights(f for v in videos for f in v.get('formats') or ())

        audio = None
        while True:
            if audio is None:
                audio = choose('Download:', ['Video', 'Audio only']) == 1
            if audio:
                fmt = list(AUDIO_FORMATS.values())[choose('Audio format:', list(AUDIO_FORMATS))]
            else:
                fmt = video_format(available[choose('Resolution:', [f'{h}p' for h in available]):])
            fmt = files(fmt, title if many else None, len(entries), channel)
            todo, sizes, picked = plan(videos, fmt)
            print(summary(len(videos), todo, sizes, picked, shutil.disk_usage('.').free))
            if not todo:
                return
            if choose('Start download?', ['Yes', 'No, choose again']) == 0:
                break
            if audio:
                audio = None  # back to the video/audio question

        download(todo, fmt)
    except (KeyboardInterrupt, EOFError):
        print('\nInterrupted. Partial downloads were kept - run the same command again to resume.')
        sys.exit(130)
    except DownloadError:
        sys.exit(1)  # yt-dlp already printed the error


if __name__ == '__main__':
    main()
