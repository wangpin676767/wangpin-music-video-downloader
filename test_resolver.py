"""Independent real-browser resolver selection, DRM and cleanup assertions."""
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
import threading
import time
from urllib.parse import urlsplit

import app
from media_resolver import _kind, resolve_media
import test_fixtures


class ResolverFixtureHandler(test_fixtures.FixtureHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        fixtures = {
            '/sample.html': ('text/html', b'<title>protected HLS</title><video width="640" height="360"></video><script>setTimeout(()=>fetch("/protected.m3u8"),100)</script>'),
            '/protected.m3u8': ('application/vnd.apple.mpegurl', b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://example.invalid/key"\n#EXTINF:3,\n/demo.mp4\n#EXT-X-ENDLIST\n'),
            '/dash-protected.html': ('text/html', b'<title>protected DASH</title><video width="640" height="360"></video><script>setTimeout(()=>fetch("/protected.mpd"),100)</script>'),
            '/protected.mpd': ('application/dash+xml', b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet><ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/></AdaptationSet></Period></MPD>'),
        }
        if path not in fixtures:
            return super().do_GET()
        mime, body = fixtures[path]
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def process_snapshot():
    """Windows Toolhelp PID/parent-PID snapshot, without a new dependency."""
    if os.name != 'nt':
        return {}
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    assert handle != ctypes.c_void_p(-1).value, 'Toolhelp snapshot failed'
    result = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while present:
            result[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            present = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return result


def descendants(snapshot, parent):
    pending = {parent}
    found = set()
    while pending:
        current = pending.pop()
        children = {pid for pid, item in snapshot.items() if item[0] == current} - found
        found.update(children)
        pending.update(children)
    return found


def run():
    # This validation never starts DownloadApp or touches settings/history.
    test_fixtures.generate()
    browser = app.find_brave()
    assert browser, 'Brave required for real resolver checks'
    assert _kind('https://example.invalid/fragment.mp4', 'video/mp2t') is None
    assert _kind('https://example.invalid/error.mp4', 'text/html') is None
    assert _kind('https://example.invalid/error.mp4', 'application/json') is None
    assert _kind('https://example.invalid/segment_001.mp4', 'video/mp4') is None
    assert _kind('https://example.invalid/init-stream0.mp4', 'video/mp4') is None
    assert _kind('https://example.invalid/initial-adventure.mp4', 'video/mp4') == 'file'
    assert _kind('https://example.invalid/video.m3u8', 'text/plain') == 'hls'
    assert _kind('https://example.invalid/video.mpd', 'application/xml') == 'dash'
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(ResolverFixtureHandler, directory=str(test_fixtures.FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    before = process_snapshot()
    observed = set()
    stop = threading.Event()
    def watch():
        while not stop.is_set():
            observed.update(descendants(process_snapshot(), os.getpid()) - set(before))
            stop.wait(.05)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    results = []
    try:
        media = resolve_media(base+'/nested.html', browser, timeout=15)
        parsed = urlsplit(media['url'])
        assert media['kind'] == 'hls' and parsed.path == '/manifest' and not parsed.query, media['kind']
        assert media['referer'] == base+'/player.html'
        assert 'Chrome/' in media['user_agent']
        results.append('extensionless master selected over variant, fragments and larger advertising iframe')
        for page in ['/sample.html', '/dash-protected.html']:
            try:
                resolve_media(base+page, browser, timeout=10)
            except RuntimeError as exc:
                assert 'DRM' in str(exc), str(exc)
            else:
                raise AssertionError('protected manifest accepted')
        results.append('HLS SAMPLE-AES and DASH ContentProtection refused')
    finally:
        stop.set()
        watcher.join(timeout=3)
        server.shutdown()
        server.server_close()
    if os.name == 'nt':
        assert observed, 'No browser descendants observed; cleanup evidence invalid'
        deadline = time.monotonic()+5
        remaining = observed.intersection(process_snapshot())
        while remaining and time.monotonic() < deadline:
            time.sleep(.1)
            remaining = observed.intersection(process_snapshot())
        assert not remaining, f'Discovery descendants leaked: {sorted(remaining)}'
        results.append('observed resolver browser/driver descendants exit after success and DRM failures')
    print(json.dumps({'passed': results, 'observed_process_count': len(observed)}, ensure_ascii=True))


if __name__ == '__main__':
    run()
