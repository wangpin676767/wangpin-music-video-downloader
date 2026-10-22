"""Real HTTP MP4, HTML video, HLS and DASH fixtures for download verification."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import time
import json
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / 'evidence' / 'fixtures'
FFMPEG = ROOT / 'tools' / 'ffmpeg.exe'


def generate():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    def ff(*args):
        subprocess.run([str(FFMPEG), '-hide_banner', '-loglevel', 'error', '-y', *args],
                       check=True, capture_output=True, creationflags=0x08000000, cwd=str(FIXTURES))
    if not (FIXTURES / 'demo.mp4').is_file():
        ff('-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440',
           '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
           '-movflags', '+faststart', str(FIXTURES / 'demo.mp4'))
    if not (FIXTURES / 'hls.m3u8').is_file():
        ff('-i', str(FIXTURES / 'demo.mp4'), '-c', 'copy', '-f', 'hls', '-hls_time', '1',
           '-hls_list_size', '0', str(FIXTURES / 'hls.m3u8'))
    if not (FIXTURES / 'dash.mpd').is_file():
        ff('-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=440',
           '-t', '2', '-filter_complex', '[0:v]split=2[v1][v2];[v2]scale=1280:720[v720]',
           '-map', '[v1]', '-map', '[v720]', '-map', '1:a', '-c:v', 'libx264', '-preset', 'ultrafast',
           '-pix_fmt', 'yuv420p', '-g', '12', '-b:v:0', '2M', '-b:v:1', '1M', '-c:a', 'aac',
           '-f', 'dash', '-adaptation_sets', 'id=0,streams=v id=1,streams=a', str(FIXTURES / 'dash.mpd'))
    (FIXTURES / 'page.html').write_text('<!doctype html><title>本機播放測試</title><video controls src="demo.mp4"></video>', encoding='utf-8')
    (FIXTURES / 'empty.html').write_text('<!doctype html><title>沒有影片</title><p>Empty page</p>', encoding='utf-8')
    (FIXTURES / 'dynamic.html').write_text('''<!doctype html><meta charset=utf-8><title>動態影片測試</title>
      <script>setTimeout(async()=>{const data=await(await fetch('/source')).json();
      const v=document.createElement('video');v.controls=true;v.width=640;v.height=360;
      v.src=data.url;document.body.append(v)},500)</script>''', encoding='utf8')
    (FIXTURES / 'dynamic-slow.html').write_text((FIXTURES / 'dynamic.html').read_text(encoding='utf8').replace("fetch('/source')", "fetch('/source?slow=1')"), encoding='utf8')
    (FIXTURES / 'nested.html').write_text('''<!doctype html><meta charset=utf-8><title>嵌入串流測試</title>
      <script>setTimeout(async()=>{const data=await(await fetch('/source?frame=1')).json();
      const f=document.createElement('iframe');f.src=data.url;f.width=640;f.height=360;
      document.body.append(f);const ad=document.createElement('iframe');ad.src='/ad-frame.html';
      ad.width=800;ad.height=600;document.body.append(ad)},500)</script>''', encoding='utf8')
    (FIXTURES / 'player.html').write_text('''<!doctype html><meta charset=utf-8><title>播放器</title>
      <video width=640 height=360></video><script>setTimeout(async()=>{
      await fetch('/manifest');await fetch('/manifest?variant=1');await fetch('/hls0.ts')},500)</script>''', encoding='utf8')
    (FIXTURES / 'ad-frame.html').write_text('''<!doctype html><meta charset=utf-8><title>廣告</title>
      <video autoplay muted width=800 height=600 src="demo.mp4"></video>''', encoding='utf8')
    (FIXTURES / 'drm.html').write_text('''<!doctype html><meta charset=utf-8><title>DRM fixture</title>
      <script>setTimeout(async()=>{document.dispatchEvent(new Event('encrypted'));
      await fetch('/source');const v=document.createElement('video');v.src=(await(await fetch('/source')).json()).url;
      v.width=640;v.height=360;document.body.append(v)},500)</script>''', encoding='utf8')


class FixtureHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if urlsplit(self.path).path == '/source':
            data = json.dumps({'url': '/player.html' if 'frame=1' in self.path else '/slow.mp4' if 'slow=1' in self.path else '/demo.mp4'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif urlsplit(self.path).path == '/manifest':
            if not self.headers.get('Referer', '').endswith('/player.html') or 'Chrome/' not in self.headers.get('User-Agent', ''):
                self.send_error(403)
                return
            if 'variant=1' in self.path:
                data = (FIXTURES / 'hls.m3u8').read_bytes()
            else:
                data = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180\n/manifest?variant=1\n'
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith('/slow.mp4'):
            data = (FIXTURES / 'demo.mp4').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                for offset in range(0, len(data), 2048):
                    self.wfile.write(data[offset:offset+2048])
                    self.wfile.flush()
                    time.sleep(0.08)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass
        elif self.path == '/broken.mp4':
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', '9999999')
            self.end_headers()
            self.wfile.write(b'incomplete')
            self.close_connection = True
        else:
            super().do_GET()


def server(port=18785):
    generate()
    return ThreadingHTTPServer(('127.0.0.1', port), partial(FixtureHandler, directory=str(FIXTURES)))


if __name__ == '__main__':
    print('Fixtures: http://127.0.0.1:18785/page.html', flush=True)
    server().serve_forever()
