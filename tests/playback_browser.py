"""Offline native-controls, layout and pause/resume checks; requires Playwright Chromium."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def serve(route):
            if route.request.url == "http://myfollows.test/":
                route.fulfill(content_type="text/html", body=(ROOT / "douyin-mcp/ui.html").read_text())
            else:
                route.fulfill(content_type="application/json", body=json.dumps({
                    "videos": [], "creators": [], "logged_in": True, "label_categories": ["example"],
                }))

        page.route("**/*", serve)
        page.goto("http://myfollows.test/")
        page.evaluate("""() => {
            window.fullscreenRequests = 0;
            document.documentElement.requestFullscreen = () => { fullscreenRequests++; return Promise.resolve(); };
            window.commands = [];
            ytCommand = (command) => commands.push(command);
            openPlayer({id:'example',platform:'youtube',title:'Example',labels:[]});
        }""")
        assert page.evaluate("fullscreenRequests") == 0
        initial = page.locator("#playerFrame").bounding_box()
        page.keyboard.press("f")
        assert page.locator("#playerInfoOverlay").is_hidden()
        expanded = page.locator("#playerFrame").bounding_box()
        assert expanded["width"] >= initial["width"]
        assert expanded["height"] >= initial["height"]
        assert page.evaluate("fullscreenRequests") == 0
        assert page.evaluate("document.fullscreenElement") is None
        page.keyboard.press("f")
        assert page.locator("#playerInfoOverlay").is_visible()

        def state(event, info=None):
            page.evaluate("""({event, info}) => window.dispatchEvent(new MessageEvent('message', {
                origin:'https://www.youtube.com', source:playerFrame.contentWindow,
                data:JSON.stringify({event,info})
            }))""", {"event": event, "info": info})

        # P pauses and uncovers native controls; Space resumes.
        assert page.locator("#playerProgressTrack").count() == 0
        assert page.locator("#playerSeek").count() == 0
        page.keyboard.press("p")
        assert page.evaluate("commands.at(-1)") == "pauseVideo"
        assert page.locator("#playerYtCapture").is_hidden()
        state("onStateChange", 2)
        page.locator("#playerFullscreen").focus()
        page.keyboard.press("Space")
        assert page.evaluate("commands.at(-1)") == "playVideo"
        count = page.evaluate("commands.length")
        page.locator("#playerFullscreen").dispatch_event("keydown", {"key": " ", "code": "Space", "repeat": True})
        assert page.evaluate("commands.length") == count
        state("onStateChange", 1)
        page.keyboard.press("Space")
        assert page.evaluate("commands.at(-1)") == "pauseVideo"
        state("onAutoplayBlocked")
        page.keyboard.press("Space")
        assert page.evaluate("commands.at(-1)") == "playVideo"

        # Exercise real native-video pause/play with a synthetic canvas stream.
        page.evaluate("""async () => {
            currentPlatform = 'douyin';
            playerFrame.classList.add('hidden'); playerVideo.classList.remove('hidden');
            const canvas = document.createElement('canvas');
            canvas.width=320; canvas.height=180;
            canvas.getContext('2d').fillRect(0,0,320,180);
            playerVideo.muted=true; playerVideo.srcObject=canvas.captureStream();
            await playerVideo.play(); playerVideo.pause();
        }""")
        page.keyboard.press("p")
        assert page.evaluate("playerVideo.controls")
        page.locator("#playerVideo").focus()
        assert page.evaluate("playerVideo.paused")
        page.keyboard.press("Space")
        page.wait_for_function("!playerVideo.paused")
        page.keyboard.press("Space")
        page.wait_for_function("playerVideo.paused")
        for width, height in [(1440,900), (390,844), (844,390)]:
            page.set_viewport_size({"width":width,"height":height})
            page.evaluate("() => openPlayer({id:'example',platform:'youtube',title:'Example',labels:[]})")
            page.wait_for_function("""() => {
                const media=playerFrame.getBoundingClientRect();
                const text=playerTitle.getBoundingClientRect();
                return Math.abs(media.left-text.left)<1;
            }""")
            media=page.locator("#playerFrame").bounding_box()
            caption=page.locator("#playerInfoOverlay").bounding_box()
            assert caption["y"] >= media["y"]+media["height"]-1
            assert page.locator("#playerInfoOverlay").evaluate(
                "(e) => getComputedStyle(e).backgroundColor") == "rgba(0, 0, 0, 0)"
        print("Playback browser: P controls, Space resume, no extra timeline, and left-aligned plain captions passed")
        browser.close()


if __name__ == "__main__":
    main()
