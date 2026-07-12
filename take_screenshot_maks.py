from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://dum-e.com/site/maks-3a4c")
        page.wait_for_timeout(3000)
        page.screenshot(path="maks_b8a5_actual_site.png", full_page=True)
        browser.close()

if __name__ == "__main__":
    run()
