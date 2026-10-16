import argparse
import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import mimetypes
import os
import shutil
import socket
import ssl
import subprocess
import warnings
from pathlib import Path
from time import perf_counter
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
import truststore
from dotenv import set_key
from PIL import Image, UnidentifiedImageError

from database import ROOT, Settings, get_settings

log = logging.getLogger(__name__)


class MaxError(Exception):
    def __init__(self, status: int = 0, retry_after: float = 0, code: str = ""):
        self.status = status
        self.retry_after = retry_after
        self.code = code
        super().__init__(f"MAX request failed (HTTP {status})")


def tls_context(settings: Settings):
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if settings.max_ca_bundle:
        context.load_verify_locations(cafile=settings.max_ca_bundle)
    return context


class MaxClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.request_lock = asyncio.Lock()
        self.next_request = 0
        self.last_request = 0
        self.http = httpx.AsyncClient(
            base_url=settings.max_api_url,
            timeout=httpx.Timeout(25, connect=10),
            limits=httpx.Limits(keepalive_expiry=300),
            headers={"Authorization": settings.max_bot_token.get_secret_value()},
            verify=tls_context(settings),
            follow_redirects=False,
            transport=transport,
        )
        self.media = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(keepalive_expiry=300),
            verify=tls_context(settings),
            follow_redirects=False,
            transport=transport,
        )

    @staticmethod
    def checked(response):
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            raise MaxError(response.status_code, code="invalid_response")
        if response.is_error or data.get("success") is False:
            try:
                delay = min(3600, max(0, float(response.headers.get("retry-after", "0"))))
            except ValueError:
                delay = 0
            raise MaxError(response.status_code, delay, str(data.get("code", ""))[:100])
        if response.status_code not in (200, 201, 204):
            raise MaxError(response.status_code)
        return data

    async def request(self, method, path, **kwargs):
        if not self.settings.api_enabled:
            raise MaxError(0, 60, "token_not_configured")
        async with self.request_lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.next_request - loop.time()))
            self.next_request = loop.time() + 1 / 30
        starts, network = {}, {}

        async def trace(name, info):
            phase, _, stage = name.rpartition(".")
            if phase in ("connection.connect_tcp", "connection.start_tls"):
                if stage == "started":
                    starts[phase] = perf_counter()
                elif stage == "complete" and phase in starts:
                    network[phase] = (perf_counter() - starts[phase]) * 1000

        extensions = {**kwargs.pop("extensions", {})}
        extensions.setdefault("trace", trace)
        started = self.last_request = perf_counter()
        try:
            response = await self.http.request(method, path, extensions=extensions, **kwargs)
        finally:
            self.last_request = perf_counter()
        log.info(
            "MAX %s %s: status=%s elapsed_ms=%.0f tcp_ms=%.0f tls_ms=%.0f",
            method,
            path,
            response.status_code,
            (perf_counter() - started) * 1000,
            network.get("connection.connect_tcp", 0),
            network.get("connection.start_tls", 0),
        )
        return self.checked(response)

    async def keepalive(self, interval=20):
        delay = 0
        while self.settings.api_enabled:
            await asyncio.sleep(delay)
            idle = perf_counter() - self.last_request
            if idle < interval:
                delay = interval - idle
                continue
            try:
                async with asyncio.timeout(5):
                    await self.request("GET", "/me", timeout=httpx.Timeout(5, connect=3))
            except (TimeoutError, httpx.HTTPError, MaxError) as exc:
                delay = max(interval, getattr(exc, "retry_after", 0))
                log.warning("MAX keepalive: %s", type(exc).__name__)
            else:
                delay = interval

    async def send(self, body, *, chat_id=None, user_id=None):
        if (chat_id is None) == (user_id is None):
            raise ValueError("Exactly one recipient required")
        params = {"chat_id": chat_id} if chat_id is not None else {"user_id": user_id}
        data = await self.request("POST", "/messages", params=params, json=body)
        mid = data.get("message", {}).get("body", {}).get("mid")
        if not mid:
            raise MaxError(200, code="missing_message_id")
        return str(mid)

    async def answer(self, callback_id, notification=None, message=None):
        body = {}
        if notification:
            body["notification"] = notification
        if message is not None:
            body["message"] = message
        data = await self.request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json=body,
        )
        if data.get("success") is not True:
            raise MaxError(200, code="answer_not_confirmed")
        log.info(
            "Callback %s accepted: mode=%s",
            hashlib.sha256(("callback:" + callback_id).encode()).hexdigest()[:12],
            "update" if message is not None else "notice",
        )

    async def edit(self, message_id, body):
        data = await self.request("PUT", "/messages", params={"message_id": message_id}, json=body)
        if data.get("success") is not True:
            raise MaxError(200, code="edit_not_confirmed")

    async def upload_image(self, path: Path, mime: str):
        slot = await self.request("POST", "/uploads", params={"type": "image"})
        url = slot["url"]
        await validate_media_url(url, self.settings)
        filename = "image" + (mimetypes.guess_extension(mime) or ".jpg")
        with path.open("rb") as f:
            response = await self.media.post(url, files={"data": (filename, f, mime)})
        data = self.checked(response)
        # Official schema: multipart image upload returns PhotoTokens, used as payload.photos.
        if isinstance(data.get("photos"), dict) and data["photos"]:
            photos = data["photos"]
            if all(isinstance(v, dict) and v.get("token") for v in photos.values()):
                return {"photos": photos}
        if isinstance(data.get("token"), str) and data["token"]:
            return {"token": data["token"]}
        raise MaxError(200, code="invalid_upload_response")

    async def close(self):
        await self.http.aclose()
        await self.media.aclose()


