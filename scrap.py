from playwright.sync_api import sync_playwright

with sync_playwright() as p:

    context = p.chromium.launch_persistent_context(
        user_data_dir=r"C:\Users\rajpt\AppData\Local\Google\Chrome\User Data",
        channel="chrome",
        headless=False
    )

    page = context.new_page()

    page.goto(
        "https://www.google.com/search?q=PCB+manufacturer+india"
    )

    input("Press Enter")

    context.close()