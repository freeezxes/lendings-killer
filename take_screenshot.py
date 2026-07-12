from playwright.sync_api import sync_playwright

def take_screenshot():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://dum-e.com/site/anna-b8a5")
        
        # Wait a bit for the React app to render completely
        page.wait_for_timeout(3000)
        
        # Take full page screenshot
        page.screenshot(path="anna_b8a5_actual_site.png", full_page=True)
        print("Screenshot saved to anna_b8a5_actual_site.png")
        browser.close()

if __name__ == "__main__":
    take_screenshot()
