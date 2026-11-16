"""Offline simulation of extension keyboard conflicts; uses synthetic data."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script("""(() => {
            window.extensionCalls = [];
            window.extensionOnWindow = false;
            // An earlier window-capture extension cannot be overridden by a page.
            window.addEventListener('keydown', event => {
                if (extensionOnWindow && event.code === 'Space') {
                    extensionCalls.push('early-window');
                    event.preventDefault(); event.stopImmediatePropagation();
                }
            }, true);
            // These handlers exist before the app's own script.
            for (const type of ['keydown', 'keypress', 'keyup']) {
                document.addEventListener(type, event => {
                    if (event.code !== 'Space') return;
                    extensionCalls.push(type);
                    event.preventDefault(); event.stopImmediatePropagation();
                }, true);
            }
        })()""")

        def serve(route):
            if route.request.url == "http://myfollows.test/":
                route.fulfill(content_type="text/html", body=(ROOT / "douyin-mcp/ui.html").read_text())
            else:
                route.fulfill(content_type="application/json", body=json.dumps({
                    "videos": [], "creators": [], "logged_in": True, "label_categories": ["frontend"],
                }))

        page.route("**/*", serve)
        page.goto("http://myfollows.test/")
        page.evaluate("""() => {
            window.commands=[];
            ytCommand = command => commands.push(command);
            openPlayer({id:'example',platform:'youtube',title:'Example',labels:[]});
            ytPlaying=false;
            // A later window listener is also blocked by the app.
            window.addEventListener('keydown', event => {
                if (event.code === 'Space') extensionCalls.push('late-window');
            }, true);
        }""")
        page.keyboard.press("Space")
        assert page.evaluate("commands.filter(c => c === 'playVideo' || c === 'pauseVideo')") == ["playVideo"]
        assert page.evaluate("extensionCalls") == []
        page.keyboard.down("Space")
        page.keyboard.down("Space")
        page.keyboard.up("Space")
        assert page.evaluate("commands.filter(c => c === 'playVideo' || c === 'pauseVideo')") == ["playVideo", "pauseVideo"]
        assert page.evaluate("extensionCalls") == []

        # Tagging still owns Space, rather than toggling video playback.
        page.evaluate("() => {rememberLabelCategories(['frontend']);renderPlayerMark(currentVideo);openSheet();}")
        page.wait_for_function("document.activeElement?.dataset.label === 'frontend'")
        page.keyboard.press("Space")
        assert page.locator('#playerLabels [data-label="frontend"]').get_attribute("aria-pressed") == "true"
        assert page.evaluate("commands.filter(c => c === 'playVideo' || c === 'pauseVideo')") == ["playVideo", "pauseVideo"]

        # The app lets editable fields receive spaces normally.
        page.locator("#labelAddBtn").click()
        before = page.evaluate("extensionCalls.length")
        page.keyboard.press("Space")
        assert page.evaluate("extensionCalls.length") > before
        page.keyboard.press("Escape")
        page.evaluate("closeSheet()")

        # If an extension captured Space before the page, a pointer button
        # remains usable; do not pretend the page can defeat that extension.
        page.evaluate("() => {extensionOnWindow=true;ytPlaying=false;commands=[];}")
        page.keyboard.press("Space")
        assert page.evaluate("commands.filter(c => c === 'playVideo' || c === 'pauseVideo')") == []
        page.locator("#playerPauseToggle").click()
        assert page.evaluate("commands.filter(c => c === 'playVideo' || c === 'pauseVideo')") == ["playVideo"]
        print("Keyboard conflicts: early document and late window handlers, repeats, tagging and click fallback passed")
        browser.close()


if __name__ == "__main__":
    main()
