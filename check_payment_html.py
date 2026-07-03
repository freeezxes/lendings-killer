from playwright.sync_api import sync_playwright

def check():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # We need to register first to bypass auth
        page.goto("https://dum-e.com/auth")
        page.fill("#regPhone", "77771234567")
        page.click("#regBtn")
        page.wait_for_url("**/dashboard*")
        
        # Now go to payment
        page.goto("https://dum-e.com/payment?reason=welcome")
        import time
        time.sleep(2)
        
        with open("payment_debug.html", "w") as f:
            f.write(page.content())
            
        browser.close()

if __name__ == "__main__":
    check()
