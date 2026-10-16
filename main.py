import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from functools import partial
from time import perf_counter

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, text, update
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


async def process_incoming(db, settings, client, photos, *, event_key=None, direct_until=None):
    async with db.sessions() as session, session.begin():
        item = await next_job(session, IncomingEvent, key=event_key)
        if item is None:
            return False
        try:
            async with session.begin_nested():
                if not await session.get(ProcessedEvent, item.idempotency_key):
                    await route(session, parse_update(item.payload), settings, photos)
                    session.add(ProcessedEvent(idempotency_key=item.idempotency_key))
                    await session.flush()
            if direct_until:
                await session.execute(
                    update(Outbox)
                    .where(
                        Outbox.dedup_key.in_((f"answer:{event_key}", f"event:{event_key}:1")),
                        Outbox.status == "pending",
                    )
                    .values(next_retry_at=direct_until)
                )
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
    answer = item.kind == "answer"
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
        {"key": "max-rate:" + key + (":answer" if answer else ":message")},
    )
    if not locked:
        item.next_retry_at = now() + timedelta(seconds=0.2)
        return False
    sent = list(
        await session.scalars(
            select(Outbox.sent_at)
            .where(
                Outbox.status == "sent",
                (Outbox.kind == "answer") == answer,
                Outbox.sent_at > now() - timedelta(seconds=1),
                or_(
                    Outbox.recipient_chat_id == chat_id if chat_id is not None else False,
                    Outbox.recipient_user_id == user_id if user_id is not None else False,
                ),
            )
            .order_by(Outbox.sent_at.desc())
            .limit(2)
        )
    )
    if len(sent) == 2:
        item.next_retry_at = sent[-1] + timedelta(seconds=1)
        return False
    return True


async def message_body(payload, photo, cache, client, photos):
    body = {key: value for key, value in payload.items() if not key.startswith("_")}
    if photo:
        path = await asyncio.to_thread(photos.verify, photo)
        if cache.get("checksum") != photo["checksum"] or not cache.get("payload"):
            cache.update(
                checksum=photo["checksum"],
                payload=photos.uploaded_images.get(photo["checksum"])
                or await client.upload_image(path, photo["media_metadata"]["mime"]),
            )
        photos.uploaded_images[photo["checksum"]] = cache["payload"]
        if len(photos.uploaded_images) > 128:
            photos.uploaded_images.pop(next(iter(photos.uploaded_images)))
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
        if item.kind == "director_sync" and item.created_at < repair.updated_at:
            return repair.director_message_id
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
        await client.answer(
            **{key: value for key, value in item.payload.items() if not key.startswith("_")}
        )
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


async def deliver_one(db, settings, client, photos, *, answer_only=None, event_key=None):
    if not settings.api_enabled:
        return False
    async with db.sessions() as session, session.begin():
        item = await next_job(session, Outbox, answer_only=answer_only, key=event_key)
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
                item.kind == "answer"
                and isinstance(exc, MaxError)
                and exc.status in (400, 404, 410)
            ):
                item.kind = "message"
                item.payload = item.payload.get("message") or {
                    "text": item.payload.get("notification") or "Откройте меню командой /start"
                }
                item.next_retry_at, item.last_error = now(), safe_error(exc)
                log.warning("Callback %s unavailable: HTTP %s", item.id, exc.status)
                return True
            if (
                isinstance(exc, MaxError)
                and exc.status == 400
                and exc.code != "attachment.not.ready"
            ):
                photos.uploaded_images.pop(cache.get("checksum"), None)
                cache = {}
            item.payload = {**item.payload, "_uploaded_image": cache}
            retry_job(item, exc)
        return True