PHOTO_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "TIFF": ".tiff", "BMP": ".bmp"}


class InvalidPhoto(ValueError):
    pass


async def validate_media_url(url: str, settings: Settings):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = [s.strip().lower() for s in settings.photo_allowed_hosts.split(",") if s.strip()]
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not any(host == suffix or host.endswith("." + suffix) for suffix in allowed)
    ):
        raise InvalidPhoto("Недопустимый адрес сервера фотографии")
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise InvalidPhoto("Недопустимый сетевой адрес")


class PhotoStore:
    def __init__(self, settings: Settings, media):
        self.settings = settings
        self.root = settings.photo_dir
        if not self.root.is_absolute():
            self.root = ROOT / self.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.media = media
        self.uploaded_images = {}

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()) or path == self.root.resolve():
            raise InvalidPhoto("Некорректный путь")
        return path

    def verify(self, reference: dict) -> Path:
        path = self.path(reference["private_path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference["checksum"]:
            raise InvalidPhoto("Контрольная сумма сохранённого фото не совпадает")
        return path

    async def receive(self, attachment: dict) -> dict:
        if attachment.get("type") != "image":
            raise InvalidPhoto("Нужна одна фотография, отправленная как изображение")
        url = attachment.get("payload", {}).get("url")
        if not isinstance(url, str):
            raise InvalidPhoto("У фотографии отсутствует URL")
        data = bytearray()
        for _ in range(4):
            await validate_media_url(url, self.settings)
            async with self.media.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if not mime.startswith("image/"):
                    raise InvalidPhoto("Сервер вернул не изображение")
                size = response.headers.get("content-length")
                if size and int(size) > self.settings.photo_max_bytes:
                    raise InvalidPhoto("Фотография слишком большая (максимум 10 МБ)")
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.settings.photo_max_bytes:
                        raise InvalidPhoto("Фотография слишком большая (максимум 10 МБ)")
                break
        else:
            raise InvalidPhoto("Слишком много перенаправлений")
        return await asyncio.to_thread(self.save, bytes(data), mime)

    def save(self, data: bytes, declared_mime: str) -> dict:
        if len(data) > self.settings.photo_max_bytes:
            raise InvalidPhoto("Фотография слишком большая")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                    if max(width, height) > 7680 or width * height > 40_000_000:
                        raise InvalidPhoto("Слишком большое разрешение фотографии")
                    fmt = image.format
                    actual_mime = Image.MIME.get(fmt, "")
                    if fmt not in PHOTO_EXTENSIONS and fmt != "WEBP":
                        raise InvalidPhoto("Формат не поддерживается. Отправьте JPEG или PNG")
                    if actual_mime != declared_mime:
                        raise InvalidPhoto("Тип содержимого не совпадает с форматом фотографии")
                    image.verify()
                source_mime = actual_mime
                if fmt == "WEBP":
                    # MAX's image CDN can return WebP. Keep a lossless PNG copy that
                    # can also be uploaded through the documented MAX image endpoint.
                    converted = io.BytesIO()
                    with Image.open(io.BytesIO(data)) as image:
                        image.save(converted, format="PNG")
                    data = converted.getvalue()
                    if len(data) > self.settings.photo_max_bytes:
                        raise InvalidPhoto(
                            "После обработки фотография слишком большая. Отправьте JPEG до 10 МБ"
                        )
                    fmt, actual_mime = "PNG", "image/png"
        except (
            UnidentifiedImageError,
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise InvalidPhoto("Повреждённая или неподдерживаемая фотография") from exc
        checksum = hashlib.sha256(data).hexdigest()
        relative = f"{checksum}{PHOTO_EXTENSIONS[fmt]}"
        path = self.path(relative)
        if not path.exists():
            temporary = self.path(f"{uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "private_path": relative,
            "checksum": checksum,
            "media_metadata": {
                "mime": actual_mime,
                "source_mime": source_mime,
                "width": width,
                "height": height,
                "size": len(data),
            },
        }


async def certificates(settings, args):
    bundle = Path(args.bundle) if args.bundle else ROOT / "private/certs/max_ca_bundle.pem"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    parts, manifest = [], []
    async with httpx.AsyncClient(
        timeout=20, verify=tls_context(Settings(max_ca_bundle=""))
    ) as client:
        for kind, name in (("root", "Root"), ("sub", "Sub")):
            url = f"https://gu-st.ru/content/lending/russian_trusted_{kind}_ca_pem.crt"
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 100_000:
                raise ValueError("Unexpected certificate download")
            der = ssl.PEM_cert_to_DER_cert(response.text)
            path = bundle.parent / f"{kind}.crt"
            path.write_bytes(response.content)
            cert = ssl._ssl._test_decode_cert(str(path))
            subject = dict(item for group in cert["subject"] for item in group)
            if subject.get("commonName") != f"Russian Trusted {name} CA":
                raise ValueError("Unexpected certificate subject")
            parts.append(response.content)
            manifest.append(
                dict(source=url, sha256=hashlib.sha256(der).hexdigest(), expires=cert["notAfter"])
            )
    bundle.write_bytes(b"\n".join(parts))
    bundle.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not args.download_only:
        configured = settings.model_copy(update={"max_ca_bundle": str(bundle)})
        async with httpx.AsyncClient(verify=tls_context(configured), timeout=15) as client:
            if (await client.get(settings.max_api_url + "/me")).status_code != 401:
                raise ValueError("Unexpected unauthenticated MAX response")
        set_key(ROOT / ".env", "MAX_CA_BUNDLE", str(bundle))
    print("Application CA bundle prepared; system certificate stores unchanged.")


async def subscriptions(settings, args):
    client = MaxClient(settings)
    try:
        me = await client.request("GET", "/me")
        items = (await client.request("GET", "/subscriptions")).get("subscriptions", [])
        print("Bot:", me.get("user_id"), me.get("username"))
        for item in items:
            print("Subscription:", item.get("url"), item.get("update_types"))
        if args.command == "subscriptions":
            print("Count:", len(items))
            return
        if not args.confirm:
            raise ValueError("Review subscriptions and use --confirm for this action")
        url = args.url or settings.webhook_url
        Settings.model_validate({**settings.model_dump(), "webhook_url": url})
        if not url:
            raise ValueError("Specify a full HTTPS /webhook URL")
        if args.command == "unregister":
            if url != settings.webhook_url:
                raise ValueError("Only the exact local WEBHOOK_URL can be removed")
            await client.request("DELETE", "/subscriptions", params={"url": url})
        else:
            if any(item.get("url") != url for item in items):
                raise ValueError("A different subscription exists; no change performed")
            if not settings.webhook_secret.get_secret_value():
                raise ValueError("WEBHOOK_SECRET is required")
            async with httpx.AsyncClient(
                verify=tls_context(settings), timeout=httpx.Timeout(15, connect=3)
            ) as http:
                try:
                    health = await http.get(url.removesuffix("/webhook") + "/health")
                except (httpx.ConnectError, httpx.TimeoutException):
                    if not await asyncio.to_thread(Path("/.dockerenv").exists):
                        raise
                    # Docker may not route the server's public IP back into its own bridge.
                    host = urlsplit(url).hostname
                    health = await http.get(
                        "https://caddy/health",
                        headers={"Host": host},
                        extensions={"sni_hostname": host},
                    )
                if health.status_code != 200 or health.json() != {"status": "ok"}:
                    raise ValueError("Public health check failed")
            await client.request(
                "POST",
                "/subscriptions",
                json=dict(
                    url=url,
                    secret=settings.webhook_secret.get_secret_value(),
                    update_types=["bot_started", "message_created", "message_callback"],
                ),
            )
        after = (await client.request("GET", "/subscriptions")).get("subscriptions", [])
        present = any(item.get("url") == url for item in after)
        if present != (args.command == "register"):
            raise ValueError("Subscription change was not verified")
        if present and url != settings.webhook_url:
            set_key(ROOT / ".env", "WEBHOOK_URL", url)
        print("Verified:", args.command, url)
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MAX administration; secrets come only from .env")
    parser.add_argument(
        "command", choices=["subscriptions", "register", "unregister", "ca", "tunnel"]
    )
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--bundle")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    try:
        settings = get_settings()
        if args.command == "tunnel":
            exe = (
                shutil.which("cloudflared") or r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
            )
            subprocess.run(
                [
                    exe,
                    "tunnel",
                    "--url",
                    f"http://127.0.0.1:{settings.app_port}",
                    "--protocol",
                    "quic",
                    "--no-autoupdate",
                    "--no-prechecks",
                    "--edge",
                    "region1.v2.argotunnel.com:7844",
                    "--edge",
                    "region2.v2.argotunnel.com:7844",
                ],
                check=True,
            )
        elif args.command == "ca":
            asyncio.run(certificates(settings, args))
        else:
            asyncio.run(subscriptions(settings, args))
    except Exception as exc:
        from database import safe_error

        print(str(exc) if isinstance(exc, ValueError) else safe_error(exc))
        raise SystemExit(1) from None
