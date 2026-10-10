"""Run: python test_ytd.py   (offline)"""
import builtins
import io
import os
import tempfile

from ytd import cli as ytd

answers = iter(['abc', '0', '3', '2'])
builtins.input = lambda _='': next(answers)
assert ytd.choose('pick', ['a', 'b', 'c']) == 2  # rejects junk and out-of-range, accepts 3

assert ytd.heights([
    {'format_note': '720p', 'height': 720},
    {'format_note': '720p60', 'height': 720},
    {'format_note': '1080p60 HDR', 'width': 1920, 'height': 804},  # widescreen film: labelled 1080p
    {'format_note': '1080p', 'width': 1080, 'height': 1920},       # vertical Short: 1080p, not 1920p
    {'format_note': None, 'height': 480},                          # unlabelled (HLS) formats are ignored
    {'format_note': 'storyboard', 'height': 90},
    {'format_note': 'medium', 'vcodec': 'none'},                   # audio
]) == [1080, 720]


def fake_video(vid, video_heights):
    formats = [{'format_id': 'a', 'url': 'http://x/a', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a', 'filesize': 10}]
    formats += [{'format_id': str(h), 'url': f'http://x/{h}', 'ext': 'mp4', 'format_note': f'{h}p', 'height': h, 'width': h * 16 // 9,
                 'vcodec': 'avc1', 'acodec': 'none', 'filesize': h} for h in video_heights]
    return {'id': vid, 'title': vid, 'extractor': 'test', 'extractor_key': 'Test',
            'webpage_url': f'http://x/{vid}', 'formats': formats}


os.chdir(tempfile.mkdtemp())
videos = [fake_video('a', [1080, 720]), fake_video('b', [480, 360]), fake_video('c', [720])]
open('c [c].mp4', 'w').close()  # c's video is already downloaded

todo, sizes, picked = ytd.plan(videos, ytd.video_format([720, 480, 360]))
assert [v['id'] for v in todo] == ['a', 'b']
assert picked == {'720p': 1, '480p': 1}  # b has no 720p, gets nearest lower
assert sizes == [720 + 10, 480 + 10]  # video + audio stream sizes
assert ytd.needed(sizes) == 1220 + 730 and ytd.needed([]) == 0  # plus room to merge the biggest

todo, sizes, picked = ytd.plan(videos, ytd.AUDIO_FORMATS['M4A'])  # same raw infos reused
assert len(todo) == 3 and picked == {'audio': 3} and sizes == [10, 10, 10]  # c's mp4 doesn't count as its audio
open('b [b].mp3', 'w').close()  # b already converted to MP3: skipped for MP3, not for M4A
assert [v['id'] for v in ytd.plan(videos, ytd.AUDIO_FORMATS['MP3 192 kbps'])[0]] == ['a', 'c']
assert len(ytd.plan(videos, ytd.AUDIO_FORMATS['M4A'])[0]) == 3


def fmt(note, vcodec, fps, size):
    return {'format_id': f'{note}{vcodec}', 'url': 'http://x/v', 'ext': 'mp4', 'format_note': note, 'vcodec': vcodec,
            'acodec': 'none', 'fps': fps, 'height': int(note.split('p')[0]), 'filesize': size}


rich = fake_video('r', [])  # AV1 up to 1440p60 HDR, H.264 up to 1080p; 1080p only at 60 fps
rich['formats'] += [fmt('1440p60 HDR', 'av01', 60, 5000), fmt('1440p60', 'av01', 60, 4000),
                    fmt('1080p60 HDR', 'av01', 60, 3000), fmt('1080p60', 'av01', 60, 2000),
                    fmt('1080p60', 'avc1', 60, 2500), fmt('480p', 'avc1', 30, 500), fmt('480p', 'av01', 30, 400)]
rf = rich['formats']
assert ytd.heights(rf) == [1440, 1080, 480] and ytd.heights(rf, compat=True) == [1080, 480]
assert ytd.heights(rf, fps60=False) == [480] and ytd.heights(rf, hdr=True) == [1440, 1080, 480]


def size(labels, **rules):
    return ytd.plan([rich], ytd.video_format(labels, **rules))[1][0] - 10  # minus the audio


assert size([1440, 1080, 480]) == 4000  # HDR skipped by default
assert size([1440, 1080, 480], hdr=True) == 5000
assert size([1080, 480], compat=True) == 2500 and size([480], compat=True) == 500
assert size([480], fps60=False) in (400, 500)

# file names: a playlist's videos numbered in a folder, a channel's dated, a single video's plain
n = fake_video('n', [720]) | {'upload_date': '20240531'}
os.mkdir('My list')
for name in ('My list/03 - n.mp4', 'My list/2024-05-31 - n.mp4', 'n.mp4'):
    open(name, 'w').close()
v720 = ytd.video_format([720])
in_list, in_channel = ytd.place(n, {'playlist_index': 3}, 'My list', False), ytd.place(n, {'playlist_index': 3}, 'My list', True)
assert not ytd.plan([in_list], ytd.files(v720, 'My list', 12))[0]  # found as 03 - n.mp4: already downloaded
assert ytd.plan([in_list], ytd.files(v720, 'My list', 120))[0]  # 120 listed: 003 - n.mp4, not there
assert not ytd.plan([in_channel], ytd.files(v720, 'My list', 12, channel=True))[0]
assert ytd.plan([in_list], ytd.files(v720, 'My list', 12, subfolder=False))[0]  # 03 - n.mp4 isn't in this folder
assert not ytd.plan([in_list], ytd.files(v720, 'My list', 12, subfolder=False, numbered=False))[0]  # n.mp4 is
assert not ytd.plan([ytd.place(n, {}, None, False)], ytd.files(v720))[0]
assert (in_list['album'], in_list['track_number']) == ('My list', 3) and 'album' not in in_channel
assert ytd.place({'album': 'LP'}, {'playlist_index': 2}, 'My list', False) == {
    'album': 'LP', 'playlist': 'My list', 'playlist_index': 2}  # YouTube's own album wins
mp3 = ytd.AUDIO_FORMATS['MP3 128 kbps']
assert [p['key'] for p in ytd.files(mp3, subs=['en'], split=True)['postprocessors']] == [
    'FFmpegExtractAudio', 'FFmpegEmbedSubtitle', 'FFmpegMetadata', 'FFmpegThumbnailsConvertor', 'EmbedThumbnail',
    'FFmpegSplitChapters'] and len(mp3['postprocessors']) == 1  # AUDIO_FORMATS untouched
assert 'writethumbnail' not in ytd.files(mp3, tags=False) and 'writesubtitles' not in ytd.files(mp3)

# download history: recorded ids, plus the old 'Title [id].ext' names in the folder
ytd.ARCHIVE = ytd.Path(tempfile.mkdtemp(), 'ytd', 'archive.txt')
assert ytd.downloaded() == set()
ytd.remember('x1')
ytd.remember('x2')
open('Old one [abcdefghijk].m4a', 'w').close()
assert ytd.downloaded() == {'x1', 'x2', 'abcdefghijk'}
assert ytd.downloaded('no such folder') == {'x1', 'x2'}  # e.g. a subscription's, on an unplugged drive

# chapter files: each tagged as its own track of an album named after the video
import subprocess
subprocess.run([ytd.BASE_OPTS['ffmpeg_location'], '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc', '-t', '0.1',
                '-metadata', 'title=Whole album', 'ch.m4a'], check=True)
ytd.tag_chapters({'title': 'Live at X', 'chapters': [{'title': 'Intro'}, {'title': 'Song', 'filepath': 'ch.m4a'}]})
assert dict(ytd.mutagen.File('ch.m4a', easy=True)) == {'title': ['Song'], 'tracknumber': ['2'], 'album': ['Live at X']}

channel = [{'_type': 'playlist', 'entries': iter([{'url': 'video'}])},  # a channel's Videos tab, then Shorts
           {'_type': 'playlist', 'entries': [{'url': 'short'}, {'_type': 'playlist', 'entries': None}]}, {'url': 'x'}]
assert [e['url'] for e in ytd.flatten(channel)] == ['video', 'short', 'x']

from collections import Counter
assert ytd.summary(1, [{}], [2048], Counter({'720p': 1}), free=4096) == 'Will download 720p, about 2.00KiB'
assert ytd.summary(3, [{}, {}], [0, 0], Counter({'720p': 1, '480p': 1})) == (
    '1 already downloaded - will be skipped.\nWill download 2 videos: 720p x1, 480p x1, about 0.00B')
assert ytd.summary(2, [], [], Counter()).endswith('Nothing left to download.')
assert ytd.summary(1, [{}], [2048], Counter({'720p': 1}), free=4095).endswith(
    'Not enough disk space: about 4.00KiB needed, 4.00KiB free.')

f = ytd.friendly
assert f("ERROR: [youtube] jNQXAC9IVRw: Private video. Sign in if you've been granted access") == (
    'jNQXAC9IVRw: This video is private.')
assert f('ERROR: [youtube] x-1_: Sign in to confirm you’re not a bot. Use --cookies').startswith('x-1_: YouTube suspects')
assert f('ERROR: [youtube:tab] @chan: Unable to download API page: <urlopen error [Errno 11001] getaddrinfo failed>'
         ) == '@chan: Could not reach YouTube. Check the internet connection and try again.'
assert f("ERROR: 'hello' is not a valid URL. Set --default-search").startswith('That is not a link')
assert f('ERROR: [youtube] aaaaaaaaaaa: This video is unavailable').startswith('aaaaaaaaaaa: This video is unavailable (')
assert f('ERROR: something new') == 'something new'  # unknown: as it was
assert f("ERROR: Unable to download video subtitles for 'ar': HTTP Error 429: Too Many Requests").startswith(
    'Could not get the subtitles')

from yt_dlp.utils import DownloadError


class SubsYDL:
    """YouTube refuses the subtitles: download_one() gets the video without them, then asks for them again.
    Videos come as stubs, fetched fresh; 'n' is saved as n.mp4, which exists."""
    params = {'writesubtitles': True, 'writeautomaticsub': True}
    got = []

    def extract_info(self, url, download, process):
        return {'id': url, 'formats': [], 'playlist_index': 99}

    def process_ie_result(self, raw, download):
        if download and self.params.get('writesubtitles'):
            raise DownloadError("ERROR: Unable to download video subtitles for 'ar': HTTP Error 429")
        if download:
            self.got.append(raw)
        return raw

    def prepare_filename(self, info):
        return f'{info["id"]}.mp4'


logged = []
assert ytd.download_one(SubsYDL(), {'webpage_url': 'x', 'playlist_index': 3}, logged.append)
assert logged == ['  downloading without subtitles...'] and SubsYDL.got == [{'id': 'x', 'formats': [], 'playlist_index': 3}]
assert SubsYDL.params == {'writesubtitles': True, 'writeautomaticsub': True}
assert ytd.download_one(SubsYDL(), {'webpage_url': 'n'}, logged.append) and logged[-1] == '  already downloaded'
assert ytd.stub(in_list) == {'webpage_url': 'http://x/n', 'id': 'n', 'title': 'n', 'playlist': 'My list',
                             'playlist_index': 3, 'album': 'My list', 'track_number': 3}

calls, said = [], []


class FakeYDL:
    """Stands in for YoutubeDL: a 5-video playlist; 'gone' is deleted, the others get "not a bot" twice."""
    def __init__(self, opts): pass
    def __enter__(self): return self
    def __exit__(self, *exc): pass

    def extract_info(self, url, download, process):
        calls.append(url)
        if url == 'playlist?list=PL1':
            return {'_type': 'playlist', 'title': 'P', 'id': 'PL1', 'channel_id': 'UC1',
                    'entries': ({'url': f'v{i}', 'id': f'v{i}'} for i in range(5))}
        if url == '@one':  # a channel with one tab: listed as that tab, under the channel's id
            return {'_type': 'playlist', 'title': 'C - Videos', 'id': 'UC1', 'channel_id': 'UC1', 'entries': [{'url': 'v', 'id': 'v'}]}
        if url == '@tabs':  # Videos and Shorts tabs: listed under the link's name
            return {'_type': 'playlist', 'title': 'C', 'id': '@tabs', 'channel_id': 'UC1',
                    'entries': [{'_type': 'playlist', 'entries': [{'url': 'v', 'id': 'v'}]}]}
        if url == 'gone':
            raise DownloadError('ERROR: [youtube] gone: Video unavailable')
        if url == 'stuck' or calls.count(url) <= 2:
            raise DownloadError(f"ERROR: [youtube] {url}: Sign in to confirm you're not a bot")
        return {'id': url, 'formats': []}


real = ytd.YoutubeDL, ytd.WAITS, ytd.PAUSE, ytd.BASE_OPTS['logger']
ytd.YoutubeDL, ytd.WAITS, ytd.PAUSE = FakeYDL, (0, 0, 0), 0
ytd.BASE_OPTS['logger'] = ytd.Log(lambda text, tag=None: said.append((text, tag)))
pl = 'playlist?list=PL1'
assert ytd.listing(pl, lambda n: n < 3) == ('P', [{'url': f'v{i}', 'id': f'v{i}', 'playlist_index': i + 1}
                                               for i in range(3)], False, None)  # stopped after 3; not a channel
assert ytd.listing(pl, lambda n: True)[2] is True
assert ytd.listing('@one', lambda n: True)[3] == ytd.listing('@tabs', lambda n: True)[3] == 'UC1'
entries = [{'url': u} for u in ('a', 'gone', 'b', 'stuck')] + [{'id': 'c', 'formats': []}]
assert ytd.check(entries, lambda results, n: results) == [{'id': 'a', 'formats': []}, None, {'id': 'b', 'formats': []},
                                                          None, {'id': 'c', 'formats': []}]
assert calls.count('a') == 3 and calls.count('gone') == 1 and calls.count('stuck') == 4  # retried; gone: no retry
assert ('gone: This video is unavailable (removed, or the link is wrong).', 'error') in said
assert ('stuck: YouTube suspects automated downloading. Wait an hour or so, then try again.', 'error') in said
assert any(tag is None and 'one at a time' in text for text, tag in said)  # told once it slowed down
ytd.YoutubeDL, ytd.WAITS, ytd.PAUSE, ytd.BASE_OPTS['logger'] = real

from ytd import gui

now = 1_000_000_000
listed = [{'title': 'Cat video', 'duration': 30, 'url': 'https://www.youtube.com/shorts/s1', 'timestamp': now - 86400},
          {'title': 'Long cat talk', 'duration': 3700, 'url': 'x', 'timestamp': now - 40 * 86400},
          {'title': 'Dog video', 'duration': 400, 'url': 'x'},  # no date: passes the date filter
          {'title': 'Live', 'url': 'x', 'live_status': 'is_upcoming'}]
assert gui.visible(listed, now=now) == [0, 1, 2, 3]
assert gui.visible(listed, 'CAT', now=now) == [0, 1] and gui.visible(listed, 'cat talk', now=now) == [1]
assert gui.visible(listed, shorts=False, now=now) == [1, 2, 3] and gui.visible(listed, shorts=True, now=now) == [0]
assert gui.visible(listed, longer=5, now=now) == [1, 2]  # no length (upcoming) counts as short
assert gui.visible(listed, uploaded=31, now=now) == [0, 2, 3] and gui.visible(listed, first=2, now=now) == [0, 1]
assert [gui.length(e) for e in listed] == ['0:30', '1:01:40', '6:40', 'Upcoming']
assert gui.length({'url': 'https://www.youtube.com/shorts/x'}) == 'Short'
# remembered 720p; a video without it gets the next lower, else the nearest above; nothing remembered: best
assert [gui.nearest([1080, 720, 360], q) for q in (None, 2160, 720, 480, 240)] == [0, 0, 1, 2, 2]
assert gui.on_screen('600x400+100+100') and not gui.on_screen('600x400+-40000+-40000')  # unplugged monitor
links = ['https://www.youtube.com/watch?v=jNQXAC9IVRw', 'https://youtu.be/jNQXAC9IVRw', 'https://m.youtube.com/@x/shorts']
assert all(gui.YOUTUBE_LINK.match(u) for u in links)
assert not any(gui.YOUTUBE_LINK.match(u) for u in ['https://example.com/x', 'see https://youtu.be/a', 'https://youtu.be/a\nb'])
assert gui.queue_line(3 * 1024 ** 2, 1024 ** 2) == 'Queue: 3.00MiB left, about 0:00:03'
assert gui.queue_line(0, None) == 'Queue: 0.00B left' and gui.queue_line(5, 0) == 'Queue: 5.00B left'  # speed unknown
# started by the browser button's ytd: link (encodeURIComponent), by hand, or plainly
zoo = 'https://www.youtube.com/watch?v=jNQXAC9IVRw&t=5'
assert gui.link_from(['ytd:https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DjNQXAC9IVRw%26t%3D5']) == zoo
assert gui.link_from([f'YTD://{zoo}']) == gui.link_from([f' {zoo} ']) == zoo and gui.link_from([]) == ''
# a channel's feed: its latest uploads, newest first
xml = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <title>jawed</title>
 <entry><id>yt:video:new</id><yt:videoId>new1</yt:videoId><title>Second &amp; last</title></entry>
 <entry><id>yt:video:old</id><yt:videoId>jNQXAC9IVRw</yt:videoId><title>Me at the zoo</title></entry>
</feed>'''
assert gui.feed(xml) == [('new1', 'Second & last'), ('jNQXAC9IVRw', 'Me at the zoo')]
# the video card: channel, then length, views, date; many videos: how many, and how long in all
zoo_info = {'channel': 'jawed', 'duration': 19, 'view_count': 436527078, 'upload_date': '20050424'}
assert gui.card([zoo_info]) == 'jawed\n0:19   ·   436,527,078 views   ·   2005-04-24'
assert gui.card([{'uploader': 'x', 'live_status': 'is_live'}]) == 'x\nLive now'
assert gui.card([zoo_info, {'channel': 'jawed', 'duration': 3600}]) == 'jawed\n2 videos, 1:00:19 in all'
assert gui.card([zoo_info, {'channel': 'other'}]).startswith('Various channels\n') and gui.card([{}, {}]) == '\n2 videos'
# a newer app on GitHub: only when its version is higher (numbers, not text: 0.10 > 0.9)
real_urlopen = gui.urllib.request.urlopen
gui.urllib.request.urlopen = lambda url, timeout: io.BytesIO(b'{"tag_name": "v0.10.0", "html_url": "https://x/v0.10.0"}')
assert gui.newer_release('0.9.2') == ('0.10.0', 'https://x/v0.10.0') and gui.newer_release('0.10.0') is None
gui.urllib.request.urlopen = real_urlopen
print('ok')
