from pathlib import Path
import sys

# Patch the browser token generator.
path = Path("/app/yt-session-generator/potoken_generator/extractor.py")
source = path.read_text()

if "import os" not in source:
    source = source.replace("import asyncio\n", "import asyncio\nimport os\n", 1)
if "from nodriver import cdp" not in source:
    source = source.replace("import nodriver\n", "import nodriver\nfrom nodriver import cdp\n", 1)

old_click = """    @staticmethod
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

new_click = """    @staticmethod
    async def _click_on_player(tab: nodriver.Tab) -> bool:
        # Wait for the embedded player to initialize before requesting playback.
        for _ in range(15):
            try:
                ready = await tab.evaluate("document.readyState")
                has_player = await tab.evaluate("!!document.querySelector('#movie_player')")
                if ready == "complete" and has_player:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        try:
            started = await tab.evaluate("(() => { const p=document.getElementById('movie_player'); if (p && typeof p.playVideo === 'function') { p.playVideo(); return true; } const v=document.querySelector('video'); if (v) { v.muted=true; v.play(); return true; } return false; })()")
            if started:
                logger.info('requested YouTube player playback via API')
                return True
        except Exception as exc:
            logger.warning('player API playback failed: %s', exc)
        logger.warning('YouTube player API not ready after retries')
        return False
"""

if old_click not in source:
    raise SystemExit("Expected upstream _click_on_player implementation not found")
source = source.replace(old_click, new_click, 1)

old_get = "await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw')"
new_get = """await tab.send(cdp.network.enable())
            await tab.send(cdp.network.set_extra_http_headers(cdp.network.Headers({"Referer": "https://social-video-downloader-6jqk.onrender.com/"})))
            await tab.get('https://www.youtube.com/embed/jNQXAC9IVRw?autoplay=1&mute=1&playsinline=1&origin=https%3A%2F%2Fsocial-video-downloader-6jqk.onrender.com')
            state = await tab.evaluate("JSON.stringify({title:document.title,url:location.href,ready:document.readyState,player:!!document.querySelector('#movie_player'),body:(document.body?.innerText||'').slice(0,240)})")
            logger.info("YouTube embed state: %s", state)"""
if old_get not in source:
    raise SystemExit("Expected upstream YouTube embed navigation not found")
source = source.replace(old_get, new_get, 1)

old_browser = "browser = await nodriver.start(headless=False, sandbox=False,"
new_browser = """browser_args = ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-extensions', '--disable-background-networking', '--disable-sync', '--renderer-process-limit=1']
                proxy = os.getenv('YT_SESSION_PROXY') or os.getenv('RESIDENTIAL_PROXY_URL') or os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')
                if proxy:
                    browser_args.append('--proxy-server=' + proxy)
                    logger.info('Chromium session generator using configured proxy')
                browser = await nodriver.start(headless=False, sandbox=False, browser_args=browser_args,"""
if old_browser not in source:
    raise SystemExit("Expected Chromium startup line not found")
source = source.replace(old_browser, new_browser, 1)

compile(source, str(path), "exec")
path.write_text(source)
print("[yt-session] browser bootstrap, Referer, proxy and low-memory patches applied")

# Make Cobalt handle the token server's HTTP 503 response without JSON parse errors.
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
print("[cobalt] session HTTP error handling patched")

# Route every YouTube API and media request through the same local HTTP bridge
# used by Chromium, so PO tokens and media requests share one residential IP.
youtube_path = Path("/app/src/processing/services/youtube.js")
youtube_source = youtube_path.read_text()
old_import = 'import { fetch, Request } from "undici";'
new_import = 'import { fetch, Request, ProxyAgent } from "undici";'
if old_import not in youtube_source:
    raise SystemExit("Expected Cobalt YouTube undici import not found")
youtube_source = youtube_source.replace(old_import, new_import, 1)
old_period = "const PLAYER_REFRESH_PERIOD = 1000 * 60 * 15; // ms\nlet innertube, lastRefreshedAt;"
new_period = "const PLAYER_REFRESH_PERIOD = 1000 * 60 * 15; // ms\nconst proxyAgent = process.env.YOUTUBE_PROXY_URL ? new ProxyAgent(process.env.YOUTUBE_PROXY_URL) : undefined;\nlet innertube, lastRefreshedAt;"
if old_period not in youtube_source:
    raise SystemExit("Expected Cobalt YouTube refresh declaration not found")
youtube_source = youtube_source.replace(old_period, new_period, 1)
old_start = "export default async function (o) {\n    const quality ="
new_start = "export default async function (o) {\n    o.dispatcher ??= proxyAgent;\n    const quality ="
if old_start not in youtube_source:
    raise SystemExit("Expected Cobalt YouTube service entry not found")
youtube_source = youtube_source.replace(old_start, new_start, 1)
if "        dispatcher: undefined," not in youtube_source:
    raise SystemExit("Expected Cobalt media dispatcher override not found")
youtube_source = youtube_source.replace("        dispatcher: undefined,", "        dispatcher: o.dispatcher,", 1)
youtube_path.write_text(youtube_source)
print("[cobalt] YouTube API and media requests use shared residential proxy bridge")

# Chromium may omit this newer CDP field; make the generated parser tolerant.
network_path = Path("/opt/yt-session-venv/lib/python3.12/site-packages/nodriver/cdp/network.py")
network_source = network_path.read_text()
old_policy = "local_network_access_request_policy=LocalNetworkAccessRequestPolicy.from_json(json['localNetworkAccessRequestPolicy']),"
new_policy = "local_network_access_request_policy=LocalNetworkAccessRequestPolicy.from_json(json['localNetworkAccessRequestPolicy']) if json.get('localNetworkAccessRequestPolicy') is not None else None,"
if old_policy not in network_source:
    raise SystemExit("Expected nodriver ClientSecurityState parser not found")
network_path.write_text(network_source.replace(old_policy, new_policy, 1))
sys.path.insert(0, "/opt/yt-session-venv/lib/python3.12/site-packages")
from nodriver.cdp.network import ClientSecurityState
state = ClientSecurityState.from_json({"initiatorIsSecureContext": True, "initiatorIPAddressSpace": "Public"})
assert state.local_network_access_request_policy is None
print("[nodriver] optional CDP field compatibility test passed")
