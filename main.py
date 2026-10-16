import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from functools import partial
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from bot import director_card, parse_update, route, verify_webhook
from database import (
    SCHEMA_VERSION,
    Database,
    IncomingEvent,
    Outbox,
    ProcessedEvent,
    Repair,
    RepairPhoto,
    User,
    configure_logging,
    get_settings,
    migrate,
    next_job,
    now,
    safe_error,
)
from gigachat_api import GigaChat
from max_api import MaxClient, MaxError, PhotoStore

log = logging.getLogger(__name__)


class DeliveryNotEnabled(Exception):
    pass


def retry_job(item, exc):
    item.attempts += 1
    item.last_error = safe_error(exc)
    delay = max(getattr(exc, "retry_after", 0), min(3600, 2 ** min(item.attempts, 12)))
    item.next_retry_at = now() + timedelta(seconds=delay + random.random())
    if isinstance(item, IncomingEvent) and item.attempts >= 10:
        item.status = "failed"
    log.warning("%s %s attempt %s: %s", item.__tablename__, item.id, item.attempts, item.last_error)


async def process_incoming(db, settings, client, photos):
    async with db.sessions() as session, session.begin():
        item = await next_job(session, IncomingEvent)
        if item is None:
            return False
        try:
            async with session.begin_nested():
                if not await session.get(ProcessedEvent, item.idempotency_key):
                    await route(session, parse_update(item.payload), settings, photos)
                    session.add(ProcessedEvent(idempotency_key=item.idempotency_key))
                    await session.flush()
            item.status, item.payload, item.last_error = "processed", None, None
            log.info(
                "Inbox %s processed: age_ms=%.0f",
                item.id,
                (now() - item.created_at).total_seconds() * 1000,
            )
        except Exception as exc:
            retry_job(item, exc)
        return True


async def throttle(session, item):
    if item.recipient_user_id is not None:
        user_id = item.recipient_user_id
        chat_id = await session.scalar(
            select(User.chat_id).where(
                User.max_user_id == user_id, User.private_chat_verified.is_(True)
            )
        )
    else:
        chat_id = item.recipient_chat_id
        user_id = await session.scalar(
            select(User.max_user_id)
            .where(User.chat_id == chat_id, User.private_chat_verified.is_(True))
            .limit(1)
        )
    key = f"chat:{chat_id}" if chat_id is not None else f"user:{user_id}"
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": "max-rate:" + key},
    )
    if not locked:
        item.next_retry_at = now() + timedelta(seconds=0.2)
        return False
    sent = await session.scalar(
        select(func.max(Outbox.sent_at)).where(
            Outbox.status == "sent",
            or_(
                Outbox.recipient_chat_id == chat_id if chat_id is not None else False,
                Outbox.recipient_user_id == user_id if user_id is not None else False,
            ),
        )
    )
    if sent and sent + timedelta(seconds=0.6) > now():
        item.next_retry_at = sent + timedelta(seconds=0.6)
        return False
    return True


async def message_body(payload, photo, cache, client, photos):
    body = {key: value for key, value in payload.items() if not key.startswith("_")}
    if photo:
        path = await asyncio.to_thread(photos.verify, photo)
        if cache.get("checksum") != photo["checksum"] or not cache.get("payload"):
            cache.update(
                checksum=photo["checksum"],
                payload=await client.upload_image(path, photo["media_metadata"]["mime"]),
            )
        body["attachments"] = [{"type": "image", "payload": cache["payload"]}] + body.get(
            "attachments", []
        )
    return body


async def deliver(session, item, cache, settings, client, photos):
    repair = None
    if item.kind == "answer":
        body = None
    elif item.kind in ("director_card", "director_sync"):
        director = await session.scalar(
            select(User).where(User.max_user_id == settings.director_user_id)
        )
        if (
            not settings.director_delivery_enabled
            or not director
            or not director.private_chat_verified
            or not director.started_at
            or not director.id_seen_at
            or director.chat_id != settings.director_chat_id
            or item.recipient_chat_id != settings.director_chat_id
        ):
            raise DeliveryNotEnabled()
        repair = await session.get(Repair, item.repair_id, with_for_update=True)
        photo = await session.scalar(select(RepairPhoto).where(RepairPhoto.repair_id == repair.id))
        if not photo:
            raise ValueError("Repair photo missing")
        author = await session.get(User, repair.author_id)
        body = await message_body(
            director_card(repair, author, settings.app_timezone),
            dict(
                private_path=photo.private_path,
                checksum=photo.checksum,
                media_metadata=photo.media_metadata,
            ),
            cache,
            client,
            photos,
        )
        if item.kind == "director_sync" and not repair.director_message_id:
            raise DeliveryNotEnabled()
    else:
        body = await message_body(item.payload, item.payload.get("_photo"), cache, client, photos)
    # Uploads must not hold the dialog's send lock and delay callback acknowledgements.
    if not await throttle(session, item):
        return False
    if item.kind == "answer":
        await client.answer(**item.payload)
        return None
    if item.kind == "director_sync":
        await client.edit(repair.director_message_id, body)
        return repair.director_message_id
    recipient = {"chat_id": item.recipient_chat_id}
    if not repair:
        recipient["user_id"] = item.recipient_user_id
    mid = await client.send(body, **recipient)
    if repair:
        repair.director_message_id = mid
    return mid


