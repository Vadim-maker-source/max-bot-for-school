import argparse
import asyncio
import getpass
import logging
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import dotenv_values, set_key
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (
    URL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    exists,
    func,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column

ROOT = Path(__file__).resolve().parent

# ==================== НАСТРОЙКИ ====================


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
    app_host: str = "127.0.0.1"
    app_port: int = 8010
    max_api_url: str = "https://platform-api2.max.ru"
    max_bot_token: SecretStr = SecretStr("")
    max_token_confirmed: bool = False
    director_chat_id: int = 0
    director_user_id: int | None = None
    director_delivery_enabled: bool = False
    guard_user_id: int | None = None
    guard_chat_id: int | None = None
    gigachat_credentials: SecretStr = SecretStr("")
    gigachat_client_id: str = ""
    gigachat_client_secret: SecretStr = SecretStr("")
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat"
    gigachat_base_url: str = "https://api.giga.chat/v1"
    gigachat_ca_bundle_file: str = ""
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "schoolbot_app"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "schoolbot"
    webhook_secret: SecretStr = SecretStr("")
    webhook_url: str = ""
    app_timezone: str = "Europe/Moscow"
    log_level: str = "INFO"
    photo_dir: Path = ROOT / "private" / "photos"
    photo_max_bytes: int = 10 * 1024 * 1024
    photo_allowed_hosts: str = "max.ru,i.oneme.ru,iu.oneme.ru,okcdn.ru,mycdn.me,userapi.com"
    max_ca_bundle: str = ""
    repair_hourly_limit: int = 5

    @field_validator("director_user_id", "guard_user_id", "guard_chat_id", mode="before")
    @classmethod
    def blank_id(cls, value):
        return None if value == "" else value

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("webhook_secret")
    @classmethod
    def valid_secret(cls, value):
        if value.get_secret_value() and not re.fullmatch(
            r"[A-Za-z0-9_-]{32,256}", value.get_secret_value()
        ):
            raise ValueError("WEBHOOK_SECRET: required 32-256 ASCII letters, digits, _ or -")
        return value

    @model_validator(mode="after")
    def validate_urls(self):
        if self.director_delivery_enabled and not (self.director_user_id and self.director_chat_id):
            raise ValueError("Director delivery requires DIRECTOR_USER_ID and DIRECTOR_CHAT_ID")
        api = urlsplit(self.max_api_url)
        if api.scheme != "https" or not api.hostname:
            raise ValueError("MAX_API_URL must use HTTPS")
        giga = urlsplit(self.gigachat_base_url)
        if (
            giga.scheme != "https"
            or not giga.hostname
            or giga.username
            or giga.query
            or giga.fragment
        ):
            raise ValueError("GIGACHAT_BASE_URL must use HTTPS without credentials or query")
        if any(
            value is not None and not 0 < value < 2**63
            for value in (self.guard_user_id, self.guard_chat_id)
        ):
            raise ValueError("GUARD IDs must be positive MAX identifiers")
        if self.webhook_url:
            url = urlsplit(self.webhook_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.port not in (None, 443)
                or url.path != "/webhook"
                or url.query
                or url.fragment
                or url.username
            ):
                raise ValueError("WEBHOOK_URL must be https://host/webhook on port 443")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def api_enabled(self):
        return self.max_token_confirmed and bool(self.max_bot_token.get_secret_value())


@lru_cache
def get_settings():
    return Settings()


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('user','director')"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="user")
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    private_chat_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    id_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# ==================== МОДЕЛИ ПОЛОМОК ====================


class RepairDraft(Base):
    __tablename__ = "repair_drafts"
    __table_args__ = (CheckConstraint("stage IN ('description','photo','location','confirm')"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(20), default="description")
    description: Mapped[str | None] = mapped_column(Text)
    location_text: Mapped[str | None] = mapped_column(Text)
    photo_ref: Mapped[dict | None] = mapped_column(JSONB)
    nonce: Mapped[str] = mapped_column(String(32), default=lambda: uuid4().hex)
    version: Mapped[int] = mapped_column(Integer, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Repair(Base):
    __tablename__ = "repairs"
    __table_args__ = (
        CheckConstraint("status IN ('new','pending','done','rejected')"),
        CheckConstraint("status != 'rejected' OR rejection_reason IS NOT NULL"),
        Index("ix_repairs_author_created", "author_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(Text)
    location_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new")
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    director_user_id: Mapped[int | None] = mapped_column(BigInteger)
    director_message_id: Mapped[str | None] = mapped_column(String(256))
    director_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RepairPhoto(Base):
    __tablename__ = "repair_photos"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repair_id: Mapped[int] = mapped_column(ForeignKey("repairs.id"), unique=True)
    private_path: Mapped[str] = mapped_column(String(256))
    media_metadata: Mapped[dict] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))


# ==================== МОДЕЛИ БЮРО НАХОДОК ====================


class FoundItem(Base):
    __tablename__ = "found_items"
    __table_args__ = (
        CheckConstraint("status IN ('stored','issued')"),
        CheckConstraint("status != 'issued' OR (issued_at IS NOT NULL AND issued_by IS NOT NULL)"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 120"),
        Index("ix_found_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    description: Mapped[str] = mapped_column(String(120))
    photo_ref: Mapped[dict] = mapped_column(JSONB)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    keywords_for: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="stored")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int] = mapped_column(BigInteger)
    issued_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(80))


class FoundDraft(Base):
    __tablename__ = "found_drafts"
    __table_args__ = (CheckConstraint("stage IN ('menu','photo','description','confirm')"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.max_user_id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(20), default="menu")
    operation: Mapped[str] = mapped_column(String(20), default="add")
    item_id: Mapped[int | None] = mapped_column(ForeignKey("found_items.id"))
    item_version: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(120))
    suggestion: Mapped[str | None] = mapped_column(String(120))
    photo_ref: Mapped[dict | None] = mapped_column(JSONB)
    nonce: Mapped[str] = mapped_column(String(32), default=lambda: uuid4().hex)
    version: Mapped[int] = mapped_column(Integer, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    searching: Mapped[bool] = mapped_column(Boolean, default=False)
    query: Mapped[str | None] = mapped_column(String(200))
    view: Mapped[str] = mapped_column(String(20), default="stored")
    page: Mapped[int] = mapped_column(Integer, default=0)
    confirmation: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class FoundEvent(Base):
    __tablename__ = "found_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("found_items.id"), index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(30))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# ==================== ОЧЕРЕДИ И РАБОТА С БАЗОЙ ====================


class Outbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint("status IN ('pending','sent')"),
        CheckConstraint("kind IN ('message','answer','director_card','director_sync')"),
        CheckConstraint("(recipient_chat_id IS NULL) != (recipient_user_id IS NULL)"),
        Index("ix_outbox_due", "status", "next_retry_at", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repair_id: Mapped[int | None] = mapped_column(ForeignKey("repairs.id"))
    recipient_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    recipient_user_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(30))
    dedup_key: Mapped[str] = mapped_column(String(256), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(100))
    sent_message_id: Mapped[str | None] = mapped_column(String(256))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IncomingEvent(Base):
    __tablename__ = "incoming_events"
    __table_args__ = (
        CheckConstraint("status IN ('pending','processed','failed')"),
        Index("ix_incoming_due", "status", "next_retry_at", "id"),
        Index("ix_incoming_actor", "actor_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    idempotency_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DirectorDraft(Base):
    __tablename__ = "director_drafts"
    __table_args__ = (CheckConstraint("stage IN ('reason','confirm')"),)
    director_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repair_id: Mapped[int] = mapped_column(ForeignKey("repairs.id"))
    stage: Mapped[str] = mapped_column(String(20), default="reason")
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    nonce: Mapped[str] = mapped_column(String(32), default=lambda: uuid4().hex)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Database:
    def __init__(self, settings: Settings):
        self.engine = create_async_engine(
            settings.database_url, pool_pre_ping=True, echo=False, hide_parameters=True
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self):
        await self.engine.dispose()


async def enqueue(
    session, *, key, kind="message", payload=None, repair_id=None, chat_id=None, user_id=None
):
    await session.execute(
        insert(Outbox)
        .values(
            dedup_key=key,
            kind=kind,
            payload=payload or {},
            repair_id=repair_id,
            recipient_chat_id=chat_id,
            recipient_user_id=user_id,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
    )


async def next_job(session, model, *, answer_only=None):
    older = aliased(model)
    incoming = model is IncomingEvent
    same_user = (
        (older.actor_id == model.actor_id)
        if incoming
        else or_(
            older.recipient_chat_id == model.recipient_chat_id,
            older.recipient_user_id == model.recipient_user_id,
        )
    )
    blocking = [older.status == "pending", older.id < model.id, same_user]
    if not incoming:
        blocking.append(older.kind != "answer")
    unblocked = ~exists(select(older.id).where(*blocking))
    priority = [] if incoming else [model.kind != "answer"]
    eligible = unblocked if incoming else or_(model.kind == "answer", unblocked)
    lane = [] if incoming or answer_only is None else [(model.kind == "answer") == answer_only]
    return await session.scalar(
        select(model)
        .where(
            model.status == "pending",
            model.next_retry_at <= now(),
            eligible,
            *lane,
        )
        .order_by(*priority, model.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def configure_logging(level="INFO"):
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True
    )
    for name in ("httpx", "httpcore", "sqlalchemy.engine", "sqlalchemy.pool"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def safe_error(exc):
    return type(exc).__name__[:80]


# ==================== МИГРАЦИИ ====================

SCHEMA_VERSION = "0003_lostfound"


async def migrate(db):
    # Keep known revisions upgradeable; never stamp an unknown schema.
    async with db.engine.begin() as connection:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('schoolbot:migrations', 0))")
        )
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        versions = []
        if "alembic_version" in tables:
            versions = list(
                (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalars()
            )
        if versions == [SCHEMA_VERSION]:
            return False
        if versions == ["0001_initial"]:
            await connection.execute(
                text("ALTER TABLE repairs ADD COLUMN director_viewed_at TIMESTAMPTZ NULL")
            )
            versions = ["0002_director_viewed"]
        if versions == ["0002_director_viewed"]:
            for model in (FoundItem, FoundDraft, FoundEvent):
                await connection.run_sync(lambda conn, table=model.__table__: table.create(conn))
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :version"),
                {"version": SCHEMA_VERSION},
            )
            return True
        if versions or set(Base.metadata.tables).intersection(tables):
            raise ValueError("Unknown database revision; no schema changes performed")
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:version)"), {"version": SCHEMA_VERSION}
        )
        return True


async def database_command(settings, args):
    from max_api import PHOTO_EXTENSIONS, PhotoStore

    db = Database(settings)
    try:
        async with db.sessions() as session, session.begin():
            if args.command == "director":
                user = await session.scalar(select(User).where(User.max_user_id == args.user_id))
                if (
                    not user
                    or not user.private_chat_verified
                    or not user.id_seen_at
                    or user.chat_id != settings.director_chat_id
                ):
                    raise ValueError(
                        "Director must send /id privately; dialog must match DIRECTOR_CHAT_ID"
                    )
                print("Director identity and private dialog verified; IDs hidden.")
                if args.enable_delivery and not args.confirm:
                    raise ValueError("--enable-delivery requires --confirm")
                if args.confirm:
                    set_key(ROOT / ".env", "DIRECTOR_USER_ID", str(user.max_user_id))
                    if args.enable_delivery:
                        set_key(ROOT / ".env", "DIRECTOR_DELIVERY_ENABLED", "true")
                    print("Saved. Restart app; enabled delivery sends queued real cards.")
                return
            if args.command == "doctor":
                print("Database:", await session.scalar(text("SELECT current_database()")))
                print(
                    "Superuser:",
                    await session.scalar(
                        text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
                    ),
                )
                print(
                    "Migration:",
                    await session.scalar(text("SELECT version_num FROM alembic_version")),
                )
            for model in (IncomingEvent, Outbox):
                counts = (
                    await session.execute(select(model.status, func.count()).group_by(model.status))
                ).all()
                print(model.__tablename__, dict(counts))
            for model, job_id in ((IncomingEvent, args.retry_inbox), (Outbox, args.retry_outbox)):
                if job_id:
                    job = await session.get(model, job_id, with_for_update=True)
                    if not job or job.status not in ("pending", "failed"):
                        raise ValueError("Only pending/failed jobs can be retried")
                    job.status, job.next_retry_at = "pending", now()
                    if model is IncomingEvent:
                        job.attempts = 0
            if args.cleanup_unused_photos:
                refs = set((await session.scalars(select(RepairPhoto.private_path))).all())
                values = list((await session.scalars(select(RepairDraft.photo_ref))).all())
                for model in (FoundItem, FoundDraft):
                    values += list((await session.scalars(select(model.photo_ref))).all())
                values += [
                    p.get("_photo")
                    for p in (
                        await session.scalars(
                            select(Outbox.payload).where(Outbox.status == "pending")
                        )
                    ).all()
                ]
                refs.update(p["private_path"] for p in values if p)
                store = PhotoStore(settings, None)
                for path in store.root.iterdir():
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and path.name not in refs
                        and path.suffix in {*PHOTO_EXTENSIONS.values(), ".bin", ".tmp"}
                        and path.stat().st_mtime < time.time() - 7 * 86400
                    ):
                        store.path(path.name).unlink()
    finally:
        await db.close()


async def init_database(settings):
    role, name = settings.postgres_user, settings.postgres_db
    if settings.postgres_host not in ("127.0.0.1", "localhost", "::1") or role == "postgres":
        raise ValueError("Local initialization requires a dedicated app role and loopback host")
    if not all(re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", v) for v in (role, name)):
        raise ValueError("Choose simple lowercase SQL identifiers")
    password = getpass.getpass("Local PostgreSQL administrator password: ")
    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user="postgres",
        password=password,
        database="postgres",
    )
    try:
        if await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname=$1", role
        ) or await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
            raise ValueError("Existing database/role will not be changed")
        secret = secrets.token_urlsafe(36)
        await conn.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
            f"PASSWORD '{secret}'"
        )
        set_key(ROOT / ".env", "POSTGRES_PASSWORD", secret)
        if not settings.webhook_secret.get_secret_value():
            set_key(ROOT / ".env", "WEBHOOK_SECRET", secrets.token_urlsafe(32))
        await conn.execute(f"CREATE DATABASE {name} OWNER {role}")
        print("New database and app role created. Password saved only to .env.")
    finally:
        await conn.close()


def backup(settings):
    from max_api import PhotoStore

    executable = shutil.which("pg_dump") or r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"
    target = ROOT / "private/backups" / now().strftime("%Y%m%dT%H%M%SZ")
    target.mkdir(parents=True, exist_ok=False)
    environment = {**os.environ, "PGPASSWORD": settings.postgres_password.get_secret_value()}
    subprocess.run(
        [
            executable,
            "-h",
            settings.postgres_host,
            "-p",
            str(settings.postgres_port),
            "-U",
            settings.postgres_user,
            "-d",
            settings.postgres_db,
            "-Fc",
            "-f",
            str(target / "database.dump"),
        ],
        env=environment,
        check=True,
    )
    with tarfile.open(target / "photos.tar.gz", "w:gz") as archive:
        archive.add(PhotoStore(settings, None).root, arcname="photos")
    print("Backup:", target)


def audit():
    values = dotenv_values(ROOT / ".env")
    secret_values = [
        values.get(k)
        for k in (
            "TOKEN",
            "MAX_BOT_TOKEN",
            "POSTGRES_PASSWORD",
            "POSTGRES_ADMIN_PASSWORD",
            "WEBHOOK_SECRET",
            "DIRECTOR_USER_ID",
            "DIRECTOR_CHAT_ID",
            "GUARD_USER_ID",
            "GUARD_CHAT_ID",
            "GIGACHAT_CREDENTIALS",
            "GIGACHAT_CLIENT_SECRET",
        )
    ]
    ignored = {".venv", ".git", "private", "work", "__pycache__", ".pytest_cache", ".ruff_cache"}
    for path in ROOT.rglob("*"):
        if (
            path.is_file()
            and not ignored.intersection(path.relative_to(ROOT).parts)
            and (
                path.suffix in (".py", ".sh", ".yaml", ".md", ".txt", ".ps1", ".toml")
                or path.name == ".env.example"
            )
        ):
            content = path.read_text(encoding="utf-8", errors="ignore")
            if any(value and len(value) >= 6 and value in content for value in secret_values):
                raise ValueError("Secret detected in " + str(path.relative_to(ROOT)))
    print("Secret scan passed.")


async def administer(args, settings):
    if args.command == "init-db":
        await init_database(settings)
    elif args.command == "migrate":
        db = Database(settings)
        try:
            changed = await migrate(db)
            print("Applied:" if changed else "Current:", SCHEMA_VERSION)
        finally:
            await db.close()
    else:
        await database_command(settings, args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PostgreSQL administration; credentials stay in .env"
    )
    parser.add_argument(
        "command", choices=["doctor", "queues", "director", "migrate", "init-db", "backup", "audit"]
    )
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--enable-delivery", action="store_true")
    parser.add_argument("--retry-inbox", type=int)
    parser.add_argument("--retry-outbox", type=int)
    parser.add_argument("--cleanup-unused-photos", action="store_true")
    args = parser.parse_args()
    try:
        settings = get_settings()
        if args.command == "audit":
            audit()
        elif args.command == "backup":
            backup(settings)
        else:
            asyncio.run(administer(args, settings))
    except Exception as exc:
        print(str(exc) if isinstance(exc, ValueError) else safe_error(exc))
        raise SystemExit(1) from None
