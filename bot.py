import hashlib
import hmac
import json
from datetime import timedelta
from functools import partial
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from database import DirectorDraft, Repair, RepairDraft, RepairPhoto, User, enqueue, now
from max_api import InvalidPhoto

WELCOME = (
    "🏫 Добро пожаловать в школьного помощника!\n"
    "Выберите нужный раздел в меню:"
)
STAGES = ("description", "photo", "location", "confirm")
FIELDS = {"description": "description", "photo": "photo_ref", "location": "location_text"}
PROMPTS = {
    "description": "🔧 Что сломалось? Опишите проблему одним сообщением. "
    "Например: Не работает проектор (до 1500 символов)",
    "photo": "📷 Прикрепите одну фотографию неисправности (как изображение, до 10 МБ)",
    "location": "📍 Где обнаружена поломка? Напишите кабинет, этаж или место в школе. "
    "Например: Кабинет 204, 2 этаж (до 300 символов)",
}
STATUS = {"new": "Новая", "pending": "Не выполнено", "done": "Выполнено", "rejected": "Отклонено"}


def keyboard(rows):
    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [{"type": "callback", "text": label, "payload": value} for label, value in row]
                for row in rows
            ]
        },
    }


MENU = keyboard([[("🔧 Сообщить о поломке", "repair:start")]])


def is_director(user_id, settings):
    return settings.director_user_id is not None and user_id == settings.director_user_id


def verify_webhook(provided, settings):
    expected = settings.webhook_secret.get_secret_value()
    return bool(expected and provided) and hmac.compare_digest(expected.encode(), provided.encode())


def parse_update(raw):
    kind = raw.get("update_type")
    message, callback = raw.get("message") or {}, raw.get("callback") or {}
    body, recipient = message.get("body") or {}, message.get("recipient") or {}
    if kind == "bot_started":
        user, chat_id, private = raw["user"], int(raw["chat_id"]), True
        if not isinstance(raw.get("timestamp"), int):
            raise ValueError("Missing timestamp")
        identity = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    elif kind in ("message_created", "message_callback"):
        clicking = kind == "message_callback"
        user = callback["user"] if clicking else message["sender"]
        if user.get("is_bot"):
            raise ValueError("Bot messages are ignored")
        identifier = callback.get("callback_id") if clicking else body.get("mid")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Missing event identifier")
        identity = ("callback:" if clicking else "message:") + identifier
        chat_id = recipient.get("chat_id")
        private = recipient.get("chat_type") == "dialog" if message else clicking
    else:
        raise ValueError("Unsupported update_type")
    user_id = int(user["user_id"])
    text = (body.get("text") or "") if kind == "message_created" else ""
    attachments = (body.get("attachments") or []) if kind == "message_created" else []
    payload = callback.get("payload") or ""
    if (
        not 0 < user_id < 2**63
        or not isinstance(text, str)
        or not isinstance(payload, str)
        or not isinstance(attachments, list)
        or any(not isinstance(a, dict) for a in attachments)
    ):
        raise ValueError("Invalid MAX event")
    return dict(
        kind=kind,
        user_id=user_id,
        chat_id=chat_id,
        private=private,
        name=str(user.get("name") or user.get("first_name") or "Пользователь")[:200],
        text=text,
        attachments=attachments,
        payload=payload,
        callback_id=callback.get("callback_id"),
        message_id=body.get("mid"),
        key=hashlib.sha256(identity.encode()).hexdigest(),
    )


async def reply(session, event, text=None, buttons=None, photo=None, *, answer=None):
    if answer and event["callback_id"]:
        await enqueue(
            session,
            key=f"answer:{event['key']}",
            kind="answer",
            user_id=event["user_id"],
            payload={"callback_id": event["callback_id"], "notification": answer},
        )
    if text is not None:
        payload = {"text": text}
        if buttons:
            payload["attachments"] = [buttons]
        if photo:
            payload["_photo"] = photo
        await enqueue(
            session, key=f"event:{event['key']}:1", user_id=event["user_id"], payload=payload
        )


