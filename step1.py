from playwright.sync_api import sync_playwright
import time

def step1():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        
        page.goto("https://dum-e.com/auth")
        
        phone = f"7777{int(time.time())}"
        page.fill("#regPhone", phone)
        page.click("#regBtn")
        page.wait_for_url("**/dashboard*")
        
        print("Registered and on dashboard!")
        browser.close()

if __name__ == "__main__":
    step1()
