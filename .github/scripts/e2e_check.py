"""Opens a page in each browser the runner image offers, through the hub, as a check does. Run by e2e.sh in
the checks image, with PW_WS_ENDPOINT (the hub's /connect) and GATEWAY_CLIENT_TOKEN."""
import os
from importlib.metadata import version

from playwright.sync_api import sync_playwright

endpoint = os.environ["PW_WS_ENDPOINT"]
headers = {"x-gateway-token": os.environ["GATEWAY_CLIENT_TOKEN"]}
print("checks: playwright", version("playwright"))
with sync_playwright() as p:
    for name in ("chromium", "firefox", "webkit"):
        browser = getattr(p, name).connect(endpoint, headers=headers, timeout=120_000)
        try:
            page = browser.new_page()
            page.set_content("<h1>ok</h1>")
            text = page.text_content("h1")
            assert text == "ok", "{}: the page says {!r}".format(name, text)
            print("{} {} through the hub: ok".format(name, browser.version))
        finally:
            browser.close()
