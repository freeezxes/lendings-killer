from playwright.sync_api import sync_playwright

def snapshot():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        if context.pages:
            page = context.pages[0]
            page.screenshot(path="browser_snapshot.png", full_page=True)
            print("Snapshot saved to browser_snapshot.png")
            print(f"Current URL: {page.url}")
        else:
            print("No pages open!")
        browser.close()

if __name__ == "__main__":
    snapshot()
