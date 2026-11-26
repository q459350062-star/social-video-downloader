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
        # YouTube can replace the player DOM node while the embed page is hydrating.
        # Use an in-page DOM click instead of a stale CDP node id, and retry briefly.
        await asyncio.sleep(3)
        expression = (
            "(() => {"
            " const button = document.querySelector('.ytp-large-play-button');"
            " const player = document.querySelector('#movie_player');"
            " const target = button || player;"
            " if (!target) return false;"
            " target.click();"
            " return true;"
            "})()"
        )
        for attempt in range(5):
            try:
                clicked = await tab.evaluate(expression, return_by_value=True)
                if clicked:
                    logger.info('clicked YouTube player using DOM evaluation')
                    return True
            except Exception as exc:
                logger.warning('DOM click attempt %s failed: %s', attempt + 1, exc)
            await asyncio.sleep(2)
        logger.warning('update failed: unable to click YouTube player after retries')
        return False
"""

if old not in source:
    raise SystemExit("Expected upstream _click_on_player implementation not found")
path.write_text(source.replace(old, new, 1))
print("[yt-session] patched resilient player click")
