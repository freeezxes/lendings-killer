import sys
import json
from playwright.sync_api import sync_playwright

def scrape_design_tokens(url):
    if not url.startswith("http"):
        url = "https://" + url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            # Wait until network is mostly idle to ensure SPA renders
            page.goto(url, timeout=15000, wait_until="networkidle")
        except Exception as e:
            # If networkidle times out (e.g. infinite polling), just proceed with what we have
            pass

        js_script = """
        () => {
            const elements = document.querySelectorAll('*');
            const colors = new Set();
            const bgColors = new Set();
            const fonts = new Set();
            const radii = new Set();
            const shadows = new Set();

            elements.forEach(el => {
                const style = window.getComputedStyle(el);
                
                // Color
                if (style.color && style.color !== 'rgba(0, 0, 0, 0)') {
                    colors.add(style.color);
                }
                
                // Background
                if (style.backgroundColor && style.backgroundColor !== 'rgba(0, 0, 0, 0)' && style.backgroundColor !== 'transparent') {
                    bgColors.add(style.backgroundColor);
                }
                
                // Fonts
                if (style.fontFamily) {
                    fonts.add(style.fontFamily);
                }
                
                // Border Radius
                if (style.borderRadius && style.borderRadius !== '0px') {
                    radii.add(style.borderRadius);
                }
                
                // Box Shadow
                if (style.boxShadow && style.boxShadow !== 'none') {
                    shadows.add(style.boxShadow);
                }
            });
            
            const googleFonts = [];
            document.querySelectorAll('link[href*="fonts.googleapis.com"]').forEach(link => {
                googleFonts.push(link.href);
            });

            return {
                colors: Array.from(colors).slice(0, 15),
                backgrounds: Array.from(bgColors).slice(0, 10),
                fonts: Array.from(fonts).map(f => f.split(',')[0].replace(/['"]/g, '').trim()).slice(0, 5),
                border_radius: Array.from(radii).slice(0, 5),
                shadows: Array.from(shadows).slice(0, 5),
                google_fonts_urls: googleFonts.slice(0, 3)
            };
        }
        """
        
        try:
            tokens = page.evaluate(js_script)
            print(json.dumps(tokens))
        except Exception as e:
            print(json.dumps({"error": str(e)}))
        finally:
            browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No URL provided"}))
        sys.exit(1)
    scrape_design_tokens(sys.argv[1])