async def show_draft(draft, send, prefix="", answer=None):
    admin = isinstance(draft, DirectorDraft)
    callback = "reject" if admin else "repair"
    suffix = f"{draft.nonce}:{draft.version}"
    rows, photo = [], None
    if admin:
        text = (
            (
                f"Причина отклонения заявки №{draft.repair_id}:\n{draft.rejection_reason}\n"
                "Подтвердить отклонение?"
            )
            if draft.stage == "confirm"
            else (
                f"Напишите причину отклонения заявки №{draft.repair_id} "
                "одним текстовым сообщением (до 1000 символов)."
            )
        )
    elif draft.stage == "confirm":
        text = (
            f"Проверьте заявку:\n🔧 {draft.description}\n📍 {draft.location_text}\n"
            "📷 Фотография сохранена.\nОтправить директору?"
        )
        photo = draft.photo_ref
    else:
        text = PROMPTS[draft.stage]
        saved = getattr(draft, FIELDS[draft.stage])
        if saved:
            text += (
                "\n\nФотография сохранена. Можно оставить или прислать другую."
                if draft.stage == "photo"
                else f"\n\nСохранено: {saved}\nМожно ввести новый текст."
            )
            rows.append([("➡️ Оставить и далее", f"repair:next:{suffix}")])
        photo = draft.photo_ref if draft.stage == "photo" else None
    if draft.stage == "confirm":
        label = "✅ Подтвердить отклонение" if admin else "✅ Отправить директору"
        rows.append([(label, f"{callback}:confirm:{suffix}")])
    rows.append(
        [("⬅️ Назад", f"{callback}:back:{suffix}"), ("❌ Отмена", f"{callback}:cancel:{suffix}")]
    )
    await send(prefix + text, keyboard(rows), photo, answer=answer)


def current_button(draft, parts):
    return (
        draft is not None
        and len(parts) == 4
        and parts[2] == draft.nonce
        and parts[3] == str(draft.version)
        and not getattr(draft, "paused", False)
    )


async def submit(session, user, draft, settings):
    count = await session.scalar(
        select(func.count())
        .select_from(Repair)
        .where(Repair.author_id == user.id, Repair.created_at >= now() - timedelta(hours=1))
    )
    if count >= settings.repair_hourly_limit:
        return None
    description, location = (draft.description or "").strip(), (draft.location_text or "").strip()
    if not (0 < len(description) <= 1500 and 0 < len(location) <= 300 and draft.photo_ref):
        raise ValueError("Incomplete repair draft")
    repair = Repair(
        author_id=user.id, description=description, location_text=location, status="new"
    )
    session.add(repair)
    await session.flush()
    session.add(RepairPhoto(repair_id=repair.id, **draft.photo_ref))
    await enqueue(
        session,
        key=f"repair:{repair.id}:director",
        kind="director_card",
        chat_id=settings.director_chat_id,
        repair_id=repair.id,
    )
    await session.delete(draft)
    return repair


async def transition(session, repair, status, director_id, settings, reason=None):
    if repair.status in ("done", "rejected") or repair.status == status:
        return False
    if status not in ("pending", "done", "rejected") or (status == "rejected" and not reason):
        raise ValueError("Invalid status transition")
    repair.status, repair.director_user_id, repair.updated_at = status, director_id, now()
    repair.rejection_reason = reason if status == "rejected" else None
    if status in ("done", "rejected"):
        repair.resolved_at = now()
    author = await session.get(User, repair.author_id)
    messages = {
        "done": f"✅ Заявка №{repair.id} выполнена",
        "pending": f"🕓 Заявка №{repair.id} пока не выполнена",
        "rejected": f"❌ Заявка №{repair.id} отклонена.\nПричина: {reason}",
    }
    await enqueue(
        session,
        key=f"repair:{repair.id}:author:{status}",
        repair_id=repair.id,
        user_id=author.max_user_id,
        payload={"text": messages[status]},
    )
    await enqueue(
        session,
        key=f"repair:{repair.id}:sync:{status}",
        kind="director_sync",
        repair_id=repair.id,
        chat_id=settings.director_chat_id,
    )
    return True