async def deliver_one(db, settings, client, photos, *, answer_only=None):
    if not settings.api_enabled:
        return False
    async with db.sessions() as session, session.begin():
        item = await next_job(session, Outbox, answer_only=answer_only)
        if item is None:
            return False
        started = perf_counter()
        cache = dict(item.payload.get("_uploaded_image") or {})
        try:
            async with session.begin_nested():
                mid = await deliver(session, item, cache, settings, client, photos)
            if mid is False:
                if cache:
                    item.payload = {**item.payload, "_uploaded_image": cache}
                return True
            item.status, item.sent_message_id, item.sent_at = "sent", mid, now()
            item.payload, item.last_error = {}, None
            log.info(
                "Outbox %s %s sent: work_ms=%.0f age_ms=%.0f",
                item.id,
                item.kind,
                (perf_counter() - started) * 1000,
                (item.sent_at - item.created_at).total_seconds() * 1000,
            )
        except Exception as exc:
            if (
                isinstance(exc, MaxError)
                and exc.status == 400
                and exc.code != "attachment.not.ready"
            ):
                cache = {}
            item.payload = {**item.payload, "_uploaded_image": cache}
            retry_job(item, exc)
        return True


async def worker_loop(db, settings, client, photos):
    from lostfound import index_one

    async def run(work, interval=0.1):
        while True:
            try:
                if await work():
                    continue
            except Exception as exc:
                log.error("Worker cycle: %s", safe_error(exc))
            await asyncio.sleep(interval)

    giga = None
    try:
        giga = GigaChat(settings)
    except (OSError, ValueError) as exc:
        log.warning("GigaChat configuration unavailable: %s", safe_error(exc))
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(run(partial(process_incoming, db, settings, client, photos)))
            for answer_only in (True, False):
                tasks.create_task(
                    run(partial(deliver_one, db, settings, client, photos, answer_only=answer_only))
                )
            if giga and giga.http:
                tasks.create_task(run(partial(index_one, db, giga), interval=5))
    finally:
        if giga:
            await giga.close()


def create_app(settings=None, db=None, *, run_worker=True):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        configure_logging(settings.log_level)
        app.state.settings, app.state.db = settings, db or Database(settings)
        client, task = None, None
        try:
            if db is None:
                await migrate(app.state.db)
            if run_worker:
                client = MaxClient(settings)
                task = asyncio.create_task(
                    worker_loop(app.state.db, settings, client, PhotoStore(settings, client.media))
                )
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if client:
                await client.close()
            if db is None:
                await app.state.db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with app.state.db.sessions() as session:
                version = await session.scalar(text("SELECT version_num FROM alembic_version"))
            if version == SCHEMA_VERSION and settings.api_enabled:
                return {"status": "ready"}
        except Exception:
            log.warning("Readiness check failed")
        return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.post("/webhook")
    async def webhook(request: Request):
        received_at = now()
        if not verify_webhook(request.headers.get("X-Max-Bot-Api-Secret"), settings):
            raise HTTPException(403, "Forbidden")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256 * 1024:
                raise HTTPException(413, "Event too large")
        try:
            raw = json.loads(body)
            event = parse_update(raw)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, "Invalid MAX update") from None
        try:
            async with app.state.db.sessions() as session, session.begin():
                await session.execute(
                    insert(IncomingEvent)
                    .values(
                        idempotency_key=event["key"],
                        actor_id=event["user_id"],
                        payload=raw,
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                )
        except Exception:
            raise HTTPException(503, "Storage unavailable") from None
        timestamp = (raw.get("callback") or {}).get("timestamp", raw.get("timestamp"))
        age_ms = None
        if isinstance(timestamp, (int, float)) and 10**8 < timestamp < 10**14:
            timestamp = timestamp / 1000 if timestamp > 10**11 else timestamp
            age = received_at.timestamp() - timestamp
            if -300 <= age <= 7 * 86400:
                age_ms = round(age * 1000)
        log.info(
            "Webhook %s %s: origin_age_ms=%s store_ms=%.0f",
            event["kind"],
            event["key"][:12],
            age_ms,
            (now() - received_at).total_seconds() * 1000,
        )
        return {"ok": True}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, access_log=False)
