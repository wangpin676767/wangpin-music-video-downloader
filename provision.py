"""Fetch official yt-dlp/Deno, verify published hashes where available, stage FFmpeg."""
import hashlib
import json
import re
from pathlib import Path
import shutil
import urllib.request
import zipfile
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / 'tools'
TOOLS.mkdir(exist_ok=True)

def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'VideoDownloader-build'})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()

def release(repo):
    data = json.loads(fetch(f'https://api.github.com/repos/{repo}/releases/latest'))
    return data['tag_name'], {asset['name']: asset['browser_download_url'] for asset in data['assets']}

records = {}
tag, assets = release('yt-dlp/yt-dlp')
engine = fetch(assets['yt-dlp.exe'])
digest = hashlib.sha256(engine).hexdigest()
checksums = fetch(assets['SHA2-256SUMS']).decode()
assert any(line.split()[0] == digest and line.split()[-1].lstrip('*') == 'yt-dlp.exe' for line in checksums.splitlines()), 'yt-dlp hash mismatch'
(TOOLS / 'yt-dlp.exe').write_bytes(engine)
records['yt-dlp'] = {'version': tag, 'source': assets['yt-dlp.exe'], 'sha256': digest}
print('yt-dlp', tag, flush=True)

tag, assets = release('denoland/deno')
name = 'deno-x86_64-pc-windows-msvc.zip'
archive = fetch(assets[name])
checksum_name = name + '.sha256sum'
if checksum_name in assets:
    checksum_text = fetch(assets[checksum_name]).decode()
    expected = re.search(r'\b[0-9A-Fa-f]{64}\b', checksum_text).group(0).lower()
    assert hashlib.sha256(archive).hexdigest() == expected, 'Deno hash mismatch'
import io
with zipfile.ZipFile(io.BytesIO(archive)) as z:
    (TOOLS / 'deno.exe').write_bytes(z.read('deno.exe'))
records['deno'] = {'version': tag, 'source': assets[name], 'sha256': hashlib.sha256((TOOLS / 'deno.exe').read_bytes()).hexdigest()}
print('Deno', tag, flush=True)

ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
shutil.copy2(ffmpeg, TOOLS / 'ffmpeg.exe')
records['ffmpeg'] = {'source': 'https://pypi.org/project/imageio-ffmpeg/', 'package': imageio_ffmpeg.__version__,
                     'sha256': hashlib.sha256((TOOLS / 'ffmpeg.exe').read_bytes()).hexdigest()}
(TOOLS / 'sources.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
print('FFmpeg ready', flush=True)
