#!/usr/bin/env python3
"""Refresh VK_ACCESS_TOKEN: opens the OAuth page and writes the fresh token to .env."""
import re
import sys
import webbrowser
from pathlib import Path

AUTH_URL = (
    "https://oauth.vk.com/authorize?client_id=6287487&display=page"
    "&redirect_uri=https://oauth.vk.com/blank.html&scope=audio"
    "&response_type=token&v=5.131&revoke=1"
)
ENV_PATH = Path(__file__).parent / ".env"

TOKEN_RE = re.compile(r"access_token=([^&\s]+)")
EXPIRES_RE = re.compile(r"expires_in=(\d+)")


def main() -> None:
    print("Открываю браузер. Разреши доступ и скопируй итоговую ссылку (oauth.vk.ru/blank.html#...).")
    webbrowser.open(AUTH_URL)
    url = input("Вставь ссылку из адресной строки: ").strip()

    m = TOKEN_RE.search(url)
    if not m:
        print("Не нашёл access_token в строке.")
        sys.exit(1)
    token = m.group(1)
    expires = EXPIRES_RE.search(url)
    expires_s = int(expires.group(1)) if expires else None

    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out = [ln for ln in lines if not ln.startswith("VK_ACCESS_TOKEN=")]
    out.append(f"VK_ACCESS_TOKEN={token}")
    ENV_PATH.write_text("\n".join(out) + "\n")

    if expires_s == 0:
        print("Токен записан (бессрочный).")
    else:
        print(f"Токен записан (живёт {expires_s // 3600} ч).")


if __name__ == "__main__":
    main()