async def respond_event(app, event, direct_until):
    event_key = event["key"]
    state = app.state
    started = perf_counter()
    try:
        async with asyncio.timeout(4):
            await process_incoming(
                state.db,
                state.settings,
                state.client,
                state.photos,
                event_key=event_key,
                direct_until=direct_until,
            )
            keys = ([f"answer:{event_key}"] if event["callback_id"] else []) + [
                f"event:{event_key}:1"
            ]
            for key in keys:
                await deliver_one(
                    state.db,
                    state.settings,
                    state.client,
                    state.photos,
                    event_key=key,
                )
    except Exception as exc:
        log.warning("Direct event %s: %s", event_key[:12], safe_error(exc))
    finally:
        try:
            async with state.db.sessions() as session, session.begin():
                for model, match in (
                    (IncomingEvent, IncomingEvent.idempotency_key == event_key),
                    (Outbox, Outbox.dedup_key.in_((f"answer:{event_key}", f"event:{event_key}:1"))),
                ):
                    await session.execute(
                        update(model)
                        .where(
                            match, model.status == "pending", model.next_retry_at == direct_until
                        )
                        .values(next_retry_at=now())
                    )
        except Exception as exc:
            log.warning("Event recovery: %s", safe_error(exc))
        state.incoming_wake.set()
        for wake in state.outgoing_wakes:
            wake.set()
    log.info(
        "Event %s %s direct_ms=%.0f",
        event["kind"],
        event_key[:12],
        (perf_counter() - started) * 1000,
    )


async def worker_loop(db, settings, client, photos, *, incoming_wake=None, outgoing_wakes=None):
    from lostfound import index_one

    incoming_wake = incoming_wake or asyncio.Event()
    answer_wake, message_wake = outgoing_wakes or (asyncio.Event(), asyncio.Event())

    async def run(work, wake, notify=(), interval=0.1):
        delay = interval
        while True:
            wake.clear()
            try:
                if await work():
                    wake.set()
                    for event in notify:
                        event.set()
                    delay = interval
                    continue
            except Exception as exc:
                log.error("Worker cycle: %s", safe_error(exc))
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay)
                delay = interval
            except TimeoutError:
                delay = min(max(1, interval), delay * 2)

    giga = None
    try:
        giga = GigaChat(settings)
    except (OSError, ValueError) as exc:
        log.warning("GigaChat configuration unavailable: %s", safe_error(exc))
    try:
        async with asyncio.TaskGroup() as tasks:
            for _ in range(4):
                tasks.create_task(
                    run(
                        partial(process_incoming, db, settings, client, photos),
                        incoming_wake,
                        notify=(answer_wake, message_wake),
                    )
                )
            for answer_only, wake in ((True, answer_wake), (False, message_wake)):
                for _ in range(4):
                    tasks.create_task(
                        run(
                            partial(
                                deliver_one,
                                db,
                                settings,
                                client,
                                photos,
                                answer_only=answer_only,
                            ),
                            wake,
                        )
                    )
            if giga and giga.http:
                tasks.create_task(run(partial(index_one, db, giga), asyncio.Event(), interval=5))
            log.info("Bot workers started")
    finally:
        if giga:
            await giga.close()


def create_app(settings=None, db=None, *, run_worker=True):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        configure_logging(settings.log_level)
        app.state.settings, app.state.db = settings, db or Database(settings)
        app.state.incoming_wake = asyncio.Event()
        app.state.outgoing_wakes = (asyncio.Event(), asyncio.Event())
        client, task = None, None
        try:
            if db is None:
                await migrate(app.state.db)
            if run_worker:
                client = app.state.client = MaxClient(settings)
                app.state.photos = PhotoStore(settings, client.media)
                task = asyncio.create_task(
                    worker_loop(
                        app.state.db,
                        settings,
                        client,
                        app.state.photos,
                        incoming_wake=app.state.incoming_wake,
                        outgoing_wakes=app.state.outgoing_wakes,
                    )
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
    async def webhook(request: Request, background_tasks: BackgroundTasks):
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
        direct = run_worker and settings.api_enabled
        direct_until = now() + timedelta(seconds=5) if direct else now()
        try:
            async with app.state.db.sessions() as session, session.begin():
                inserted = await session.scalar(
                    insert(IncomingEvent)
                    .values(
                        idempotency_key=event["key"],
                        actor_id=event["user_id"],
                        payload=raw,
                        next_retry_at=direct_until,
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    .returning(IncomingEvent.id)
                )
        except Exception:
            raise HTTPException(503, "Storage unavailable") from None
        if direct and inserted is not None:
            background_tasks.add_task(respond_event, app, event, direct_until)
        else:
            app.state.incoming_wake.set()
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
