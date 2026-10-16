import hashlib
import hmac
import json
from datetime import datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from database import DirectorDraft, Repair, RepairDraft, RepairPhoto, User, enqueue, now
from max_api import InvalidPhoto

WELCOME = "🏫 Добро пожаловать в школьного помощника!\nВыберите нужный раздел в меню:"
STAGES = ("description", "photo", "location", "confirm")
FIELDS = {"description": "description", "photo": "photo_ref", "location": "location_text"}
PROMPTS = {
    "description": "🔧 Что сломалось? Опишите проблему одним сообщением. "
    "Например: Не работает проектор (до 1500 символов)",
    "photo": "📷 Прикрепите одну фотографию неисправности (как изображение, до 10 МБ)",
    "location": "📍 Где обнаружена поломка? Напишите кабинет, этаж или место в школе. "
    "Например: Кабинет 204, 2 этаж (до 300 символов)",
}
STATUS = {
    "new": "🆕 Новая",
    "pending": "🕓 Не выполнено",
    "done": "✅ Выполнено",
    "rejected": "❌ Отклонено",
}
MOSCOW = ZoneInfo("Europe/Moscow")
MONTHS = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


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


async def main_menu(session, user_id, settings):
    if not is_director(user_id, settings):
        return MENU
    unread = await session.scalar(
        select(func.count()).select_from(Repair).where(Repair.director_viewed_at.is_(None))
    )
    return keyboard(
        [
            [(f"📥 Новые заявки ({unread})", "director:inbox")],
            [("📅 Архив заявок", "director:archive")],
            [("🔧 Сообщить о поломке", "repair:start")],
        ]
    )


