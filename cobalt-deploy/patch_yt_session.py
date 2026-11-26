from pathlib import Path

path = Path("/app/yt-session-generator/potoken_generator/extractor.py")
source = path.read_text()

if "from nodriver import cdp" not in source:
    source = source.replace("import nodriver\n", "import nodriver\nfrom nodriver import cdp\n", 1)

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
    """await tab.send(cdp.network.enable())
            await tab.send(cdp.network.set_extra_http_headers(cdp.network.Headers({"Referer": "https://social-video-downloader-6jqk.onrender.com/"})))
            await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw?autoplay=1&mute=1&playsinline=1&origin=https%3A%2F%2Fsocial-video-downloader-6jqk.onrender.com')
            state = await tab.evaluate("JSON.stringify({title:document.title,url:location.href,ready:document.readyState,player:!!document.querySelector('#movie_player'),button:!!document.querySelector('.ytp-large-play-button'),video:!!document.querySelector('video'),body:(document.body?.innerText||'').slice(0,240)})")
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

# Chromium's CDP payload can omit this newer field on some bundled versions.
# Treat it as optional so nodriver can continue parsing Network events.
network_path = Path("/opt/yt-session-venv/lib/python3.12/site-packages/nodriver/cdp/network.py")
network_source = network_path.read_text()
old_policy = "local_network_access_request_policy=LocalNetworkAccessRequestPolicy.from_json(json['localNetworkAccessRequestPolicy']),"
new_policy = "local_network_access_request_policy=LocalNetworkAccessRequestPolicy.from_json(json['localNetworkAccessRequestPolicy']) if json.get('localNetworkAccessRequestPolicy') is not None else None,"
if old_policy not in network_source:
    raise SystemExit("Expected nodriver ClientSecurityState parser not found")
network_path.write_text(network_source.replace(old_policy, new_policy, 1))
import sys
sys.path.insert(0, "/opt/yt-session-venv/lib/python3.12/site-packages")
from nodriver.cdp.network import ClientSecurityState
state = ClientSecurityState.from_json({"initiatorIsSecureContext": True, "initiatorIPAddressSpace": "Public"})
assert state.local_network_access_request_policy is None
print("[nodriver] optional CDP security-state field test passed")
print("[yt-session] patched player bootstrap and click retries")
