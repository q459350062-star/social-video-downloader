from pathlib import Path

path = Path("/app/yt-session-generator/potoken_generator/extractor.py")
source = path.read_text()

old = """    @staticmethod
    async def _click_on_player(tab: nodriver.Tab) -> bool:
        try:
            player = await tab.select('#movie_player', 10)
        except asyncio.TimeoutError:
            logger.warning('update failed: unable to locate video player on the page')
            return False
        else:
            await player.click()
            return True
"""

new = """    @staticmethod
    async def _click_on_player(tab: nodriver.Tab) -> bool:
        # Wait for the embed/player JS to hydrate before interacting with it.
        for _ in range(15):
            try:
                ready = await tab.evaluate("document.readyState")
                if ready == "complete":
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        state = await tab.evaluate("JSON.stringify({title:document.title,ready:document.readyState,player:!!document.querySelector('#movie_player'),button:!!document.querySelector('.ytp-large-play-button'),video:!!document.querySelector('video'),body:(document.body?.innerText||'').slice(0,240)})")
        logger.info('YouTube player DOM state: %s', state)
        # The embedded player API can start playback more reliably than clicking
        # the container while the play button is still being replaced.
        try:
            started = await tab.evaluate("(() => { const p=document.getElementById('movie_player'); if (p && typeof p.playVideo === 'function') { p.playVideo(); return true; } const v=document.querySelector('video'); if (v) { v.muted=true; v.play(); return true; } return false; })()")
            if started:
                logger.info('requested YouTube player playback via player API')
                return True
        except Exception as exc:
            logger.warning('player API playback request failed: %s', exc)
        # Fallback to fresh trusted element clicks; never reuse stale node ids.
        for attempt in range(5):
            for selector in ('.ytp-large-play-button', '#movie_player'):
                try:
                    player = await tab.select(selector, 3)
                    await player.click()
                    logger.info('clicked YouTube player using selector %s', selector)
                    return True
                except Exception as exc:
                    logger.warning('player click attempt %s selector %s failed: %s', attempt + 1, selector, exc)
            await asyncio.sleep(2)
        logger.warning('update failed: unable to start YouTube player after retries')
        return False
"""

if old not in source:
    raise SystemExit("Expected upstream _click_on_player implementation not found")
source = source.replace(old, new, 1)
source = source.replace(
    "await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw')",
    """await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw?autoplay=1&mute=1&playsinline=1')
            state = await tab.evaluate("JSON.stringify({title:document.title,url:location.href,ready:document.readyState,player:!!document.querySelector('#movie_player'),button:!!document.querySelector('.ytp-large-play-button'),video:!!document.querySelector('video')})")
            logger.info("YouTube embed state: %s", state)""",
    1,
)
source = source.replace(
    "browser = await nodriver.start(headless=False, sandbox=False,",
    "browser = await nodriver.start(headless=False, sandbox=False, browser_args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-extensions', '--disable-background-networking', '--disable-sync', '--renderer-process-limit=1'],",
    1,
)
compile(source, str(path), 'exec')
path.write_text(source)

# Cobalt should report an HTTP 503 from the not-yet-ready token server clearly,
# rather than trying to parse its plain-text body as JSON.
cobalt_path = Path("/app/src/processing/helpers/youtube-session.js")
cobalt_source = cobalt_path.read_text()
old_fetch = """    const newSession = await fetch(
        sessionServerUrl,
        { method: 'POST', dispatcher: defaultAgent }
    ).then(a => a.json());"""
new_fetch = """    const response = await fetch(
        sessionServerUrl,
        { method: 'POST', dispatcher: defaultAgent }
    );
    if (!response.ok) {
        const errorBody = await response.text();
        throw new Error("session server returned HTTP " + response.status + ": " + errorBody);
    }
    const newSession = await response.json();"""
if old_fetch not in cobalt_source:
    raise SystemExit("Expected Cobalt session fetch implementation not found")
cobalt_path.write_text(cobalt_source.replace(old_fetch, new_fetch, 1))
print("[cobalt] patched session HTTP error handling")
print("[yt-session] patched player bootstrap and click retries")
