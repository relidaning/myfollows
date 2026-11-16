"""Offline tagging UI checks; run with python3 tests/tagging_browser.py."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        writes = []
        fail_labels = False
        categories = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
        videos = [
            {"id": name, "platform": "youtube", "user": "Sample", "title": name, "labels": []}
            for name in ["first", "second"]
        ]

        def route_request(route):
            nonlocal fail_labels
            request = route.request
            if request.url == "http://myfollows.test/":
                route.fulfill(content_type="text/html", body=(ROOT / "douyin-mcp/ui.html").read_text())
                return
            if request.method == "POST":
                writes.append((request.url, request.post_data_json))
                if fail_labels and request.url.endswith("/labels"):
                    route.fulfill(status=500, body="{}")
                    return
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "videos": [], "creators": [], "label_categories": categories, "logged_in": True,
            }))

        page.route("**/*", route_request)
        page.goto("http://myfollows.test/")
        page.evaluate("""({videos, categories}) => {
            rememberLabelCategories(categories); currentVideos = videos;
            openPlayer(videos[0]); openSheet();
        }""", {"videos": videos, "categories": categories})
        page.locator("#playerSheet").wait_for(state="visible")
        assert page.locator("#playerUnlistBtn").count() == 0
        assert page.locator("#playerUnfollowBtn").inner_text() == "Unsubscribe channel"
        page.wait_for_function("document.activeElement?.dataset.label === 'alpha'")
        page.keyboard.press("l")
        assert page.locator(":focus").get_attribute("data-label") == "beta"
        page.keyboard.press("j")
        lower = page.locator(":focus").bounding_box()["y"]
        page.keyboard.press("k")
        assert page.locator(":focus").bounding_box()["y"] < lower
        page.keyboard.press("h")
        page.keyboard.press("Space")
        assert page.locator("#playerLabels [aria-pressed=true]").count() == 1
        page.keyboard.press("3")
        assert page.locator("#playerScoreDescription").inner_text() == "Great"
        assert page.locator(":focus").get_attribute("data-label") is not None

        # Typing navigation letters or numbers must not trigger shortcuts.
        page.locator("#labelAddBtn").click()
        page.locator("#labelAddInput").press_sequentially("frontend")
        page.keyboard.press("Tab")
        page.wait_for_function("currentVideo.labels.includes('frontend')")
        assert page.locator('#playerLabels [data-label="frontend"]').count() == 1
        assert page.locator('#labelFilter option[value="frontend"]').count() == 1
        page.evaluate("() => rememberLabelCategories(['backend'])")
        page.evaluate("() => renderPlayerMark(currentVideo)")
        assert page.locator('#playerLabels [data-label="frontend"]').count() == 1

        # Submit waits for saves and advances; the custom category survives.
        page.locator('#playerLabels [data-label="frontend"]').focus()
        page.keyboard.press("Enter")
        page.wait_for_function("currentVideo.id === 'second'")
        assert any(data.get("labels") and "frontend" in data["labels"] for _, data in writes)
        page.evaluate("openSheet()")
        assert page.locator('#playerLabels [data-label="frontend"]').count() == 1
        page.keyboard.press("Enter")
        assert page.locator("#playerTagStatus").inner_text() == "Choose a score and at least one tag."

        page.keyboard.press("5")
        page.keyboard.press("Space")
        fail_labels = True
        page.keyboard.press("Enter")
        page.wait_for_function("document.getElementById('playerTagStatus').textContent.includes('Could not save')")
        assert page.locator("#playerSheet").is_visible()
        assert page.evaluate("currentVideo.id") == "second"
        fail_labels = False
        page.keyboard.press("Enter")
        page.wait_for_function("!document.getElementById('playerSheet').classList.contains('show')")
        print("Tagging: navigation, selection, custom tags, validation, save/retry, and actions passed")
        browser.close()


if __name__ == "__main__":
    main()
