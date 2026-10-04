import json
import re
import urllib.parse
from playwright.sync_api import sync_playwright

def test_maps(query="Grand Hotel Oslo"):
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="no-NO"
        )
        page = context.new_page()
        encoded = urllib.parse.quote(query)
        page.goto(f"https://www.google.com/maps/search/{encoded}")
        
        # Click consent if present
        for sel in ["button:has-text('Godta alle')", "button:has-text('Accept all')", "form button"]:
            try:
                page.click(sel, timeout=1200)
                break
            except Exception:
                pass
                
        page.wait_for_timeout(3500)
        final_url = page.url
        print("Final URL:", final_url)
        print("Page Title:", page.title())
        
        # 1. CID from URL
        cid = None
        m = re.search(r":0x([0-9a-fA-F]+)!", final_url)
        if m:
            cid = str(int(m.group(1), 16))
            
        # 2. Title
        title_el = page.query_selector("h1.DUwDvf, div.qBF1Pd")
        title = title_el.inner_text().strip() if title_el else page.title().split(" - ")[0]
        
        # 3. Rating & Review count
        rating = None
        rating_count = None
        rating_el = page.query_selector("div.F7nice")
        if rating_el:
            text = rating_el.inner_text().replace(",", ".")
            m_rat = re.search(r"(\d+\.\d+)", text)
            if m_rat:
                rating = float(m_rat.group(1))
            m_cnt = re.search(r"\((\d[\d\s.,]*)\)", text)
            if m_cnt:
                digits = re.sub(r"\D", "", m_cnt.group(1))
                if digits:
                    rating_count = int(digits)
                    
        # 4. Address
        addr_el = page.query_selector("button[data-item-id='address']")
        address = addr_el.inner_text().strip() if addr_el else None
        
        # 5. Website
        web_el = page.query_selector("a[data-item-id='authority']")
        website = web_el.get_attribute("href") if web_el else None
        
        # 6. Phone
        phone_el = page.query_selector("button[data-item-id^='phone:']")
        phone = phone_el.inner_text().strip() if phone_el else None
        
        # Clean unicode characters / icon glyphs
        def clean_s(val):
            if not val:
                return None
            return re.sub(r"[\ue000-\uf8ff]", "", str(val)).strip()

        result = {
            "title": clean_s(title),
            "address": clean_s(address),
            "rating": rating,
            "ratingCount": rating_count,
            "cid": cid,
            "website": website,
            "phoneNumber": clean_s(phone)
        }
        print("Replicated Serper Place output:")
        print(json.dumps(result, indent=2))
        browser.close()
        return result

if __name__ == "__main__":
    test_maps("Grand Hotel Oslo")


