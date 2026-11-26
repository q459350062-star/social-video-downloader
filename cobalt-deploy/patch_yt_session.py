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
        # YouTube replaces the player node during hydration. Re-select the DOM
        # element before each trusted click instead of reusing a stale CDP node id.
        await asyncio.sleep(3)
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
        logger.warning('update failed: unable to click YouTube player after retries')
        return False
"""

if old not in source:
    raise SystemExit("Expected upstream _click_on_player implementation not found")
source = source.replace(old, new, 1)
source = source.replace(
    "browser = await nodriver.start(headless=False, sandbox=False,",
    "browser = await nodriver.start(headless=False, sandbox=False, browser_args=['--disable-gpu', '--disable-extensions', '--disable-background-networking', '--disable-sync', '--renderer-process-limit=1'],",
    1,
)
compile(source, str(path), 'exec')
path.write_text(source)
print("[yt-session] patched trusted CDP player click")
