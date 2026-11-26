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
        # Use trusted CDP mouse events: HTMLElement.click() is synthetic and may
        # not trigger YouTube's player request / PO-token generation.
        await asyncio.sleep(3)
        expression = (
            "(() => {"
            " const button = document.querySelector('.ytp-large-play-button');"
            " const player = document.querySelector('#movie_player');"
            " const target = button || player;"
            " if (!target) return null;"
            " const r = target.getBoundingClientRect();"
            " return (r.left + r.width / 2) + \",\" + (r.top + r.height / 2);"
            "})()"
        )
        for attempt in range(5):
            try:
                point = await tab.evaluate(expression)
                if point:
                    x_text, y_text = point.split(",", 1)
                    x = float(x_text)
                    y = float(y_text)
                    await tab.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=x, y=y))
                    logger.info("player click coordinates x=%s y=%s", x, y)
                    await tab.send(cdp.input_.dispatch_mouse_event(type_="mousePressed", x=x, y=y, button=cdp.input_.MouseButton.LEFT, click_count=1))
                    await asyncio.sleep(0.1)
                    await tab.send(cdp.input_.dispatch_mouse_event(type_="mouseReleased", x=x, y=y, button=cdp.input_.MouseButton.LEFT, click_count=1))
                    logger.info('clicked YouTube player using trusted CDP mouse events')
                    return True
            except Exception as exc:
                logger.warning('CDP player click attempt %s failed: %s', attempt + 1, exc)
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
