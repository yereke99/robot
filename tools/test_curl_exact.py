import httpx

cookie_str = """BNES_selected_contracts_content_supplier_27822=acZzWVdIP7CGTBdCXi2xTbU/OD9ygpyk30PX5KiuwCe/czqSi72zvNUEBGtbY4LGTaTmL1M+d2J9AXz8Xn1bLgbEAeRG6qoFfPOh8ssMltA2OAisqiRVEA30I31a1zex; BNES_selected_contracts_content_supplier_751175=SEWx38HbLwmryAiBDd1IrwRbpQKLHavcRLx3LNuqaQ0Au1hj1pQX7rDgDPV7I0adJy2mkLB80KcTKWPEXbVTfxnyblStlyvBSu7rb2IZP/vlrZBQYSTQFc/eoNfQMvRo; agreement_user_2024=OTYzNDQ1XzUzOTM1Ng%3D%3D; csrf_cookie=81c6168946bc0fe348d47145d250c3cd; ci_session=WjFdZ1FmUD8FKlcmUz5XZgAzUGcDClVwXCcAMl17V3ZQOwI9DTMAU1k6C28DXAVyDmEPfgNuUmQAN1BnBw4LLl9lU2ZcOFBhAjNTZwA2VmpaMV1oUWtQMgUyVzBTNVdmADVQPwNgVWVcNwBvXWxXM1BmAjANaQA7WWALOQMzBWcOPg80A2NSYAA3UG0HawttX2NTZVwxUDcCZlNlAGVWYlpnXWlRN1A2BTNXZ1NhV28AZlBkA2ZVNFxhADNdOlc1UA4CcA1mAH9ZaQs6AzAFag4GDy8DPVIiAFxQPAc2C2hfJVNhXHpQcQJcUyEAa1YpWmpdbFFgUD8FDVd3UzdXYAAtUG4DZFV8XGMAZF0mVzdQZgJkDQEALlloC3gDOgVhDmoPNwMIUnAAdlAuBzcLfl8IU2VcblBnAm5TdwAMVnhaa10uUWhQNAVgV2RTPlcLACFQEAM5VShcPQA7XWRXZFB9AmcNcwA8WXMLIwNXBTkONA9pAztSJQBwUH0HHAtYX3dTNVw5UCwCMFM4AHBWDVo5XTNRZFAxBWpXdVN8V2EAN1B0A3ZVE1wkACddZFdgUAUCNw0%2FAEdZOgt%2FAy8FZQ5pDzoDelJhADVQfQd6C0dfH1NQXERQTgIsUyMAPFYzWjtdOFFyUEIFNFc2U29XOAAqUH0DFVU6XCYAOF1lV2BQfQJjDWgAO1l9CzsDLgVgDnQPPQN0UgEAYlA7BzMLfl8%2BUytcPFAxAjdTLQBjVmxaDF1%2FUWlQdgVrV2RTN1dtAF9QfwM6VTNcJwAjXVdXZFAxAiYNNAB6WToLfwN5BQwOeA82Az1SaAAyUGoHaws4X2JTPFw5UDQCM1M2AGtWJw%3D%3D"""

cookies = {}
for item in cookie_str.split(";"):
    if "=" in item:
        k, v = item.strip().split("=", 1)
        cookies[k.strip()] = v.strip()

headers = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

transport = httpx.HTTPTransport(retries=3)
client = httpx.Client(base_url="https://v3bl.goszakup.gov.kz", cookies=cookies, headers=headers, transport=transport, follow_redirects=True, timeout=15.0)
r = client.get("/cabinet/profile")
print("STATUS:", r.status_code)
print("FINAL URL:", r.url)
print("FOUND Истыбаев:", "Истыбаев" in r.text)
print("FOUND Беласар:", "Беласар" in r.text)
print("FOUND БИН 070440013464:", "070440013464" in r.text)
