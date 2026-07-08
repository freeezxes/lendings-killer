import time
from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # 1. Landing Light
        page.goto("http://localhost:8080")
        time.sleep(2) # wait for animations
        page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
        page.screenshot(path="landing_light.png", full_page=True)
        
        # 2. Landing Dark
        page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        time.sleep(0.5)
        page.screenshot(path="landing_dark.png", full_page=True)
        
        # 3. Auth Light
        page.goto("http://localhost:8080/auth/login")
        time.sleep(1)
        page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
        page.screenshot(path="auth_light.png", full_page=True)
        
        # 4. Auth Dark
        page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        time.sleep(0.5)
        page.screenshot(path="auth_dark.png", full_page=True)
        
        # 5. Chat Light
        page.goto("http://localhost:8080/chat")
        time.sleep(1)
        page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
        page.screenshot(path="chat_light.png", full_page=True)
        
        # 6. Chat Dark
        page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        time.sleep(0.5)
        page.screenshot(path="chat_dark.png", full_page=True)
        
        # 7. Dashboard Light
        page.goto("http://localhost:8080/dashboard")
        time.sleep(1)
        page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
        page.screenshot(path="dashboard_light.png", full_page=True)
        
        browser.close()

if __name__ == "__main__":
    run()
