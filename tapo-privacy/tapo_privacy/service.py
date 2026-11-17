"""HTTP-обёртка над pytapo: включение и выключение приватного режима камер Tapo.

Работает в двух режимах:

1. Как аддон Home Assistant — камеры берутся из /data/options.json.
2. Как обычный процесс — камеры берутся из переменной TAPO_CAMERAS.

    TAPO_CAMERAS='{"kitchen": {"host": "192.168.1.50", "password": "secret"}}' \
    python3 service.py --port 8099

API:
    POST /privacy?camera=kitchen&state=on
    POST /privacy?camera=kitchen&state=off
    GET  /status?camera=kitchen
    GET  /status                       — все камеры
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pytapo import Tapo


def load_cameras() -> dict[str, dict]:
    """Собрать словарь камер из переменной окружения или из options.json аддона."""
    raw = os.environ.get("TAPO_CAMERAS")
    if raw:
        return json.loads(raw)

    options_path = Path(os.environ.get("TAPO_OPTIONS", "/data/options.json"))
    if options_path.exists():
        options = json.loads(options_path.read_text(encoding="utf-8"))
        cameras = options.get("cameras") or []
        return {item["name"]: item for item in cameras if item.get("host")}

    return {}


def load_option(key: str, default=None):
    """Взять значение из options.json аддона, если файл есть."""
    options_path = Path(os.environ.get("TAPO_OPTIONS", "/data/options.json"))
    if not options_path.exists():
        return default
    return json.loads(options_path.read_text(encoding="utf-8")).get(key, default)


def load_timeout() -> float:
    """Таймаут обращения к камере: переменная окружения или настройка аддона."""
    if os.environ.get("TAPO_TIMEOUT"):
        return float(os.environ["TAPO_TIMEOUT"])
    return float(load_option("timeout", 10))


CAMERAS: dict[str, dict] = load_cameras()
TIMEOUT: float = load_timeout()
TOKEN: str = os.environ.get("TAPO_TOKEN") or str(load_option("token", "") or "")
CLIENTS: dict[str, Tapo] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tapo")


def call_with_timeout(function, *args):
    """Выполнить запрос к камере с ограничением по времени.

    pytapo не принимает таймаут наружу, а камера может просто не отвечать:
    TCP-коннект к выключенному устройству висит до минуты и больше.
    """
    future = EXECUTOR.submit(function, *args)
    try:
        return future.result(timeout=TIMEOUT)
    except FutureTimeout as error:
        raise TimeoutError(f"камера не ответила за {TIMEOUT:.0f} с") from error


def get_client(name: str) -> Tapo:
    """Вернуть (и закешировать) клиента pytapo для камеры."""
    if name not in CAMERAS:
        raise KeyError(name)
    if name not in CLIENTS:
        config = CAMERAS[name]
        CLIENTS[name] = Tapo(
            config["host"],
            config.get("user", "admin"),
            config["password"],
            cloudPassword=config.get("cloud_password", ""),
        )
    return CLIENTS[name]


def _privacy_state(client: Tapo) -> dict:
    current = call_with_timeout(client.getPrivacyMode)
    return {"privacy_enabled": current.get("enabled") == "on"}


def set_privacy(name: str, enabled: bool) -> dict:
    """Включить или выключить приватный режим и вернуть фактическое состояние."""
    client = call_with_timeout(get_client, name)
    call_with_timeout(client.setPrivacyMode, enabled)
    return {"camera": name, **_privacy_state(client)}


def read_privacy(name: str) -> dict:
    """Прочитать текущее состояние приватного режима."""
    client = call_with_timeout(get_client, name)
    return {"camera": name, **_privacy_state(client)}


class Handler(BaseHTTPRequestHandler):
    server_version = "tapo-privacy/1.0"

    def _authorized(self) -> bool:
        """Проверить токен, если он задан в настройках."""
        if not TOKEN:
            return True
        if self.headers.get("X-Auth-Token") == TOKEN:
            return True
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "bad or missing token"})
            return
        parsed = urlparse(self.path)
        if parsed.path != "/status":
            self._send(404, {"error": "unknown path"})
            return
        query = parse_qs(parsed.query)
        names = query.get("camera") or list(CAMERAS)
        result = []
        for name in names:
            try:
                result.append(read_privacy(name))
            except KeyError:
                result.append({"camera": name, "error": "unknown camera"})
            except Exception as error:  # noqa: BLE001
                result.append({"camera": name, "error": str(error)})
        self._send(200, {"data": result})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "bad or missing token"})
            return
        parsed = urlparse(self.path)
        if parsed.path != "/privacy":
            self._send(404, {"error": "unknown path"})
            return
        query = parse_qs(parsed.query)
        name = (query.get("camera") or [""])[0]
        state = (query.get("state") or [""])[0].lower()
        if state not in ("on", "off"):
            self._send(400, {"error": "state must be on or off"})
            return
        try:
            self._send(200, set_privacy(name, state == "on"))
        except KeyError:
            self._send(404, {"error": f"unknown camera: {name}"})
        except Exception as error:  # noqa: BLE001
            CLIENTS.pop(name, None)
            self._send(502, {"camera": name, "error": str(error)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Приватный режим камер Tapo по HTTP.")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args(argv)

    if not CAMERAS:
        print("Не заданы камеры: ни TAPO_CAMERAS, ни /data/options.json", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"tapo-privacy слушает {args.bind}:{args.port}, камеры: {', '.join(CAMERAS)}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
