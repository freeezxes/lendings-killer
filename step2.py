from playwright.sync_api import sync_playwright
import time

def step2():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0]
        
        # Click Create Site
        page.click(".dash-card[href='/dashboard/create']")
        page.wait_for_url("**/payment*")
        
        # Give time for rendering
        time.sleep(2)
        
        with open("payment_page_state.html", "w") as f:
            f.write(page.content())
            
        print("Payment page HTML saved to payment_page_state.html")
        browser.close()

if __name__ == "__main__":
    step2()
