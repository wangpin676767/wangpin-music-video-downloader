"""Real packaged EXE discovery regressions; requires an isolated test root."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import test_integration as integration
import test_fixtures


def run():
    assert os.environ.get('VIDEO_DOWNLOADER_TEST_ROOT'), 'Use isolated app root to preserve user settings/history'
    root = integration.ROOT
    integration.RESULTS.clear()
    fixtures = test_fixtures.server()
    threading.Thread(target=fixtures.serve_forever, daemon=True).start()
    proc = subprocess.Popen([str(root / '王品音樂影片下載器.exe'), '--no-browser', '--port', '18786'])
    try:
        integration.connect()
        path, _ = integration.download(integration.SOURCE + '/dynamic.html')
        import hashlib
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256((test_fixtures.FIXTURES/'demo.mp4').read_bytes()).digest()
        integration.decode(path)
        assert integration.state()['state']['title'] == '動態影片測試'
        integration.record('dynamic JavaScript MP4 fallback, original page title, exact hash and A/V decode', str(path))
        mtime = path.stat().st_mtime_ns
        same, _ = integration.download(integration.SOURCE + '/dynamic.html')
        assert same == path and path.stat().st_mtime_ns == mtime
        integration.record('fallback duplicate retains stable filename and file')
        path, _ = integration.download(integration.SOURCE + '/nested.html', quality='720')
        integration.decode(path)
        logs = integration.state()['logs']
        assert any('已找到播放器影片來源' in line for line in logs), logs
        assert '嵌入串流測試' in path.name
        integration.record('nested iframe extensionless HLS with Referer/UA, master over fragment and ad iframe', str(path))
        before = integration.state()['history']
        assert integration.request('/api/download', {'url': integration.SOURCE+'/drm.html', 'folder':str(integration.OUTPUT)})[0] == 200
        result, _ = integration.wait_done()
        assert result['state']['status'] == 'error' and 'DRM' in result['state']['message'], result
        assert result['history'] == before
        integration.record('encrypted-player DRM refused without success/history')
        assert integration.request('/api/download', {'url': integration.SOURCE+'/empty.html', 'folder':str(integration.OUTPUT)})[0] == 200
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = integration.state()
            if '正在偵測' in current['state']['message']:
                break
            time.sleep(.1)
        assert current['active'] and '正在偵測' in current['state']['message'], current
        from test_resolver import descendants, process_snapshot
        server_processes = {proc.pid, json.loads((root/'.running.json').read_text(encoding='utf8'))['pid']}
        observed = set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            snapshot = process_snapshot()
            children = descendants(snapshot, proc.pid) - server_processes
            observed.update(children)
            if any(snapshot[pid][1].lower() == 'brave.exe' for pid in children):
                break
            time.sleep(.1)
        assert any(snapshot[pid][1].lower() == 'brave.exe' for pid in children), 'No resolver browser observed'
        # Give all browser children time to initialize and include them in the check.
        time.sleep(.5)
        observed.update(descendants(process_snapshot(), proc.pid) - server_processes)
        assert integration.request('/api/cancel', {})[0] == 200
        result, _ = integration.wait_done(timeout=15)
        assert result['state']['status'] == 'cancelled' and result['history'] == before, result
        deadline = time.monotonic() + 10
        remaining = observed.intersection(process_snapshot())
        while remaining and time.monotonic() < deadline:
            time.sleep(.1)
            remaining = observed.intersection(process_snapshot())
        assert not remaining, f'Cancelled discovery processes remain: {remaining}'
        integration.record('cancel during active browser discovery; owned browser/driver processes exit without history entry', str(len(observed))+' processes')
        path, _ = integration.download(integration.SOURCE+'/dynamic.html')
        integration.decode(path)
        integration.record('retry immediately after discovery cancellation succeeds')
        before = integration.state()['history']
        folder = integration.OUTPUT/'dynamic-cancel'
        assert integration.request('/api/download', {'url': integration.SOURCE+'/dynamic-slow.html', 'folder':str(folder)})[0] == 200
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = integration.state()
            if current['state']['status'] == 'downloading' and current['state']['bytes'] > 0:
                break
            time.sleep(.1)
        assert current['state']['status'] == 'downloading', current
        assert any('已找到播放器' in line for line in current['logs']), current['logs']
        assert integration.request('/api/cancel', {})[0] == 200
        result, _ = integration.wait_done(timeout=15)
        assert result['state']['status'] == 'cancelled' and result['history'] == before
        path, _ = integration.download(integration.SOURCE+'/dynamic-slow.html', folder=folder)
        integration.decode(path)
        integration.record('cancel during fallback media download and retry complete with A/V decode')
        # Fallback should never run for completed/partial downloads or restricted errors.
        import app
        for error in ['ERROR: DRM unsupported URL', 'ERROR: HTTP Error 403: unsupported URL',
                      'ERROR: login no video', 'ERROR: permission unable to extract']:
            assert not app.DownloadApp.needs_browser([error], [], False)
        assert not app.DownloadApp.needs_browser(['ERROR: unsupported URL'], [], True)
        integration.record('fallback gating preserves DRM/login/storage/partial-download errors')
        (root/'evidence/dynamic-results.json').write_text(json.dumps(integration.RESULTS, ensure_ascii=False, indent=2),encoding='utf8')
    finally:
        try:
            integration.request('/api/shutdown', {})
            proc.wait(timeout=20)
        except Exception:
            import app
            app.stop_process_tree(proc)
        fixtures.shutdown()


if __name__ == '__main__':
    run()