async def repair_callback(session, user, draft, event, send, settings):
    parts = event["payload"].split(":")
    if event["payload"] == "repair:start":
        if draft is None:
            draft = RepairDraft(user_id=user.id)
            session.add(draft)
            await session.flush()
        elif draft.paused:
            draft.paused, draft.version = False, draft.version + 1
        return await show_draft(draft, send, answer="Продолжим заполнение заявки")
    if not current_button(draft, parts):
        if draft and not draft.paused:
            return await show_draft(
                draft, send, "Кнопка устарела. Текущий шаг:\n", "Кнопка устарела"
            )
        return await send("Кнопка устарела. Выберите действие:", MENU, answer="Кнопка устарела")
    action = parts[1]
    if action == "cancel":
        await session.delete(draft)
        return await send("Черновик отменён.\n" + WELCOME, MENU, answer="Черновик закрыт")
    if action == "back" and draft.stage == "description":
        draft.paused, draft.version = True, draft.version + 1
        return await send("Черновик сохранён.\n" + WELCOME, MENU, answer="Черновик сохранён")
    if action == "back" or (action == "next" and draft.stage != "confirm"):
        if action == "next" and not getattr(draft, FIELDS[draft.stage]):
            return await show_draft(draft, send, answer="Сначала заполните этот шаг")
        draft.stage = STAGES[STAGES.index(draft.stage) + {"back": -1, "next": 1}[action]]
        draft.version += 1
        return await show_draft(draft, send, answer="Данные сохранены")
    if action == "confirm" and draft.stage == "confirm":
        repair = await submit(session, user, draft, settings)
        if repair is None:
            return await show_draft(
                draft, send, "Лимит заявок в час. Черновик сохранён.\n", "Лимит заявок"
            )
        return await send(
            f"✅ Заявка №{repair.id} зарегистрирована и поставлена в очередь отправки директору",
            MENU,
            answer="Заявка зарегистрирована",
        )
    await show_draft(draft, send, answer="Недоступное действие")


async def admin_callback(session, user, draft, event, send, settings):
    if not is_director(event["user_id"], settings):
        return await send("Это действие доступно только директору.", answer="Недостаточно прав")
    parts = event["payload"].split(":")
    if parts[0] == "reject":
        if not current_button(draft, parts):
            if draft:
                return await show_draft(draft, send, answer="Кнопка устарела")
            return await send(answer="Кнопка устарела")
        action = parts[1]
        if action == "cancel" or (action == "back" and draft.stage == "reason"):
            await session.delete(draft)
            return await send(
                "Отклонение отменено. Статус заявки не изменён.", answer="Отклонение отменено"
            )
        if action == "back":
            draft.stage, draft.version = "reason", draft.version + 1
            return await show_draft(draft, send, answer="Введите причину заново")
        if action == "confirm" and draft.stage == "confirm":
            repair = await session.get(Repair, draft.repair_id, with_for_update=True)
            changed = repair and await transition(
                session, repair, "rejected", event["user_id"], settings, draft.rejection_reason
            )
            await session.delete(draft)
            text = "Заявка отклонена." if changed else "Заявка уже закрыта."
            return await send(text, answer=text)
        return await send(answer="Действие недоступно")
    if len(parts) != 3 or not parts[2].isdigit() or not 0 < int(parts[2]) < 2**63:
        return await send(answer="Некорректная кнопка")
    repair = await session.get(Repair, int(parts[2]), with_for_update=True)
    if not repair or repair.status in ("done", "rejected"):
        return await send(answer="Заявка уже закрыта" if repair else "Заявка не найдена")
    if parts[1] == "reject":
        if draft and draft.repair_id != repair.id:
            return await show_draft(draft, send, answer="Завершите текущее отклонение")
        if draft is None:
            draft = DirectorDraft(director_user_id=event["user_id"], repair_id=repair.id)
            session.add(draft)
            await session.flush()
        return await show_draft(draft, send, answer="Укажите причину")
    if parts[1] not in ("done", "pending"):
        return await send(answer="Неизвестное действие")
    changed = await transition(session, repair, parts[1], event["user_id"], settings)
    if draft and draft.repair_id == repair.id and parts[1] == "done":
        await session.delete(draft)
    await send(answer="Статус обновлён" if changed else "Статус уже установлен")


