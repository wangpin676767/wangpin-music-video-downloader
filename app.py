"""Local-only video downloader UI. Engine runs in a cancellable subprocess."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
ASSETS = Path(getattr(sys, '_MEIPASS', ROOT))
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
ACTIVE = {'starting', 'extracting', 'downloading', 'processing', 'cancelling'}
MODES = {'video', 'audio'}
QUALITIES = {'best', '1080', '720', '480'}
MAX_BATCH = 50
AD_URL = 'https://www.wangpin67.com/'


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def format_selector(quality):
    limit = '' if quality == 'best' else f'[height<=?{quality}]'
    # Unknown dimensions are common on direct media URLs; do not reject them.
    return f'bv{limit}+ba/b{limit}'


def find_brave():
    candidates = [Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')),
                  Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')),
                  Path(os.environ.get('LOCALAPPDATA', 'C:/Users/Default/AppData/Local'))]
    for base in candidates:
        exe = base / 'BraveSoftware/Brave-Browser/Application/brave.exe'
        if exe.is_file():
            return str(exe)
    return shutil.which('brave')


def open_brave(url):
    brave = find_brave()
    if not brave:
        raise RuntimeError('找不到 Brave。請安裝 Brave 後重新啟動。')
    # No profile switches: reuse Brave's existing signed-in profile.
    subprocess.Popen([brave, url], creationflags=NO_WINDOW)


def stop_process_tree(proc):
    if proc and proc.poll() is None:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           capture_output=True, creationflags=NO_WINDOW, timeout=15)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


class DownloadApp:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.tools = self.root / 'tools'
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        raw = read_json(self.root / 'settings.json', {})
        raw = raw if isinstance(raw, dict) else {}
        self.settings = {
            'mode': raw.get('mode') if isinstance(raw.get('mode'), str) and raw.get('mode') in MODES else 'video',
            'folder': raw.get('folder') if isinstance(raw.get('folder'), str) and raw['folder'] else str(self.root / '影片'),
            'quality': raw.get('quality') if isinstance(raw.get('quality'), str) and raw.get('quality') in QUALITIES else 'best',
        }
        history = read_json(self.root / 'history.json', [])
        self.history = [x for x in history if isinstance(x, dict) and isinstance(x.get('path'), str) and isinstance(x.get('title'), str)][:30] if isinstance(history, list) else []
        self.state = self.idle_state()
        self.logs = collections.deque(maxlen=80)
        self.proc = None
        self.cancel_event = threading.Event()
        self.job_id = 0
        self.queue = []
        self.batch_running = False
        self.server = None
        self.authority = ''
        self.exiting = False

    @staticmethod
    def idle_state():
        return {'status': 'idle', 'title': '', 'percent': None, 'speed': None,
                'eta': None, 'message': '貼上網址，開始下載。', 'path': '', 'bytes': 0, 'batch': None}

    def snapshot(self):
        with self.lock:
            return {'state': dict(self.state), 'settings': dict(self.settings),
                    'history': list(self.history), 'logs': list(self.logs),
                    'queue': [dict(x) for x in self.queue],
                    'engine': 'yt-dlp + Brave 播放器偵測 + FFmpeg', 'active': self.busy()}

    def busy(self):
        return self.batch_running or self.state['status'] in ACTIVE

    @staticmethod
    def check_url(url):
        if not isinstance(url, str):
            raise ValueError('請輸入完整的 http:// 或 https:// 網址。')
        url = url.strip()
        try:
            parts = urlsplit(url)
            valid = parts.scheme in {'http', 'https'} and bool(parts.hostname) and not parts.username and not parts.password
            parts.port
        except ValueError:
            valid = False
        if not valid or any(ord(c) < 32 for c in url) or len(url) > 8192:
            raise ValueError('請輸入完整的 http:// 或 https:// 網址；不接受帳密放在網址內。'
                             + (f'（{url[:80]}）' if url else ''))
        return url

    def start(self, payload):
        raw = payload.get('urls')
        if raw is None:
            raw = [payload.get('url', '')]
        if not isinstance(raw, list) or not raw:
            raise ValueError('請至少輸入一個影片網址。')
        urls = []
        for item in raw:
            url = self.check_url(item)
            if url not in urls:
                urls.append(url)
        if len(urls) > MAX_BATCH:
            raise ValueError(f'一次最多 {MAX_BATCH} 個網址。')
        mode = payload.get('mode', 'video')
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError('請選擇下載影片或音樂。')
        quality = payload.get('quality', 'best')
        if not isinstance(quality, str) or quality not in QUALITIES:
            raise ValueError('請選擇有效的畫質。')
        folder = payload.get('folder', self.settings['folder'])
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError('請輸入儲存資料夾的完整路徑。')
        output = Path(folder.strip()).expanduser()
        if not output.is_absolute():
            raise ValueError('儲存位置必須是完整路徑，例如 C:\\影片。')
        with self.lock:
            if self.exiting:
                raise ValueError('App 正在關閉。')
            if self.busy():
                raise ValueError('目前已有下載工作，請等待完成或先取消。')
            for tool in ['yt-dlp.exe', 'ffmpeg.exe', 'deno.exe']:
                if not (self.tools / tool).is_file():
                    raise ValueError(f'缺少下載工具 {tool}，請重新執行「建置.ps1」。')
            try:
                output.mkdir(parents=True, exist_ok=True)
                probe = output / f'.write-test-{secrets.token_hex(8)}'
                with probe.open('xb') as f:
                    f.write(b'')
                probe.unlink()
                new_settings = {'folder': str(output), 'quality': quality, 'mode': mode}
                write_json(self.root / 'settings.json', new_settings)
            except OSError as exc:
                raise ValueError(f'無法寫入儲存位置，請改選資料夾。{exc}') from exc
            self.settings = new_settings
            self.state = self.idle_state()
            self.state.update(status='starting', message='準備下載…')
            self.logs.clear()
            self.cancel_event = threading.Event()
            self.job_id += 1
            job = self.job_id
            self.queue = [{'url': u, 'status': 'waiting', 'title': '', 'message': '等待中', 'path': ''} for u in urls]
            self.batch_running = True
            threading.Thread(target=self.run_batch, args=(job, urls, quality, output, self.cancel_event, mode), daemon=True).start()

    def run_batch(self, job, urls, quality, output, cancel, mode="video"):
        total = len(urls)
        try:
            for index, url in enumerate(urls):
                with self.lock:
                    if cancel.is_set():
                        break
                    self.queue[index].update(status='running', message='下載中')
                    if index:
                        self.state = self.idle_state()
                        self.state.update(status='starting', message='準備下載…')
                        self.logs.append(f'── 第 {index + 1}/{total} 支 ──')
                    self.state['batch'] = [index + 1, total]
                self.worker(job, url, quality, output, cancel, mode)
                with self.lock:
                    self.queue[index].update(status=self.state['status'], title=self.state['title'],
                                             message=self.state['message'], path=self.state['path'])
        finally:
            with self.lock:
                for item in self.queue:
                    if item['status'] in {'waiting', 'running'}:
                        item.update(status='cancelled', message='已取消')
                if total > 1:
                    done = sum(x['status'] == 'done' for x in self.queue)
                    failed = sum(x['status'] == 'error' for x in self.queue)
                    if cancel.is_set():
                        status, message = 'cancelled', f'已取消。完成 {done}/{total} 支。'
                    elif done == total:
                        status, message = 'done', f'全部完成，共 {total} 個檔案已存好。'
                    else:
                        status, message = 'error', f'完成 {done}/{total} 支，{failed} 支失敗，請看下方清單。'
                    self.state.update(status=status, message=message, speed=None, eta=None, batch=None,
                                      percent=done / total * 100)
                else:
                    self.state['batch'] = None
                self.batch_running = False

    def command(self, url, quality, output, media=None, mode="video"):
        command = [str(self.tools / 'yt-dlp.exe'), '--ignore-config', '--no-playlist',
                '--extractor-args', 'generic:impersonate',
                '--no-overwrites', '--windows-filenames', '--trim-filenames', '160',
                '--no-simulate', '--newline', '--no-colors', '--progress',
                '--progress-delta', '0.5', '--socket-timeout', '20', '--retries', '3',
                '--fragment-retries', '3', '--concurrent-fragments', '4', '--abort-on-unavailable-fragments',
                '--match-filters', '!is_live',
                '--ffmpeg-location', str(self.tools / 'ffmpeg.exe'),
                '--js-runtimes', f'deno:{self.tools / "deno.exe"}',
                '-P', str(output), '-o', '%(title).120B [%(id)s].%(ext)s',
                '--progress-template', 'download:__PROGRESS__%(progress)j',
                '--print', 'before_dl:__TITLE__%(title)j',
                '--print', 'after_move:__FILE__%(filepath)j']
        if mode == 'audio':
            command += ['-f', 'ba/b', '--extract-audio', '--audio-format', 'mp3', '--audio-quality', '0']
        else:
            command += ['-f', format_selector(quality), '-S', 'res,vcodec:h264,ext:mp4:m4a', '--merge-output-format', 'mp4']
        if media:
            for key, flag in [('referer', '--referer'), ('user_agent', '--user-agent')]:
                value = media.get(key)
                if value and not any(ord(c) < 32 for c in value):
                    command += [flag, value]
            origin = media.get('origin')
            if origin and not any(ord(c) < 32 for c in origin):
                command += ['--add-headers', 'Origin:' + origin]
            # Keep the original page identity, even when CDN tokens change.
            command += ['--replace-in-metadata', 'title', '(?s)^.*$',
                        str(media.get('title') or '影片').replace('\\', '\\\\'),
                        '--replace-in-metadata', 'id', '(?s)^.*$', media['id']]
        return command + ['--', url]

    @staticmethod
    def needs_browser(errors, paths, downloaded):
        error = ' '.join(errors).lower()
        return (not paths and not downloaded and not any(x in error for x in
                ['drm', '403', '404', 'sign in', 'login', 'private', 'permission', 'disk', 'space'])
                and any(x in error for x in ['unsupported url', 'no video', 'unable to extract',
                                            'no supported media', "'ascii' codec", "'utf-8' codec"]))

    def discover_media(self, url, cancel):
        if not find_brave():
            raise RuntimeError('需要 Brave 才能偵測動態播放器，請安裝後再試。')
        command = ([sys.executable] if getattr(sys, 'frozen', False)
                   else [sys.executable, str(Path(__file__).resolve())])
        with self.lock:
            if cancel.is_set():
                return None
            self.state.update(status='extracting', message='正在偵測網頁播放器…（最多約 35 秒）')
            self.logs.append('一般解析無法找到影片，改用 Brave 偵測動態播放器。')
            proc = subprocess.Popen(command + ['--resolve-media', url], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                    text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
            self.proc = proc
        try:
            stdout, _ = proc.communicate(timeout=65)
            if cancel.is_set():
                return None
            result = json.loads(stdout)
            if proc.returncode or 'error' in result:
                raise RuntimeError(result.get('error') or '播放器偵測失敗。')
            media = result['media']
            parts = urlsplit(media['url'])
            if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
                raise RuntimeError('播放器沒有提供有效的影片網址。')
            media['id'] = hashlib.sha256(url.encode()).hexdigest()[:12]
            return media
        except subprocess.TimeoutExpired:
            raise RuntimeError('播放器偵測逾時，請確認網頁能播放影片後再試。') from None
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('播放器偵測失敗，請確認 Brave 已安裝且影片可公開播放。') from None
        finally:
            stop_process_tree(proc)
            with self.lock:
                if self.proc is proc:
                    self.proc = None

    def run_engine(self, command, cancel):
        paths, errors = [], []
        downloaded = False
        with self.lock:
            if cancel.is_set():
                return -1, paths, errors, downloaded
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
                                    errors='replace', creationflags=NO_WINDOW)
            self.proc = proc
            self.state.update(status='extracting', message='解析影片網址…')
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                with self.lock:
                    if cancel.is_set():
                        continue
                    if line.startswith('__PROGRESS__'):
                        try:
                            progress = json.loads(line[len('__PROGRESS__'):])
                            amount = progress.get('downloaded_bytes') or 0
                            downloaded = downloaded or amount > 0
                            total = progress.get('total_bytes') or progress.get('total_bytes_estimate')
                            percent = min(100, amount / total * 100) if total else None
                            finished = progress.get('status') == 'finished'
                            self.state.update(status='processing' if finished else 'downloading',
                                              message='處理影片 / 合併音軌…' if finished else '正在下載影片…',
                                              percent=percent, bytes=amount,
                                              speed=progress.get('speed'), eta=progress.get('eta'))
                        except (ValueError, TypeError, ZeroDivisionError):
                            pass
                    elif line.startswith('__TITLE__'):
                        try:
                            self.state['title'] = json.loads(line[len('__TITLE__'):])
                        except ValueError:
                            pass
                    elif line.startswith('__FILE__'):
                        try:
                            paths.append(Path(json.loads(line[len('__FILE__'):])))
                        except (ValueError, TypeError):
                            pass
                    else:
                        safe = re.sub(r'https?://\S+', '[來源網址]', line)
                        self.logs.append(safe[:1500])
                        if line.startswith('ERROR:'):
                            errors.append(safe)
                        if '[Merger]' in line or '[Fixup' in line:
                            self.state.update(status='processing', message='合併音軌與影片…')
            return proc.wait(), paths, errors, downloaded
        finally:
            stop_process_tree(proc)
            proc.stdout.close()
            with self.lock:
                if self.proc is proc:
                    self.proc = None

    def worker(self, job, url, quality, output, cancel, mode="video"):
        try:
            code, paths, errors, downloaded = self.run_engine(self.command(url, quality, output, mode=mode), cancel)
            if code and not cancel.is_set() and self.needs_browser(errors, paths, downloaded):
                media = self.discover_media(url, cancel)
                if media and not cancel.is_set():
                    with self.lock:
                        self.logs.append('已找到播放器影片來源，開始下載。')
                    code, paths, errors, _ = self.run_engine(
                        self.command(media['url'], quality, output, media, mode=mode), cancel)
            with self.lock:
                if cancel.is_set():
                    self.state.update(status='cancelled', message='已取消。未完成的 .part 檔保留供重試續傳。', speed=None, eta=None)
                elif code == 0 and paths and all(p.is_file() and p.stat().st_size > 0 for p in paths):
                    path = paths[0].resolve()
                    record = {'title': self.state['title'] or path.stem, 'path': str(path),
                              'time': time.strftime('%Y-%m-%d %H:%M'), 'size': path.stat().st_size, 'mode': mode}
                    self.history = [record] + [x for x in self.history if x['path'] != str(path)]
                    self.history = self.history[:30]
                    self.state.update(status='done', message='下載完成，檔案已存好。', percent=100,
                                      speed=None, eta=None, path=str(path))
                    try:
                        write_json(self.root / 'history.json', self.history)
                    except OSError as exc:
                        self.logs.append(f'影片已下載，但歷史紀錄無法儲存：{exc}')
                else:
                    self.state.update(status='error', message=self.friendly_error(errors), speed=None, eta=None)
        except Exception as exc:
            with self.lock:
                if cancel.is_set():
                    self.state.update(status='cancelled', message='已取消。')
                else:
                    message = str(exc) if isinstance(exc, RuntimeError) else '下載失敗，請查看下方詳細資訊或更換網址。'
                    safe = re.sub(r'https?://\S+', '[來源網址]', message)
                    self.state.update(status='error', message=safe, speed=None, eta=None)
                    self.logs.append(safe[:1500])

    @staticmethod
    def friendly_error(errors):
        error = ' '.join(errors).lower()
        if 'drm' in error:
            return '這部影片受到 DRM 保護，無法下載。'
        if any(x in error for x in ['sign in', 'login', 'private', '403', 'cookies', 'bot']):
            return '網站要求登入或拒絕下載。請改用可公開下載的影片網址。'
        if 'unsupported url' in error or 'no video' in error:
            return '找不到可下載的影片。請確認網頁可播放，或貼 MP4 / M3U8 / MPD 的直接網址。'
        if '404' in error:
            return '網址不存在或影片已被移除，請檢查網址。'
        if 'permission' in error or 'disk' in error or 'space' in error:
            return '無法寫入影片，請檢查資料夾權限與磁碟空間。'
        return '下載未完成。請檢查網路或更換影片網址；詳細資訊在下方。'

    def cancel(self):
        with self.lock:
            if not self.busy():
                return
            self.cancel_event.set()
            self.state.update(status='cancelling', message='正在取消下載…')
            proc = self.proc
        stop_process_tree(proc)

    def open_folder(self, index=None):
        with self.lock:
            if index is None:
                folder = Path(self.settings['folder'])
                folder.mkdir(parents=True, exist_ok=True)
            else:
                if not isinstance(index, int) or index < 0 or index >= len(self.history):
                    raise ValueError('找不到這筆下載紀錄。')
                folder = Path(self.history[index]['path']).parent
            if not folder.is_dir():
                raise ValueError('資料夾已不存在。')
        os.startfile(str(folder))

    def delete_history(self, index=None, path=None):
        # Only removes the record; the downloaded video file is kept.
        with self.lock:
            if index is None:
                self.history = []
            else:
                if not isinstance(index, int) or index < 0 or index >= len(self.history)                         or (path is not None and self.history[index]['path'] != path):
                    raise ValueError('找不到這筆下載紀錄，請重新整理後再試。')
                del self.history[index]
            write_json(self.root / 'history.json', self.history)

    def shutdown(self):
        with self.lock:
            self.exiting = True
        self.cancel()
        if self.server:
            self.server.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = 'VideoDownloader'

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def reply(self, code, data, content_type='application/json; charset=utf-8'):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', f"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-src {AD_URL.rstrip('/')}; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self, api=False):
        if self.headers.get('Host') != self.app.authority:
            self.reply(403, {'error': '拒絕非本機請求。'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{self.app.authority}':
            self.reply(403, {'error': '拒絕跨來源請求。'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.reply(403, {'error': '拒絕跨來源請求。'})
            return False
        if api and not secrets.compare_digest(self.headers.get('X-App-Token', ''), self.app.token):
            self.reply(403, {'error': '連線驗證失敗，請重新開啟 app。'})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.authorized(api=path.startswith('/api/')):
            return
        if path == '/':
            html = (ASSETS / 'index.html').read_text(encoding='utf-8')
            self.reply(200, html.replace('__APP_TOKEN__', self.app.token).encode('utf-8'), 'text/html; charset=utf-8')
        elif path == '/api/state':
            self.reply(200, self.app.snapshot())
        elif path == '/favicon.ico':
            self.reply(204, b'', 'image/x-icon')
        else:
            self.reply(404, {'error': '找不到頁面。'})

    def do_POST(self):
        if not self.authorized(api=True):
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 512 * 1024:
                raise ValueError('請求格式不正確。')
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('請求格式不正確。')
            if self.path == '/api/download':
                self.app.start(payload)
            elif self.path == '/api/cancel':
                self.app.cancel()
            elif self.path == '/api/open-folder':
                self.app.open_folder(payload.get('index'))
            elif self.path == '/api/delete-history':
                self.app.delete_history(payload.get('index'), payload.get('path'))
            elif self.path == '/api/clear-history':
                self.app.delete_history()
            elif self.path == '/api/shutdown':
                threading.Thread(target=self.app.shutdown, daemon=True).start()
            else:
                self.reply(404, {'error': '找不到功能。'})
                return
            self.reply(200, {'ok': True})
        except (ValueError, TypeError, OSError) as exc:
            self.reply(400, {'error': str(exc)})
        except Exception:
            self.reply(500, {'error': '操作失敗，請重新開啟 app。'})


def create_server(app, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = app
    app.server = server
    app.authority = f'127.0.0.1:{server.server_port}'
    return server


def notice(message):
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, '王品音樂影片下載器', 0x40)
    else:
        print(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resolve-media', metavar='URL')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if args.resolve_media:
        # A separate subprocess makes discovery cancellable, including its browser tree.
        from media_resolver import resolve_media
        try:
            media = resolve_media(args.resolve_media, find_brave())
            print(json.dumps({'media': media}, ensure_ascii=True), flush=True)
        except Exception as exc:
            message = str(exc) if isinstance(exc, RuntimeError) else '播放器偵測失敗，請確認 Brave 已安裝且網頁可播放。'
            message = re.sub(r'https?://\S+', '[來源網址]', message)
            print(json.dumps({'error': message}, ensure_ascii=True), flush=True)
            raise SystemExit(1)
        return
    mutex = None
    if os.name == 'nt':
        import ctypes
        import hashlib
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        name = 'Local\\VideoDownloader-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
        mutex = kernel.CreateMutexW(None, False, name)
        if ctypes.get_last_error() == 183:
            info = read_json(ROOT / '.running.json', {})
            url = info.get('url', '') if isinstance(info, dict) else ''
            if re.fullmatch(r'http://127\.0\.0\.1:\d+/', url):
                try:
                    if not args.no_browser:
                        open_brave(url)
                    return
                except Exception as exc:
                    notice(str(exc))
                    return
            notice('App 已啟動，請查看 Brave 中的「王品音樂影片下載器」分頁。')
            return
    app = None
    try:
        app = DownloadApp()
        server = create_server(app, args.port)
        url = f'http://{app.authority}/'
        write_json(ROOT / '.running.json', {'url': url, 'pid': os.getpid()})
        if not args.no_browser:
            open_brave(url)
        server.serve_forever(poll_interval=0.25)
    except Exception as exc:
        notice(f'無法啟動：{exc}')
    finally:
        if app:
            app.cancel()
            if app.server:
                app.server.server_close()
        try:
            (ROOT / '.running.json').unlink(missing_ok=True)
        except OSError:
            pass
        if mutex:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
