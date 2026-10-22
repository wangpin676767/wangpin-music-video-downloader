"""Discover public media requests in a disposable Brave session.

No existing browser profile, cookies or authorization headers are read. The
caller runs this module in a subprocess so discovery can be cancelled.
"""
from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


_AD_FRAME = re.compile(r'(?:doubleclick|googlesyndication|adservice|popads|adsystem|vast|vpaid|(?:^|[/_.-])ads?(?:[/_.-]|$))', re.I)
_INIT = """(() => {
  window.__downloadEncrypted = false;
  document.addEventListener('encrypted', () => { window.__downloadEncrypted = true; }, true);
})()"""


def _kind(url, mime=''):
    try:
        parts = urlsplit(url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname:
            return None
        path = parts.path.lower()
    except ValueError:
        return None
    mime = mime.split(';', 1)[0].strip().lower()
    # A misleading extension must not turn a transport fragment or HTML error
    # document into a downloadable complete video.
    if mime in {'video/mp2t', 'video/mpeg', 'text/html', 'application/xhtml+xml', 'application/json'}:
        return None
    if path.endswith(('.mp4', '.webm', '.mov', '.mkv')) and mime in {'text/plain', 'application/xml', 'text/xml'}:
        return None
    if re.search(r'\.(?:ts|m4s|aac|vtt|srt)$', path) or re.search(r'(?:^|[/_.-])init(?:[/_.-]|$)', path):
        return None
    if re.search(r'/(?:chunk|segment|fragment)[_-]?\d+\.mp4$', path):
        return None
    if path.endswith('.m3u8') or mime in {'application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl', 'audio/x-mpegurl'}:
        return 'hls'
    if path.endswith('.mpd') or mime == 'application/dash+xml':
        return 'dash'
    if path.endswith(('.mp4', '.webm', '.mov', '.mkv')) or mime in {'video/mp4', 'video/webm', 'video/quicktime', 'video/x-matroska'}:
        return 'file'
    return None


def resolve_media(url, browser_path, timeout=35):
    """Return one playable media URL plus its non-secret HTTP context.

    Raises RuntimeError with safe, user-readable messages on failure. Browser
    requests execute website JavaScript but never reuse the user's profile.
    """
    if not _kind(url) and urlsplit(url).scheme not in {'http', 'https'}:
        raise RuntimeError('瀏覽器解析只接受 HTTP(S) 網址。')
    deadline = time.monotonic() + max(1, min(float(timeout), 90))
    candidates = {}
    browser = None
    page = None
    drm_frames = set()
    frame_areas = {}
    title = ''
    first_media = None

    def capture(response):
        nonlocal first_media
        try:
            request = response.request
            if request.method != 'GET' or not 200 <= response.status < 300:
                return
            kind = _kind(response.url, response.headers.get('content-type', ''))
            if not kind:
                return
            frame = request.frame
            frame_url = frame.url
            if _AD_FRAME.search(frame_url) or _AD_FRAME.search(frame.name or ''):
                return
            referer = request.header_value('referer') or frame_url or url
            ua = request.header_value('user-agent') or ''
            origin = request.header_value('origin')
            record = {'url': response.url, 'referer': referer, 'user_agent': ua,
                      'kind': kind, '_frame': frame_url, '_master': False}
            if origin:
                record['origin'] = origin
            candidates[response.url] = record
            first_media = first_media or time.monotonic()
        except Exception:
            # Detached frames and failed responses are expected during redirects.
            pass

    def finished(request):
        # Read manifests only after their body has finished; response callbacks
        # otherwise block indefinitely on servers that leave a request open.
        try:
            response = request.response()
            if response is None:
                return
            record = candidates.get(response.url)
            if not record or record['kind'] not in {'hls', 'dash'}:
                return
            body = response.text()
            if record['kind'] == 'hls':
                if '#EXTM3U' not in body:
                    candidates.pop(response.url, None)
                    return
                record['_master'] = '#EXT-X-STREAM-INF' in body
                protected = bool(re.search(r'#EXT-X-(?:SESSION-)?KEY:[^\r\n]*METHOD=(?:SAMPLE-AES|SAMPLE-AES-CTR)', body, re.I))
            else:
                protected = bool(re.search(r'<(?:\w+:)?ContentProtection\b', body, re.I))
            if protected:
                drm_frames.add(record['_frame'])
                candidates.pop(response.url, None)
        except Exception:
            pass

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(browser_path), headless=True,
                                                 args=['--autoplay-policy=no-user-gesture-required'])
            context = browser.new_context(viewport={'width': 1280, 'height': 900}, ignore_https_errors=False)
            context.set_default_timeout(1500)
            context.add_init_script(_INIT)
            page = context.new_page()
            page.on('response', capture)
            page.on('requestfinished', finished)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=max(1000, int((deadline-time.monotonic())*1000)))
            except Exception:
                # A timed-out page can still have a working embedded player.
                pass
            clicked = set()
            while time.monotonic() < deadline:
                for frame in list(page.frames):
                    try:
                        if _AD_FRAME.search(frame.url) or _AD_FRAME.search(frame.name or ''):
                            continue
                        info = frame.evaluate("""() => {
                          let area = 0;
                          for (const v of document.querySelectorAll('video')) {
                            const r = v.getBoundingClientRect();
                            area = Math.max(area, r.width*r.height);
                            v.muted = true;
                            const p = v.play(); if (p) p.catch(() => {});
                          }
                          return {area, encrypted: !!window.__downloadEncrypted};
                        }""")
                        area = info['area']
                        if frame.parent_frame:
                            element = frame.frame_element()
                            box = element.bounding_box()
                            if box:
                                area = max(area, box['width']*box['height'])
                        frame_areas[frame.url] = area
                        if info['encrypted']:
                            drm_frames.add(frame.url)
                        if frame.url not in clicked:
                            # Only controls belonging to recognized video players.
                            control = frame.locator('.vjs-big-play-button, .jw-icon-display, .dplayer-play-icon, .plyr__control--overlaid')
                            if control.count() == 1 and control.is_visible():
                                clicked.add(frame.url)
                                control.click(timeout=500, no_wait_after=True)
                    except Exception:
                        pass
                if first_media and time.monotonic()-first_media >= 3:
                    break
                page.wait_for_timeout(min(250, max(1, int((deadline-time.monotonic())*1000))))
            title = page.title()
            if not candidates:
                if drm_frames:
                    raise RuntimeError('這部影片受到 DRM 保護，無法下載。')
                raise RuntimeError('瀏覽器沒有找到可下載的影片串流；網站可能要求登入、驗證或播放操作。')
            def rank(record):
                area = frame_areas.get(record['_frame'], 0)
                # Actual visible player dominance outranks format preference.
                return (area > 60000, area, record['_master'], record['kind'] in {'hls', 'dash'})
            selected = max(candidates.values(), key=rank)
            if selected['_frame'] in drm_frames or page.url in drm_frames:
                raise RuntimeError('這部影片受到 DRM 保護，無法下載。')
            result = {key: value for key, value in selected.items() if not key.startswith('_')}
            result['title'] = title or '網頁影片'
            result['id'] = hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]
            if not result['user_agent']:
                result['user_agent'] = page.evaluate('navigator.userAgent')
            browser.close()
            browser = None
            return result
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError('瀏覽器影片解析失敗，請檢查 Brave 是否可啟動或稍後重試。') from None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
