"""Build dist/YouTubeDownloader-Setup.exe: the app as a wheel inside an Inno Setup installer.

Run from anywhere: python build_installer.py   (needs uv and Inno Setup 6)
"""
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).parent
build = root / 'build'
shutil.rmtree(build, ignore_errors=True)  # setuptools reuses build/ and could ship deleted files
subprocess.run(['uv', 'build', '--wheel', '--out-dir', str(build / 'wheel')], cwd=root, check=True)
wheel = next((build / 'wheel').glob('ytd-*.whl'))
version = wheel.name.split('-')[1]
iscc = shutil.which('ISCC') or Path(os.environ['LOCALAPPDATA'], 'Programs', 'Inno Setup 6', 'ISCC.exe')
subprocess.run([iscc, '/Q', f'/DVersion={version}', f'/DWheel={wheel}', 'installer.iss'], cwd=root, check=True)
shutil.rmtree(build)
shutil.rmtree(root / 'ytd.egg-info', ignore_errors=True)  # setuptools leftover
print('Built', root / 'dist' / 'YouTubeDownloader-Setup.exe')