async def repair_list(session, send, page=0, year=None, month=None):
    if year is None:
        condition = Repair.director_viewed_at.is_(None)
        callback = "director:inbox"
    else:
        start = datetime(year, month, 1, tzinfo=MOSCOW)
        end = (
            datetime(year + 1, 1, 1, tzinfo=MOSCOW)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=MOSCOW)
        )
        condition = (Repair.created_at >= start) & (Repair.created_at < end)
        callback = f"director:month:{year}:{month}"
    counts = dict(
        (
            await session.execute(
                select(Repair.status, func.count()).where(condition).group_by(Repair.status)
            )
        ).all()
    )
    total = sum(counts.values())
    page = min(page, max(0, (total - 1) // 10))
    repairs = (
        await session.scalars(
            select(Repair)
            .where(condition)
            .order_by(Repair.created_at.desc(), Repair.id.desc())
            .offset(page * 10)
            .limit(10)
        )
    ).all()
    if year is None:
        title = f"📥 Непросмотренные заявки: {total}" if total else "✅ Новых заявок нет"
    else:
        lines = [f"📊 Заявки за {MONTHS[month - 1].lower()} {year}", f"Всего: {total}"]
        for status, label in (
            ("done", "✅ Выполнено"),
            ("pending", "🕓 Не выполнено"),
            ("new", "🆕 Новые"),
            ("rejected", "❌ Отклонено"),
        ):
            lines.append(f"{label}: {counts.get(status, 0)}")
        if not total:
            lines.append("За выбранный месяц заявки отсутствуют")
        title = "\n".join(lines)
    blocks, rows = [title], []
    for repair in repairs:
        date = repair.created_at.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
        description = " ".join(repair.description.split())[:100]
        location = " ".join(repair.location_text.split())[:60]
        block = f"№{repair.id} · {date}\n{description}\n📍 {location}\n{STATUS[repair.status]}"
        if repair.rejection_reason:
            reason = " ".join(repair.rejection_reason.split())[:80]
            block += f"\nПричина: {reason}"
        blocks.append(block)
        rows.append(
            [
                (
                    f"Открыть №{repair.id}",
                    f"director:open:{repair.id}:{year or 0}:{month or 0}:{page}",
                )
            ]
        )
    navigation = []
    if page:
        navigation.append(("⬅️ Назад", f"{callback}:{page - 1}"))
    if (page + 1) * 10 < total:
        navigation.append(("Далее ➡️", f"{callback}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    if total:
        blocks.append(f"Страница {page + 1} из {(total + 9) // 10}")
    if year is not None:
        rows.append([("⬅️ Назад к месяцам", f"director:year:{year}")])
    rows += [[("🔄 Обновить", f"{callback}:{page}")], [("🏠 Главное меню", "director:menu")]]
    await send("\n\n".join(blocks), keyboard(rows), answer="Список обновлён")


async def director_callback(session, user, draft, event, send, settings):
    if not is_director(event["user_id"], settings):
        return await send("Это действие доступно только директору.", answer="Недостаточно прав")
    parts = event["payload"].split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        numbers = [int(value) for value in parts[2:]]
        if any(value < 0 or value >= 2**63 for value in numbers):
            raise ValueError
    except ValueError:
        return await send(answer="Некорректная кнопка")
    current_year = now().astimezone(MOSCOW).year
    if action == "menu" and not numbers:
        return await send(
            WELCOME, await main_menu(session, event["user_id"], settings), answer="Главное меню"
        )
    if action == "inbox" and len(numbers) <= 1:
        return await repair_list(session, send, numbers[0] if numbers else 0)
    if action == "archive" and len(numbers) <= 1:
        years = list(range(2026, current_year + 1))
        page = min(numbers[0] if numbers else 0, max(0, (len(years) - 1) // 10))
        buttons = [
            (str(year), f"director:year:{year}") for year in years[page * 10 : page * 10 + 10]
        ]
        rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        navigation = []
        if page:
            navigation.append(("⬅️ Назад", f"director:archive:{page - 1}"))
        if (page + 1) * 10 < len(years):
            navigation.append(("Далее ➡️", f"director:archive:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("🏠 Главное меню", "director:menu")])
        return await send("📅 Выберите год:", keyboard(rows), answer="Архив заявок")
    if action == "year" and len(numbers) == 1 and 2026 <= numbers[0] <= current_year:
        year = numbers[0]
        buttons = [(name, f"director:month:{year}:{month}") for month, name in enumerate(MONTHS, 1)]
        rows = [buttons[i : i + 3] for i in range(0, 12, 3)]
        rows += [
            [("⬅️ К выбору года", f"director:archive:{(year - 2026) // 10}")],
            [("🏠 Главное меню", "director:menu")],
        ]
        return await send(f"📅 {year}: выберите месяц", keyboard(rows), answer="Выберите месяц")
    if (
        action == "month"
        and len(numbers) in (2, 3)
        and 2026 <= numbers[0] <= current_year
        and 1 <= numbers[1] <= 12
    ):
        year, month = numbers[:2]
        return await repair_list(session, send, numbers[2] if len(numbers) == 3 else 0, year, month)
    if action == "open" and len(numbers) in (1, 4) and numbers[0] > 0:
        back = "director:inbox"
        if len(numbers) == 4:
            _, year, month, page = numbers
            if year == month == 0:
                back += f":{page}"
            elif 2026 <= year <= current_year and 1 <= month <= 12:
                back = f"director:month:{year}:{month}:{page}"
            else:
                return await send(answer="Некорректная кнопка")
        repair = await session.get(Repair, numbers[0], with_for_update=True)
        if repair is None:
            return await send(answer="Заявка не найдена")
        repair.director_viewed_at = repair.director_viewed_at or now()
        author = await session.get(User, repair.author_id)
        photo = await session.scalar(select(RepairPhoto).where(RepairPhoto.repair_id == repair.id))
        card = director_card(repair, author, MOSCOW.key)
        buttons = card["attachments"][0] if card["attachments"] else keyboard([])
        buttons["payload"]["buttons"] += keyboard(
            [[("⬅️ К списку", back)], [("🏠 Главное меню", "director:menu")]]
        )["payload"]["buttons"]
        reference = (
            None
            if photo is None
            else dict(
                private_path=photo.private_path,
                checksum=photo.checksum,
                media_metadata=photo.media_metadata,
            )
        )
        return await send(card["text"], buttons, reference, answer="Заявка открыта")
    await send(answer="Некорректная или устаревшая кнопка")


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
        label = "✅ Подтвердить отклонение" if admin else "✅ Отправить"
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
    repair.director_viewed_at = repair.director_viewed_at or now()
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
        return await send(
            "Кнопка устарела. Выберите действие:",
            await main_menu(session, event["user_id"], settings),
            answer="Кнопка устарела",
        )
    action = parts[1]
    if action == "cancel":
        await session.delete(draft)
        return await send(
            "Черновик отменён.\n" + WELCOME,
            await main_menu(session, event["user_id"], settings),
            answer="Черновик закрыт",
        )
    if action == "back" and draft.stage == "description":
        draft.paused, draft.version = True, draft.version + 1
        return await send(
            "Черновик сохранён.\n" + WELCOME,
            await main_menu(session, event["user_id"], settings),
            answer="Черновик сохранён",
        )
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
            await main_menu(session, event["user_id"], settings),
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
        return await send(WELCOME + suffix, await main_menu(session, event["user_id"], settings))
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
            "director": director_callback,
        }.get(prefix)
        if handler:
            return await handler(
                session, user, draft if prefix == "repair" else admin, event, send, settings
            )
        return await send(
            WELCOME,
            await main_menu(session, event["user_id"], settings),
            answer="Неизвестная или устаревшая кнопка",
        )
    if admin or (draft and not draft.paused):
        return await draft_message(session, admin or draft, event, send, photos)
    await send(
        "Выберите действие в меню.\n" + WELCOME,
        await main_menu(session, event["user_id"], settings),
    )


def director_card(repair, author, timezone):
    created = repair.created_at.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
    text = (
        f"📋 Заявка №{repair.id}\n👤 Отправитель: {author.name}\n"
        f"🆔 MAX ID: {author.max_user_id}\n🔧 Неисправность: {repair.description}\n"
        f"📍 Место: {repair.location_text}\n🕒 Создана: {created}\n"
        f"📌 Статус: {STATUS[repair.status]}"
    )
    if repair.rejection_reason:
        text += f"\nПричина отклонения: {repair.rejection_reason}"
    buttons = []
    if repair.status not in ("done", "rejected"):
        actions = {"done": "✅ Выполнено", "pending": "🕓 Не выполнено", "reject": "❌ Отклонить"}
        rows = [[(label, f"admin:{action}:{repair.id}")] for action, label in actions.items()]
        buttons = [keyboard(rows)]
    return {"text": text, "attachments": buttons}