async def draft_message(session, draft, event, send, photos):
    text = event["text"].strip()
    if isinstance(draft, DirectorDraft):
        repair = await session.get(Repair, draft.repair_id, with_for_update=True)
        if not repair or repair.status in ("done", "rejected"):
            await session.delete(draft)
            return await send("Заявка уже закрыта. Отклонение отменено.")
        if draft.stage == "confirm":
            return await show_draft(draft, send, "Используйте кнопки подтверждения или возврата.\n")
        field, limit, next_stage = "rejection_reason", 1000, "confirm"
    elif draft.stage == "confirm":
        return await show_draft(
            draft, send, "Используйте кнопки для подтверждения или изменения.\n"
        )
    elif draft.stage == "photo":
        if len(event["attachments"]) != 1 or event["attachments"][0].get("type") != "image":
            return await show_draft(draft, send, "Пришлите ровно одно изображение.\n")
        try:
            draft.photo_ref = await photos.receive(event["attachments"][0])
        except InvalidPhoto as exc:
            return await show_draft(draft, send, f"{exc}\n")
        field, limit, next_stage = None, None, "location"
    else:
        field = FIELDS[draft.stage]
        limit = {"description": 1500, "location": 300}[draft.stage]
        next_stage = STAGES[STAGES.index(draft.stage) + 1]
    if field:
        if event["attachments"] or not 0 < len(text) <= limit:
            return await show_draft(draft, send, f"Нужен непустой текст до {limit} символов.\n")
        setattr(draft, field, text)
    draft.stage, draft.version = next_stage, draft.version + 1
    await show_draft(draft, send)


async def route(session, event, settings, photos):
    send = partial(reply, session, event)
    if not event["private"]:
        return await send(answer="Откройте личный диалог с ботом")
    identity = dict(
        max_user_id=event["user_id"],
        name=event["name"],
        role="director" if is_director(event["user_id"], settings) else "user",
    )
    user = await session.scalar(
        insert(User)
        .values(**identity)
        .on_conflict_do_update(
            index_elements=["max_user_id"],
            set_={"name": identity["name"], "role": identity["role"], "updated_at": now()},
        )
        .returning(User)
    )
    if event["chat_id"] is not None and event["kind"] != "message_callback":
        user.chat_id, user.private_chat_verified = event["chat_id"], True
        user.started_at = user.started_at or now()
    draft = await session.get(RepairDraft, user.id)
    admin = (
        await session.get(DirectorDraft, event["user_id"])
        if is_director(event["user_id"], settings)
        else None
    )
    if event["kind"] == "bot_started" or event["text"].strip() == "/start":
        suffix = ""
        if draft:
            draft.paused, draft.version = True, draft.version + 1
            suffix = (
                "\n\nЧерновик сохранён, ввод приостановлен. "
                "Нажмите «Сообщить о поломке», чтобы продолжить."
            )
        if admin:
            await session.delete(admin)
            suffix += "\nВвод причины отклонения отменён. Статус заявки не изменён."
        return await send(WELCOME + suffix, MENU)
    if event["text"].strip() == "/id":
        user.id_seen_at = now()
        text = f"Ваш MAX user_id: {event['user_id']}"
        if event["chat_id"] is not None:
            text += f"\nID вашего личного диалога: {event['chat_id']}"
        return await send(text)
    if event["callback_id"]:
        prefix = event["payload"].split(":", 1)[0]
        if prefix == "repair" and admin:
            return await show_draft(admin, send, answer="Завершите или отмените отклонение")
        handler = {
            "repair": repair_callback,
            "admin": admin_callback,
            "reject": admin_callback,
        }.get(prefix)
        if handler:
            return await handler(
                session, user, draft if prefix == "repair" else admin, event, send, settings
            )
        return await send(WELCOME, MENU, answer="Неизвестная или устаревшая кнопка")
    if admin or (draft and not draft.paused):
        return await draft_message(session, admin or draft, event, send, photos)
    await send("Выберите действие в меню.\n" + WELCOME, MENU)


def director_card(repair, author, timezone):
    created = repair.created_at.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
    text = (
        f"🚨 Новая заявка №{repair.id}\n👤 Отправитель: {author.name}\n"
        f"🆔 MAX ID: {author.max_user_id}\n🔧 Неисправность: {repair.description}\n"
        f"📍 Место: {repair.location_text}\n🕒 Создана: {created}\n"
        f"📌 Статус: {STATUS[repair.status]}"
    )
    if repair.rejection_reason:
        text += f"\nПричина отклонения: {repair.rejection_reason}"
    buttons = []
    if repair.status not in ("done", "rejected"):
        actions = {"done": "✅ Выполнено", "pending": "🕓 Не выполнено", "reject": "❌ Отклонить"}
        rows = [
            [(label, f"admin:{action}:{repair.id}")]
            for action, label in actions.items()
            if action != "pending" or repair.status == "new"
        ]
        buttons = [keyboard(rows)]
    return {"text": text, "attachments": buttons}
