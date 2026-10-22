"""Regression for opening the HTML directly, service disconnects, and real X download.

Run with VIDEO_DOWNLOADER_TEST_ROOT pointing to an isolated packaged EXE/tools root.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

import app
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
TEST_ROOT = Path(os.environ['VIDEO_DOWNLOADER_TEST_ROOT'])
BASE = 'http://127.0.0.1:18788/'
URL = 'https://x.com/KeyGoas79093/status/2110325916887052722'


def run():
    results = []
    proc = None
    token = None
    evidence = TEST_ROOT / 'evidence'
    evidence.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=app.find_brave(), headless=True)
            page = browser.new_page(viewport={'width':1280, 'height':1100})
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url))
            page.goto((ROOT / 'index.html').as_uri())
            assert page.locator('#connectionHelp').is_visible()
            assert '王品音樂影片下載器.exe' in page.locator('#connectionMessage').inner_text()
            assert page.locator('#download').is_disabled()
            page.locator('#url').fill(URL)
            page.locator('#url').press('Enter')
            page.wait_for_timeout(1000)
            assert not any('/api/' in url for url in requests), requests
            assert not errors, errors
            page.screenshot(path=str(evidence/'file-startup.png'), full_page=True)
            results.append({'test':'direct HTML gives startup instructions and makes no API requests', 'passed':True})
            print('PASS direct file startup guard', flush=True)

            proc = subprocess.Popen([str(TEST_ROOT/'王品音樂影片下載器.exe'), '--no-browser', '--port', '18788'], creationflags=app.NO_WINDOW)
            for _ in range(150):
                try:
                    html = urllib.request.urlopen(BASE, timeout=2).read().decode()
                    token = re.search(r"const TOKEN='([^']+)'", html)[1]
                    break
                except OSError:
                    time.sleep(.2)
            assert token, 'Packaged EXE did not start'
            def request(path, data=None):
                body = json.dumps(data).encode() if data is not None else None
                headers = {'X-App-Token':token, 'Content-Type':'application/json'}
                return json.load(urllib.request.urlopen(urllib.request.Request(BASE+path, body, headers), timeout=10))

            page.goto(BASE)
            page.wait_for_function("!document.getElementById('download').disabled")
            assert not page.locator('#connectionHelp').is_visible()
            page.locator('#url').fill(URL)
            page.locator('#folder').fill(str(evidence/'x-download'))
            page.locator('#download').click()
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                snapshot = request('api/state')
                if snapshot['state']['status'] == 'done':
                    break
                assert snapshot['state']['status'] != 'error', snapshot
                time.sleep(.5)
            assert snapshot['state']['status'] == 'done', snapshot
            path = Path(snapshot['state']['path'])
            decoded = subprocess.run([str(TEST_ROOT/'tools/ffmpeg.exe'), '-hide_banner', '-i', str(path),
                                      '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                                     capture_output=True, text=True, encoding='utf8', errors='replace', timeout=90,
                                     creationflags=app.NO_WINDOW)
            assert decoded.returncode == 0, decoded.stderr[-3000:]
            page.wait_for_function("document.getElementById('badge').textContent==='已完成'")
            page.screenshot(path=str(evidence/'x-complete.png'), full_page=True)
            results.append({'test':'packaged EXE UI downloads screenshot X URL; full audio/video decode',
                            'passed':True, 'url':URL, 'path':str(path), 'bytes':path.stat().st_size,
                            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                            'metadata':decoded.stderr[:2500]})
            print('PASS packaged EXE X download and full A/V decode', flush=True)

            request('api/shutdown', {})
            proc.wait(timeout=20)
            page.wait_for_function("!document.getElementById('connectionHelp').hidden", timeout=20000)
            assert page.locator('#download').is_disabled()
            assert page.locator('#open').is_disabled()
            assert page.locator('#quit').is_disabled()
            assert 'Failed to fetch' not in page.locator('body').inner_text()
            results.append({'test':'service disconnect disables unavailable actions and shows Chinese recovery instructions', 'passed':True})
            print('PASS service disconnect guidance', flush=True)
            proc = subprocess.Popen([str(TEST_ROOT/'王品音樂影片下載器.exe'), '--no-browser', '--port', '18788'], creationflags=app.NO_WINDOW)
            for _ in range(150):
                try:
                    urllib.request.urlopen(BASE, timeout=2).close()
                    break
                except OSError:
                    time.sleep(.2)
            page.locator('#reconnect').click()
            page.wait_for_function("!document.getElementById('download').disabled", timeout=15000)
            html = urllib.request.urlopen(BASE, timeout=5).read().decode()
            token = re.search(r"const TOKEN='([^']+)'", html)[1]
            assert not page.locator('#connectionHelp').is_visible()
            assert not errors, errors
            results.append({'test':'reconnect after EXE restart loads new session and restores working UI', 'passed':True})
            print('PASS reconnect after EXE restart', flush=True)
            browser.close()
            (evidence/'startup-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf8')
    finally:
        if proc and proc.poll() is None:
            try:
                headers = {'X-App-Token':token, 'Content-Type':'application/json'}
                urllib.request.urlopen(urllib.request.Request(BASE+'api/shutdown', b'{}', headers), timeout=5).close()
                proc.wait(timeout=20)
            except Exception:
                app.stop_process_tree(proc)


if __name__ == '__main__':
    run()
