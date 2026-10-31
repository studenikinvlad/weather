"""Private, process-local Xray tunnel for all bot network clients."""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import secrets
import socket
import subprocess
import urllib.request
import zipfile
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

import aiohttp
from aiohttp_socks import ProxyConnector

from city_folders.models import PilotError

PROXY = ContextVar("snowaa_proxy", default=None)
RELEASE = "v26.3.27"
ARCHIVE_SHA256 = (
    "d004c39288ce9ada487c6f398c7c545f7d749e44bdfdd59dbc9f865afba4e1ad"
)
logger = logging.getLogger(__name__)


def http_connector():
    url = PROXY.get()
    return ProxyConnector.from_url(url, rdns=True) if url else None


def telegram_proxy():
    url = PROXY.get()
    if not url:
        return None
    parsed = urlsplit(url)
    return dict(
        proxy_type="socks5",
        addr=parsed.hostname,
        port=parsed.port,
        username=parsed.username,
        password=parsed.password,
        rdns=True,
    )


def subscription_nodes(raw):
    try:
        text = raw.decode("utf-8-sig").strip()
        if not text.startswith("vless://"):
            text = base64.b64decode(text + "=" * (-len(text) % 4)).decode()
        nodes = []
        for line in text.splitlines():
            if not line.startswith("vless://"):
                continue
            p = urlsplit(line.strip())
            q = {k: v[0] for k, v in parse_qs(p.query).items()}
            if q.get("type") != "grpc" or q.get("security") != "tls":
                continue
            if not p.hostname or not p.port or not p.username:
                continue
            if q.get("encryption", "none") != "none":
                continue
            if q.get("allowInsecure", "0") not in {"0", "false"}:
                raise ValueError("Unsafe TLS settings")
            nodes.append(
                {
                    "protocol": "vless",
                    "settings": {
                        "vnext": [
                            {
                                "address": p.hostname,
                                "port": p.port,
                                "users": [
                                    {
                                        "id": str(UUID(unquote(p.username))),
                                        "encryption": "none",
                                    }
                                ],
                            }
                        ]
                    },
                    "streamSettings": {
                        "network": "grpc",
                        "security": "tls",
                        "grpcSettings": {
                            "serviceName": q.get("serviceName", ""),
                            "authority": q.get("authority", ""),
                            "multiMode": q.get("mode") == "multi",
                        },
                        "tlsSettings": {
                            "serverName": q.get("sni", p.hostname),
                            "fingerprint": q.get("fp", "chrome"),
                            "alpn": q.get("alpn", "h2").split(","),
                            "allowInsecure": False,
                        },
                    },
                }
            )
        if not nodes:
            raise ValueError("No compatible nodes")
        return nodes
    except Exception:
        raise PilotError(
            "Подписка не содержит корректных VLESS/gRPC/TLS узлов."
        ) from None


def fetch_subscription(url):
    if urlsplit(url).scheme != "https":
        raise PilotError("Подписка Xray должна использовать HTTPS.")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "v2rayN"})
        with urllib.request.urlopen(request, timeout=25) as response:
            if urlsplit(response.url).scheme != "https":
                raise ValueError("Insecure redirect")
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Subscription too large")
        return subscription_nodes(raw)
    except PilotError:
        raise
    except Exception:
        raise PilotError(
            "Не удалось получить подписку Xray. Проверь сеть и ссылку в .env."
        ) from None


def install_xray(directory):
    custom = os.getenv("XRAY_EXECUTABLE", "").strip()
    if custom:
        binary = Path(custom).expanduser().resolve()
        if not binary.is_file():
            raise PilotError("Файл XRAY_EXECUTABLE не найден.")
        return binary
    binary = directory / RELEASE / "xray.exe"
    if binary.is_file():
        return binary
    if os.name != "nt":
        raise PilotError("Укажи путь к Xray в XRAY_EXECUTABLE.")
    url = f"https://github.com/XTLS/Xray-core/releases/download/{RELEASE}/Xray-windows-64.zip"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read(60_000_001)
        if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
            raise ValueError("Checksum mismatch")
        binary.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            # Extract only the executable; no archive paths are trusted.
            contents = archive.read("xray.exe")
        temporary = binary.with_suffix(".tmp")
        temporary.write_bytes(contents)
        temporary.replace(binary)
        return binary
    except Exception:
        raise PilotError(
            "Не удалось установить проверенную версию Xray с GitHub."
        ) from None


def tunnel_config(node, port, username, password):
    return {
        "log": {"loglevel": "none"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "socks",
                "settings": {
                    "auth": "password",
                    "accounts": [{"user": username, "pass": password}],
                    "udp": False,
                },
            }
        ],
        "outbounds": [node],
    }


async def stop_process(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)


@asynccontextmanager
async def xray_tunnel(data_dir):
    enabled = os.getenv("XRAY_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise PilotError("XRAY_ENABLED: true или false.")
    if enabled == "false":
        yield
        return
    url = os.getenv("XRAY_SUBSCRIPTION_URL", "").strip()
    if not url:
        raise PilotError("Заполни XRAY_SUBSCRIPTION_URL в .env.")
    directory = data_dir / "xray"
    binary = await asyncio.to_thread(install_xray, directory)
    nodes = await asyncio.to_thread(fetch_subscription, url)
    process, token, config_file = None, None, directory / "runtime.json"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        for index, node in enumerate(nodes):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            username, password = secrets.token_hex(12), secrets.token_hex(24)
            config_file.write_text(
                json.dumps(tunnel_config(node, port, username, password)),
                encoding="utf-8",
            )
            if os.name != "nt":
                config_file.chmod(0o600)
            process = await asyncio.create_subprocess_exec(
                str(binary),
                "run",
                "-config",
                str(config_file),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0,
            )
            proxy = f"socks5://{username}:{password}@127.0.0.1:{port}"
            ready = False
            for _ in range(30):
                if process.returncode is not None:
                    break
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", port), timeout=0.5
                    )
                    writer.close()
                    await writer.wait_closed()
                    ready = True
                    break
                except (OSError, TimeoutError):
                    await asyncio.sleep(0.1)
            if ready:
                try:
                    async with aiohttp.ClientSession(
                        connector=ProxyConnector.from_url(proxy, rdns=True),
                        timeout=aiohttp.ClientTimeout(total=12),
                    ) as session:
                        async with session.get(
                            "https://api.telegram.org", allow_redirects=False
                        ) as response:
                            ready = response.status in {200, 301, 302}
                except Exception:
                    ready = False
            if ready:
                token = PROXY.set(proxy)
                logger.warning(
                    "Xray подключён: узел %s, VLESS/gRPC/TLS.", index + 1
                )
                break
            await stop_process(process)
            process = None
        if token is None:
            raise PilotError(
                "Узлы Xray недоступны. Прямое подключение отключено."
            )
        try:
            yield
        finally:
            PROXY.reset(token)
    finally:
        if process is not None:
            await stop_process(process)
        config_file.unlink(missing_ok=True)
