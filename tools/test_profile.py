import httpx

raw_header = """BNES_selected_contracts_content_supplier_27822=acZzWVdIP7CGTBdCXi2xTbU/OD9ygpyk30PX5KiuwCe/czqSi72zvNUEBGtbY4LGTaTmL1M+d2J9AXz8Xn1bLgbEAeRG6qoFfPOh8ssMltA2OAisqiRVEA30I31a1zex; BNES_selected_contracts_content_supplier_751175=SEWx38HbLwmryAiBDd1IrwRbpQKLHavcRLx3LNuqaQ0Au1hj1pQX7rDgDPV7I0adJy2mkLB80KcTKWPEXbVTfxnyblStlyvBSu7rb2IZP/vlrZBQYSTQFc/eoNfQMvRo; agreement_user_2024=OTYzNDQ1XzUzOTM1Ng%3D%3D; csrf_cookie=81c6168946bc0fe348d47145d250c3cd; ci_session=DmVYYlNkVDsEKwBxWzZWZ1ZlUGcDCgciCnFTYVdxUXAEbwU6Az0EV10%2BCW1RDlEmB2hSI1E8AzUHMAE2AQgGI1phBGtVNQBnVDVTMlYxBG4OM1g8U2BUNwQ0ADdbblZvVmBQPgMwB2YKYFNlV2BRMQRiBWwDYQQ8XTIJalExUTQHY1I2UTEDMQc8AWkBNQYyWmcEZFUxAGJUblNhVjQEOg43WDlTYlRgBDQAOltpVmdWbFA4A24HNgozUzdXZFFlBFoFdwNoBHtdbQk4UWJRPgcPUnJRbwNzB1sBbQEwBmVaIAQ2VXMAIVQKUyFWPQR7Dj5YaVNiVDsEDAAgWz9WYVZ7UG4DZAcuCjVTN1csUTEEMgVjAw8EKl1sCXpRaFE1B2NSalFaAyEHcQF%2FATEGc1oNBDJVZwA3VDhTd1ZaBCoOP1grU2pUMARhADNbNlYKVndQEAM5B3oKa1NoV25RYgQpBWADfQQ4XXcJIVEFUW0HPVI0UWkDdAd3ASwBGgZVWnIEYlUwAHxUZlM4ViYEXw5tWDZTZlQ1BGsAIlt0VmBWYVB0A3YHQQpyU3RXblFmBFEFMAMxBENdPgl9UX1RMQdgUmdRKAMwBzIBLAF8BkpaGgQHVU0AHlR6UyNWagRhDm9YPVNwVEYENQBhW2dWOVZ8UH0DFQdoCnBTa1dvUWYEKQVkA2YEP115CTlRfFE0B31SYFEmA1AHZQFqATUGc1o7BHxVNQBhVGFTLVY1BD4OWFh6U2tUcgRqADNbP1ZsVglQfwM6B2EKcVNwV11RYgRlBSEDOgR%2BXT4JfVErUVgHcVJrUW8DOQc1ATsBbQY1WmcEZFU4AGZUYFM3Vj0EdQ%3D%3D"""

cookies = {}
for item in raw_header.split(";"):
    if "=" in item:
        k, v = item.strip().split("=", 1)
        cookies[k.strip()] = v.strip()

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

client = httpx.Client(base_url="https://v3bl.goszakup.gov.kz", cookies=cookies, headers=headers, follow_redirects=False, timeout=12.0)
for url in ["/cabinet/profile", "/ru/cabinet/profile", "/ru/searchanno"]:
    r = client.get(url)
    print(url, "STATUS:", r.status_code, "LOC:", r.headers.get("location"))
    print("SET-COOKIE:", r.headers.get("set-cookie"))
