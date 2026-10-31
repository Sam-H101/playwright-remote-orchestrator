from playwright.sync_api import Playwright
import os


url = "https://status.docusign.com/"
second_ms = 1000
thirty_seconds_ms = 30 * second_ms


def test_docusign(playwright: Playwright):
    port = os.environ.get("port")
    host = os.environ.get("host")
    try:
        browser = playwright.chromium.connect("ws://{}:{}".format(host,port))
    except Exception as ex:
        raise Exception("connect_Failure") from ex
    page = browser.new_page()
    try:
        page.goto(url)
        NA1 = page.locator(".environment-status-column > .key-status").first
        NA2 = page.locator("tr:nth-child(4) > .environment-status-column > .key-status")
        NA3 = page.locator("tr:nth-child(6) > .environment-status-column > .key-status")
        NA4 = page.locator("tr:nth-child(10) > .environment-status-column > .key-status")
        classes = (NA1.get_attribute("class"), NA2.get_attribute("class"), NA3.get_attribute("class"), NA4.get_attribute("class"))
        i = 1
        fail = False
        for itm in classes:

            key = str.split(itm, " ")[1]
            if not key.__contains__("GRN"):

                fail = True
                break

            i = i + 1
        if fail:
            raise Exception("NA{} Failed".format(i))

        page.close()
        browser.close()

    except Exception as ex:
        page.screenshot(path="main_error.jpeg")

        page.close()
        browser.close()

        raise ex
