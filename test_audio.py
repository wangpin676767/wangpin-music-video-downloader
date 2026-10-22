"""Isolated packaged UI, batch MP3 extraction and video regression test."""
import functools
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.request

import app
from playwright.sync_api import sync_playwright


def run():
    source = Path(__file__).resolve().parent
    root = Path(os.environ['VIDEO_DOWNLOADER_TEST_ROOT']).resolve()
    assert root != source and not root.exists(), 'Use a fresh isolated test folder'
    root.mkdir(parents=True)
    (root / 'tools').mkdir()
    for tool in source.joinpath('tools').glob('*.exe'):
        os.link(tool, root / 'tools' / tool.name)
    exe = source / '王品音樂影片下載器.exe'
    shutil.copy2(exe, root / exe.name)
    fixtures = root / 'fixtures'
    fixtures.mkdir()
    ffmpeg = root / 'tools' / 'ffmpeg.exe'
    subprocess.run([str(ffmpeg), '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=320x180:r=24', '-f', 'lavfi', '-i',
                    'sine=frequency=440', '-t', '2', '-c:v', 'libx264',
                    '-c:a', 'aac', str(fixtures / 'demo.mp4')], check=True)
    shutil.copy2(fixtures / 'demo.mp4', fixtures / 'second.mp4')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(fixtures)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = subprocess.Popen([str(root / exe.name), '--no-browser'], creationflags=app.NO_WINDOW)
    base = token = None
    try:
        for _ in range(200):
            try:
                base = json.loads((root / '.running.json').read_text())['url']
                html = urllib.request.urlopen(base, timeout=2).read().decode()
                token = re.search(r"const TOKEN='([^']+)'", html)[1]
                break
            except (OSError, ValueError, TypeError):
                time.sleep(.2)
        assert token, 'EXE startup failed'

        def api(path, data=None):
            request = urllib.request.Request(base.rstrip('/') + path,
                data=json.dumps(data).encode() if data is not None else None,
                headers={'X-App-Token': token, 'Content-Type': 'application/json'})
            return json.load(urllib.request.urlopen(request, timeout=10))

        def wait_done():
            for _ in range(400):
                state = api('/api/state')
                if not state['active']:
                    assert state['state']['status'] == 'done', state
                    return state
                time.sleep(.2)
            raise AssertionError('Download timed out')

        url = f'http://127.0.0.1:{server.server_port}'
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=app.find_brave(), headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('https://www.wangpin67.com/**', lambda route: route.abort())
            page.goto(base)
            page.wait_for_function("!document.querySelector('#download').disabled")
            assert page.title() == '王品音樂影片下載器'
            assert page.locator('#mode').input_value() == 'video'
            page.select_option('#mode', 'audio')
            assert page.locator('#quality').is_disabled()
            page.fill('#folder', str(root / 'output'))
            page.fill('#url', url + '/demo.mp4\n' + url + '/second.mp4')
            page.click('#download')
            state = wait_done()
            assert len(state['history']) == 2
            for record in state['history']:
                path = Path(record['path'])
                assert path.suffix == '.mp3' and record['mode'] == 'audio'
                decoded = subprocess.run([str(ffmpeg), '-hide_banner', '-i', str(path),
                    '-f', 'null', '-'], capture_output=True, text=True, encoding="utf-8", errors="replace")
                assert decoded.returncode == 0 and 'Audio:' in decoded.stderr and 'Video:' not in decoded.stderr
            page.reload()
            page.wait_for_function("document.querySelector('#mode').value === 'audio'")
            assert page.locator('#quality').is_disabled()
            page.select_option('#mode', 'video')
            assert page.locator('#quality').is_enabled()
            page.fill('#url', url + '/demo.mp4')
            page.click('#download')
            state = wait_done()
            video = Path(state['history'][0]['path'])
            decoded = subprocess.run([str(ffmpeg), '-hide_banner', '-i', str(video),
                '-f', 'null', '-'], capture_output=True, text=True, encoding="utf-8", errors="replace")
            assert decoded.returncode == 0 and 'Video:' in decoded.stderr and 'Audio:' in decoded.stderr
            assert not errors, errors
            page.screenshot(path=str(root / 'verified-ui.png'), full_page=True)
            browser.close()
        print('PASS packaged UI; batch MP3; decoded audio/video; remembered mode; no JS errors')
    finally:
        if token:
            try:
                api('/api/shutdown', {})
            except OSError:
                pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.stop_process_tree(proc)
        server.shutdown()


if __name__ == '__main__':
    run()
