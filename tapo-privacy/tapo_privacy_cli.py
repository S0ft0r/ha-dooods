"""Проверка приватного режима одной камеры Tapo из командной строки.

Нужен ровно один вызов, чтобы понять, поддерживает ли камера lens mask:

    python3 tapo_privacy_cli.py --host 192.168.1.50 --password 'пароль' --state status

Пароль — тот, что задан в приложении Tapo: Настройки → Дополнительно →
Учётная запись камеры. Если приходит ошибка -40211 или «Invalid authentication
data», передайте тот же пароль через --cloud-password.
"""

from __future__ import annotations

import argparse
import socket
import sys

from pytapo import Tapo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Приватный режим камеры Tapo.")
    parser.add_argument("--host", required=True, help="IP-адрес камеры")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True, help="пароль учётной записи камеры")
    parser.add_argument("--cloud-password", default="", help="пароль аккаунта TP-Link")
    parser.add_argument(
        "--state",
        default="status",
        choices=("on", "off", "status"),
        help="что сделать (по умолчанию только прочитать состояние)",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    # pytapo не даёт выставить таймаут: без этого выключенная камера держит
    # TCP-коннект до минуты.
    socket.setdefaulttimeout(args.timeout)

    tapo = Tapo(args.host, args.user, args.password, cloudPassword=args.cloud_password)

    if args.state == "status":
        info = tapo.getPrivacyMode()
        print(f"приватный режим: {info.get('enabled')}")
        return 0

    tapo.setPrivacyMode(args.state == "on")
    info = tapo.getPrivacyMode()
    print(f"приватный режим: {info.get('enabled')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"ошибка: {error}", file=sys.stderr)
        raise SystemExit(1) from error
