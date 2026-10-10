"""Publish a new version: python release.py 1.2.3 "What changed, one line per change"

Sets the version in pyproject.toml, commits it, tags v1.2.3 with the notes and pushes. GitHub Actions
(.github/workflows/release.yml) then tests, builds YouTubeDownloader-Setup.exe and publishes the release;
installed apps see it on their next start.
"""
import re
import subprocess
import sys
from pathlib import Path

version, notes = sys.argv[1:3] if len(sys.argv) == 3 else sys.exit(__doc__)
if not re.fullmatch(r'\d+\.\d+\.\d+', version):
    sys.exit(f'{version!r} is not a version like 1.2.3')
pyproject = Path(__file__).with_name('pyproject.toml')
text = pyproject.read_text(encoding='utf-8')
if f'version = "{version}"' not in text:  # else bumped by hand already
    pyproject.write_text(re.sub(r'(?m)^version = ".*"', f'version = "{version}"', text, count=1), encoding='utf-8')
    subprocess.run(['git', 'commit', '-m', f'Release {version}', 'pyproject.toml'], check=True)
subprocess.run(['git', 'tag', '-a', f'v{version}', '-m', notes], check=True)
subprocess.run(['git', 'push', '--atomic', 'origin', 'HEAD', f'v{version}'], check=True)
print(f'Pushed v{version}. The release appears in a few minutes: https://github.com/bassiony1/youtube-downloader/releases')
