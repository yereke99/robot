"""Скрипт для поиска числовых кодов iik и subject_address на goszakup.

Запуск:
    .\.venv\Scripts\python tools/find_iik_codes.py 17027506
"""
import asyncio
import re
import sys
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from goszakup import secrets as gz_secrets


async def main(announce_id: str):
    config_path = Path("config.yaml")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    # Получаем cookie сессии
    ci_service = raw["goszakup"].get("ci_session_service", "atagos-cookie")
    cookie = gz_secrets.get(ci_service, "default")
    if not cookie:
        print("❌ Cookie сессии не найдена. Сначала запусти бота и выполни /login")
        return

    headers = {
        "Cookie": f"ci_session={cookie}",
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    async with httpx.AsyncClient(base_url="https://v3bl.goszakup.gov.kz",
                                  follow_redirects=True, timeout=15) as client:
        print(f"Загружаю страницу создания заявки для тендера {announce_id}...")
        r = await client.get(f"/ru/application/create/{announce_id}", headers=headers)
        html = r.text

        # Ищем subject_address опции
        print("\n=== subject_address (юр. адрес) ===")
        matches = re.findall(
            r'<option\s+value="(\d+)"[^>]*>([^<]+)</option>',
            html
        )
        addr_block = re.search(
            r'name="subject_address".*?</select>', html, re.DOTALL
        )
        if addr_block:
            addr_options = re.findall(
                r'<option\s+value="(\d+)"[^>]*>([^<]*)</option>',
                addr_block.group(0)
            )
            for val, label in addr_options:
                marker = " ← ВЫБЕРИ ЭТОТ" if "751410" in label or "Алматы" in label.lower() or "Бостандык" in label.lower() else ""
                print(f"  {val:>10}  {label.strip()}{marker}")
        else:
            print("  Не найдено. Возможно сессия устарела — выполни /login")

        # Ищем iik опции
        print("\n=== iik (банковский счёт) ===")
        iik_block = re.search(
            r'name="iik".*?</select>', html, re.DOTALL
        )
        if iik_block:
            iik_options = re.findall(
                r'<option\s+value="(\d+)"[^>]*>([^<]*)</option>',
                iik_block.group(0)
            )
            for val, label in iik_options:
                marker = " ← ВЫБЕРИ ЭТОТ" if "KZ536" in label or "Народный" in label else ""
                print(f"  {val:>10}  {label.strip()}{marker}")
        else:
            print("  Не найдено. Возможно сессия устарела — выполни /login")

        print("\n✅ Скопируй нужные числовые значения в config.yaml")
        print("   company.subject_address: \"ЧИСЛО\"")
        print("   company.iik: \"ЧИСЛО\"")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python tools/find_iik_codes.py <announce_id>")
        print("Пример:        python tools/find_iik_codes.py 17027506")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
