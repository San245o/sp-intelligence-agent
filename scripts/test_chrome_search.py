from playwright.sync_api import sync_playwright

def test_search():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        # Test query for a real company
        query = "DRAUPNIR INVEST AS TRONDHEIM Norge"
        page.goto(f"https://www.google.com/search?q={query}&hl=en")
        
        # Click Google consent if present
        for sel in ["#L2AGLb", "button:has-text('Accept all')", "button:has-text('I agree')"]:
            try:
                page.click(sel, timeout=1000)
                break
            except Exception:
                pass
        
        page.wait_for_timeout(1000)
        print("Page URL:", page.url)
        print("Page Title:", page.title())
        
        # Extract search result elements
        results = []
        # Google search results container: 'div.g' or 'div[data-sokoban-container]'
        links = page.eval_on_selector_all("a[href^='http']", "elements => elements.map(e => ({href: e.href, text: e.innerText}))")
        
        # Filter for distinct external domains
        seen = set()
        for item in links:
            url = item["href"]
            if any(bad in url for bad in ["google.", "gstatic.", "schema.org", "w3.org"]):
                continue
            base = url.split("?")[0]
            if base not in seen:
                seen.add(base)
                results.append((base, item["text"].strip().replace("\n", " ")))
        
        print(f"Discovered {len(results)} external links:")
        for r, text in results[:8]:
            print(f" - {r} [{text[:40]}]")
            
        browser.close()

if __name__ == "__main__":
    test_search()
