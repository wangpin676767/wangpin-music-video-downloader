"""Exercise the packaged application's real HTTP API and real engine/media files."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

import app
import test_fixtures

ROOT = Path(os.environ.get('VIDEO_DOWNLOADER_TEST_ROOT', Path(__file__).resolve().parent))
BASE = 'http://127.0.0.1:18786'
SOURCE = 'http://127.0.0.1:18785'
OUTPUT = ROOT / 'evidence' / 'output'
TOKEN = ''
RESULTS = []


def record(name, details=''):
    RESULTS.append({'test': name, 'passed': True, 'details': details})
    print('PASS', name, details, flush=True)


def request(path, payload=None, extra=None):
    headers = {'X-App-Token': TOKEN}
    headers.update(extra or {})
    data = json.dumps(payload).encode() if payload is not None else None
    if data:
        headers['Content-Type'] = 'application/json'
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE+path, data, headers), timeout=25) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def state():
    code, result = request('/api/state')
    assert code == 200, result
    return result


def wait_done(timeout=90):
    deadline = time.monotonic()+timeout
    states = []
    while time.monotonic() < deadline:
        s = state()
        status = s['state']['status']
        if not states or states[-1] != status:
            states.append(status)
        if not s['active']:
            return s, states
        time.sleep(0.15)
    raise AssertionError('Download timeout: '+str(state()))


def download(url, quality='best', folder=OUTPUT):
    code, result = request('/api/download', {'url': url, 'folder': str(folder), 'quality': quality})
    assert code == 200, result
    result, states = wait_done()
    assert result['state']['status'] == 'done', result
    return Path(result['state']['path']), states


def decode(path, height=None):
    result = subprocess.run([str(ROOT/'tools/ffmpeg.exe'), '-hide_banner', '-i', str(path),
                             '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stderr
    if height:
        assert re.search(rf'Video:.*\b(?:1920x{height}|1280x{height})\b', result.stderr), result.stderr


def connect():
    global TOKEN
    for _ in range(100):
        try:
            with urllib.request.urlopen(BASE+'/', timeout=2) as r:
                html = r.read().decode()
            TOKEN = re.search(r"const TOKEN='([^']+)'", html).group(1)
            return
        except OSError:
            time.sleep(0.2)
    raise AssertionError('EXE did not start')


def run():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    fixtures = test_fixtures.server()
    threading.Thread(target=fixtures.serve_forever, daemon=True).start()
    proc = subprocess.Popen([str(ROOT/'王品音樂影片下載器.exe'), '--no-browser', '--port', '18786'])
    try:
        connect()
        record('packaged EXE serves Chinese UI without Python', 'port 18786 / 127.0.0.1')
        for name, extra in [('wrong token', {'X-App-Token':'wrong'}), ('cross origin', {'Origin':'https://example.org'}), ('cross site', {'Sec-Fetch-Site':'cross-site'})]:
            assert request('/api/state', extra=extra)[0] == 403
            record(name)
        connection = http.client.HTTPConnection('127.0.0.1', 18786)
        connection.request('GET', '/', headers={'Host':'attacker.example'})
        assert connection.getresponse().status == 403
        connection.close()
        record('DNS rebinding / wrong Host rejected')
        for url in ['', 'file:///etc/passwd', 'javascript:alert(1)', 'https://user:secret@example.com/video', 'https://example.org:bad/video']:
            assert request('/api/download', {'url':url, 'folder':str(OUTPUT)})[0] == 400
        record('empty / invalid / credential URL rejected')
        blocked_output = ROOT/'evidence/unwritable-file'
        blocked_output.write_text('output path must be a directory', encoding='utf8')
        assert request('/api/download', {'url':SOURCE+'/demo.mp4', 'folder':str(blocked_output)})[0] == 400
        record('unwritable output rejected')
        ffmpeg = ROOT/'tools/ffmpeg.exe'
        held = ROOT/'tools/ffmpeg.test-held.exe'
        ffmpeg.rename(held)
        try:
            assert request('/api/download', {'url':SOURCE+'/demo.mp4', 'folder':str(OUTPUT)})[0] == 400
        finally:
            held.rename(ffmpeg)
        record('missing FFmpeg rejected, tool restored')
        path, states = download(SOURCE+'/demo.mp4')
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256((test_fixtures.FIXTURES/'demo.mp4').read_bytes()).digest()
        decode(path)
        record('direct MP4 exact hash and A/V decode', str(path))
        mtime = path.stat().st_mtime_ns
        path2, _ = download(SOURCE+'/demo.mp4')
        assert path2 == path and path.stat().st_mtime_ns == mtime
        record('duplicate download preserves existing file')
        path, _ = download(SOURCE+'/page.html', quality='720')
        decode(path)
        record('HTML video with unknown dimensions / 720 cap accepted', str(path))
        path, states = download(SOURCE+'/hls.m3u8')
        decode(path)
        record('HLS stream download and A/V decode', str(path))
        path, states = download(SOURCE+'/dash.mpd', quality='720', folder=OUTPUT/'720p')
        decode(path, 720)
        record('DASH separate A/V merged and output height 720', str(path))
        path, states = download(SOURCE+'/dash.mpd', quality='best', folder=OUTPUT/'1080p')
        decode(path, 1080)
        record('best quality selects 1080 / A/V decode', str(path))
        for url in [SOURCE+'/empty.html', SOURCE+'/missing.mp4', 'http://127.0.0.1:1/offline.mp4']:
            assert request('/api/download', {'url':url, 'folder':str(OUTPUT)})[0] == 200
            result, _ = wait_done()
            assert result['state']['status'] == 'error', result
        record('empty page / 404 / disconnected network failure without success')
        code, _ = request('/api/download', {'url':SOURCE+'/slow.mp4', 'folder':str(OUTPUT/'cancel')})
        assert code == 200
        assert request('/api/download', {'url':SOURCE+'/demo.mp4', 'folder':str(OUTPUT)})[0] == 400
        for _ in range(100):
            if state()['state']['status'] == 'downloading':
                break
            time.sleep(.15)
        assert state()['state']['status'] == 'downloading'
        assert request('/api/cancel', {})[0] == 200
        result, states = wait_done()
        assert result['state']['status'] == 'cancelled', result
        record('active download progress, second job rejected, cancellation')
        path, _ = download(SOURCE+'/slow.mp4', folder=OUTPUT/'cancel')
        decode(path)
        record('retry after cancellation completes')
        # Exercise rapid terminal->next job boundary repeatedly (worker ownership regression).
        for _ in range(5):
            download(SOURCE+'/demo.mp4')
        record('rapid consecutive jobs complete')
        saved = state()
        assert request('/api/shutdown', {})[0] == 200
        proc.wait(timeout=15)
        proc = subprocess.Popen([str(ROOT/'王品音樂影片下載器.exe'), '--no-browser', '--port', '18786'])
        connect()
        restarted = state()
        assert restarted['settings'] == saved['settings'] and restarted['history'] == saved['history']
        record('settings and history persist across EXE restart')
        duplicate = subprocess.Popen([str(ROOT/'王品音樂影片下載器.exe'), '--no-browser'])
        assert duplicate.wait(timeout=15) == 0
        assert state()['state']['status'] == 'idle'
        record('duplicate EXE launch reuses live instance')
        if request('/api/open-folder', {})[0] != 200:
            raise AssertionError('Open folder failed')
        record('open output folder real Windows call')
        (ROOT/'evidence/integration-results.json').write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        try:
            request('/api/shutdown', {})
            proc.wait(timeout=15)
        except Exception:
            app.stop_process_tree(proc)
        fixtures.shutdown()


if __name__ == '__main__':
    run()
