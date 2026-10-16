import asyncio
import re
from datetime import timedelta
from difflib import SequenceMatcher
from functools import lru_cache
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, or_, select

from bot import keyboard, main_menu
from database import FoundDraft, FoundEvent, FoundItem, now, safe_error
from max_api import InvalidPhoto

# ==================== КОНСТАНТЫ И КЛАВИАТУРЫ ====================

PAGE_SIZE = 8
HOME = [("🏠 Главное меню", "found:home")]
SENSITIVE_TITLES = {"Паспорт", "Пропуск", "Банковская карта", "Карта", "Ключи", "Документ"}
INTRO = (
    "🎒 Бюро находок\n\nПотеряли вещь?\nПопробуйте найти её среди предметов, "
    "которые находятся на школьной вахте."
)
COLOURS = {
    "черн": ("чёрный", "чёрная", "чёрные"),
    "бел": ("белый", "белая", "белые"),
    "син": ("синий", "синяя", "синие"),
    "голуб": ("голубой", "голубая", "голубые"),
    "красн": ("красный", "красная", "красные"),
    "зелен": ("зелёный", "зелёная", "зелёные"),
    "желт": ("жёлтый", "жёлтая", "жёлтые"),
    "сер": ("серый", "серая", "серые"),
    "серебрист": ("серебристый", "серебристая", "серебристые"),
    "розов": ("розовый", "розовая", "розовые"),
    "фиолетов": ("фиолетовый", "фиолетовая", "фиолетовые"),
    "оранжев": ("оранжевый", "оранжевая", "оранжевые"),
    "коричнев": ("коричневый", "коричневая", "коричневые"),
}
ALIASES = {
    "сменка": "сменная обувь",
    "олимпийка": "спортивная кофта",
    "пеналчик": "пенал",
    "пенальчик": "пенал",
    "пенальчики": "пенал",
    "флешка": "usb накопитель",
    "флешки": "usb накопитель",
    "флеш": "usb",
    "ранец": "рюкзак",
    "портфель": "рюкзак",
    "рюкзачок": "рюкзак",
    "мобилка": "телефон",
    "наушнички": "наушники",
}
NOUNS = {
    "пенал": (0, "пенала пеналу пеналом пенале пеналы пеналов"),
    "рюкзак": (0, "рюкзака рюкзаку рюкзаком рюкзаке рюкзаки рюкзаков"),
    "кофта": (1, "кофты кофту кофте кофтой кофт"),
    "флешка": (1, "флешки флешку флешке флешкой флешек"),
    "шапка": (1, "шапки шапку шапке шапкой шапок"),
    "телефон": (0, "телефона телефону телефоном телефоне телефоны телефонов"),
    "наушники": (2, "наушников наушникам наушниками наушниках"),
    "ключи": (2, "ключей ключам ключами ключах"),
}
ENDINGS = (
    "иями",
    "ами",
    "ями",
    "ого",
    "ему",
    "ому",
    "ыми",
    "ими",
    "иях",
    "ах",
    "ях",
    "ов",
    "ев",
    "ей",
    "ый",
    "ий",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ой",
    "ом",
    "ым",
    "ем",
    "ам",
    "ям",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
    "о",
    "ь",
)
STOP_WORDS = set(
    "я мы у меня мой моя мое мои потерял потеряла потеряли потерян "
    "найти ищу где вещь пожалуйста это".split()
)
KEYBOARD = str.maketrans("qwertyuiop[]asdfghjkl;'zxcvbnm,.", "йцукенгшщзхъфывапролджэячсмитьбю")


def is_guard(user_id, settings):
    return bool(
        settings.guard_user_id and settings.guard_chat_id and user_id == settings.guard_user_id
    )


def guard_allowed(user, event, settings):
    return bool(
        is_guard(event["user_id"], settings)
        and event["private"]
        and user.private_chat_verified
        and user.started_at
        and user.chat_id == settings.guard_chat_id
        and event["chat_id"] in (None, settings.guard_chat_id)
    )


def public_description(description):
    text = description.casefold().replace("ё", "е")
    for pattern, label in (
        (r"паспорт", "Паспорт"),
        (r"пропуск", "Пропуск"),
        (
            r"(?:банковск|кредитн|дебетов).*карт|карт.*(?:банк|visa|мир|mastercard|\d{4})",
            "Банковская карта",
        ),
        (r"ключ", "Ключи"),
        (r"\bкарт(?:а|очка|очки)\b", "Карта"),
        (r"удостоверени|документ|свидетельств", "Документ"),
    ):
        if re.search(pattern, text):
            return label
    return description


# ==================== ПОИСК ПРЕДМЕТОВ ====================


@lru_cache(maxsize=4096)
def terms(text):
    words = re.findall(r"[a-zа-я0-9]+", text.casefold().replace("ё", "е"))
    words = " ".join(ALIASES.get(word, word) for word in words if word not in STOP_WORDS).split()
    result = []
    for word in words:
        for ending in ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                word = word[: -len(ending)]
                break
        result.append(word)
    return tuple(result)


def search_score(query, description, keywords=()):
    title = public_description(description)
    words = list(terms(title))
    if title == description:
        words += [word for variant in keywords for word in terms(variant)]
    words = list(dict.fromkeys(words))
    if not words:
        return 0
    translit = query.casefold()
    for latin, russian in (
        ("shch", "щ"),
        ("sch", "щ"),
        ("zh", "ж"),
        ("kh", "х"),
        ("ch", "ч"),
        ("sh", "ш"),
        ("yu", "ю"),
        ("ya", "я"),
        ("yo", "ё"),
        ("ts", "ц"),
    ):
        translit = translit.replace(latin, russian)
    translit = translit.translate(
        str.maketrans("abvgdezijklmnoprstufhyc", "абвгдезийклмнопрстуфхыц")
    )
    best = 0
    item_colours = set(terms(title)) & COLOURS.keys()
    for variant in dict.fromkeys((query, query.casefold().translate(KEYBOARD), translit)):
        wanted = terms(variant)
        if not wanted:
            continue
        colours = set(wanted) & COLOURS.keys()
        if colours and item_colours and not colours <= item_colours:
            continue
        scores = []
        for word in wanted:
            if word in words:
                scores.append(1)
                continue
            candidates = words
            if len(word) > 7:
                candidates = words + [
                    left + right for left in words[:8] for right in words[:8] if left != right
                ]
            scores.append(
                max(
                    (
                        SequenceMatcher(None, word, other).ratio()
                        for other in candidates
                        if min(len(word), len(other)) > 3
                        and abs(len(word) - len(other)) <= max(2, len(word) // 3)
                    ),
                    default=0,
                )
            )
        if min(scores) >= 0.66:
            score = sum(scores) / len(scores)
            if colours and not item_colours:
                score *= 0.85
            best = max(best, score)
    return best if best >= 0.72 else 0


def suggested_title(description):
    words = description.casefold().replace("ё", "е").split()
    for noun, (gender, forms) in NOUNS.items():
        if not any(word in [noun, *forms.split()] for word in words):
            continue
        corrected = []
        for word in words:
            if word in forms.split():
                word = noun
            else:
                for root, variants in COLOURS.items():
                    if word.startswith(root) and word in {v.replace("ё", "е") for v in variants} | {
                        root + suffix
                        for suffix in ("ого", "ому", "ым", "ой", "ей", "ую", "юю", "ыми", "их")
                    }:
                        word = variants[gender]
                        break
                for root in ("школьн", "спортивн"):
                    if word.startswith(root):
                        word = root + ("ый", "ая", "ые")[gender]
            corrected.append(word)
        result = " ".join(corrected)
        if result.replace("ё", "е") != description.casefold().replace("ё", "е"):
            return result.capitalize() if description[:1].isupper() else result
    return None


async def list_items(session, draft, send, settings, admin=False):
    if draft.view == "issued" and not admin:
        return await send(answer="Недостаточно прав")
    status = "issued" if draft.view == "issued" else "stored"
    items = list(
        (
            await session.scalars(
                select(FoundItem)
                .where(FoundItem.status == status)
                .order_by(FoundItem.created_at.desc(), FoundItem.id.desc())
            )
        ).all()
    )
    if draft.view == "search":
        ranked = [
            (search_score(draft.query or "", item.description, item.keywords), item)
            for item in items
        ]
        items = [
            item for score, item in sorted(ranked, key=lambda pair: pair[0], reverse=True) if score
        ]
    total = len(items)
    draft.page = min(draft.page, max(0, (total - 1) // PAGE_SIZE))
    title = (
        f"🔎 Найдено предметов: {total}"
        if draft.view == "search"
        else f"📁 Выданные предметы: {total}"
        if draft.view == "issued"
        else f"📦 Предметы на хранении: {total}"
    )
    if not total:
        title = "😔 Подходящих предметов пока не найдено.\nПопробуйте другое название."
    rows, blocks = [], [title]
    timezone = ZoneInfo(settings.app_timezone)
    for item in items[draft.page * PAGE_SIZE : (draft.page + 1) * PAGE_SIZE]:
        description = item.description if admin else public_description(item.description)
        block = f"📦 №{item.id} — {description}"
        if draft.view == "issued":
            block += (
                f"\nДобавлен: {item.created_at.astimezone(timezone):%d.%m.%Y}"
                f" · Выдан: {item.issued_at.astimezone(timezone):%d.%m.%Y}"
            )
        blocks.append(block)
        rows.append([(f"📦 Открыть №{item.id}", f"found:open:{item.id}")])
    navigation = []
    if draft.page:
        navigation.append(("⬅️ Предыдущая страница", f"found:list:{draft.view}:{draft.page - 1}"))
    if (draft.page + 1) * PAGE_SIZE < total:
        navigation.append(("➡️ Следующая страница", f"found:list:{draft.view}:{draft.page + 1}"))
    if navigation:
        rows.append(navigation)
    rows += [
        [("🔎 Новый поиск", "found:search")],
        [("📦 Все предметы", "found:list:stored:0")],
        [("⬅️ Бюро находок", "found:menu")],
        HOME,
    ]
    await send("\n\n".join(blocks), keyboard(rows), answer="Список обновлён")


# ==================== ДОБАВЛЕНИЕ ПРЕДМЕТОВ ====================


async def show_menu(draft, send, admin):
    if admin:
        rows = [
            [
                (
                    "➕ Продолжить добавление / изменение"
                    if draft.stage != "menu"
                    else "➕ Добавить предмет",
                    "found:add",
                )
            ],
            [("📦 Предметы на хранении", "found:list:stored:0")],
            [("🔎 Найти предмет", "found:search")],
            [("📁 Выданные предметы", "found:list:issued:0")],
            HOME,
        ]
    else:
        rows = [
            [("🔎 Найти вещь", "found:search")],
            [("📦 Все найденные вещи", "found:list:stored:0")],
            HOME,
        ]
    await send("🎒 Бюро находок — вахта" if admin else INTRO, keyboard(rows), answer="Бюро находок")


async def show_form(session, draft, send, prefix=""):
    suffix = f"{draft.nonce}:{draft.version}"
    rows = []
    if draft.stage == "photo":
        text = "📷 Прикрепите фотографию найденного предмета.\nОтправьте одно изображение."
        if draft.photo_ref:
            text += "\nФотография сохранена; можно прислать другую."
            rows.append([("➡️ Оставить фотографию", f"found:next:{suffix}")])
    elif draft.stage == "description":
        text = (
            "✏️ Напишите краткое название предмета в именительном падеже, в начальной форме "
            "(до 120 символов).\n\nПримеры:\nЧёрный школьный пенал\nСиняя спортивная кофта\n"
            "Белые беспроводные наушники\nСеребристая флешка\n\n"
            "Не указывайте номера документов и коды."
        )
        if draft.description:
            text += f"\n\nСохранено: {draft.description}"
            rows.append([("➡️ Оставить описание", f"found:next:{suffix}")])
    else:
        text = (
            "📦 "
            + ("Новый найденный предмет" if draft.operation == "add" else "Изменение предмета")
            + "\n\n"
            f"📷 Фотография прикреплена\n📝 Название: {draft.description}\n\nВсё правильно?"
        )
        duplicates = list(
            (
                await session.scalars(
                    select(FoundItem.id)
                    .where(
                        FoundItem.status == "stored",
                        FoundItem.id != (draft.item_id or 0),
                        or_(
                            func.lower(func.replace(FoundItem.description, "ё", "е"))
                            == (draft.description or "").casefold().replace("ё", "е"),
                            FoundItem.photo_ref["checksum"].as_string()
                            == (draft.photo_ref or {}).get("checksum"),
                        ),
                    )
                    .limit(3)
                )
            ).all()
        )
        if duplicates:
            text += "\n\n⚠️ Похожий предмет уже есть: " + ", ".join(f"№{n}" for n in duplicates)
            text += ". Можно сохранить ещё один экземпляр."
        rows += [
            [("✅ Сохранить", f"found:save:{suffix}")],
            [("✏️ Изменить описание", f"found:description:{suffix}")],
            [("📷 Заменить фотографию", f"found:photo:{suffix}")],
        ]
        if draft.suggestion:
            text += f"\n\nВозможная начальная форма: {draft.suggestion}"
            rows.append([("✏️ Использовать предложенную форму", f"found:suggest:{suffix}")])
    rows.append([("⬅️ Назад", f"found:back:{suffix}"), ("❌ Отмена", f"found:cancel:{suffix}")])
    await send(
        prefix + text,
        keyboard(rows),
        draft.photo_ref if draft.stage in ("photo", "confirm") else None,
        answer="Данные сохранены",
    )


async def save_form(session, draft, event, send, photos):
    if (
        not draft.photo_ref
        or not draft.description
        or not 0 < len(draft.description.strip()) <= 120
    ):
        return await show_form(session, draft, send, "Нужны фотография и краткое описание.\n")
    try:
        await asyncio.to_thread(photos.verify, draft.photo_ref)
    except (InvalidPhoto, OSError):
        return await show_form(session, draft, send, "Фотография недоступна. Замените её.\n")
    if draft.operation == "add":
        item = FoundItem(
            description=draft.description, photo_ref=draft.photo_ref, created_by=event["user_id"]
        )
        session.add(item)
        await session.flush()
        action = "created"
    else:
        item = await session.get(FoundItem, draft.item_id, with_for_update=True)
        if item is None or item.version != draft.item_version:
            return await show_form(
                session, draft, send, "Предмет уже изменён. Отмените форму и откройте его заново.\n"
            )
        if item.description != draft.description:
            item.description, item.keywords, item.keywords_for = draft.description, [], None
            item.attempts, item.next_retry_at, item.last_error = 0, now(), None
        item.photo_ref, item.version = draft.photo_ref, item.version + 1
        action = "edited"
    session.add(FoundEvent(item_id=item.id, actor_id=event["user_id"], action=action))
    draft.stage, draft.version, draft.paused = "menu", draft.version + 1, False
    draft.description, draft.suggestion, draft.photo_ref = None, None, None
    draft.item_id, draft.item_version, draft.confirmation = None, None, None
    await send(
        f"✅ Предмет №{item.id} {'добавлен' if action == 'created' else 'обновлён'}!\n\n"
        f"Название: {item.description}\n\nПредмет доступен для поиска."
        if item.status == "stored"
        else f"✅ Предмет №{item.id} обновлён в архиве.",
        keyboard(
            [
                [("➕ Добавить ещё", "found:add")],
                [("📦 Предметы на хранении", "found:list:stored:0")],
                HOME,
            ]
        ),
        answer="Предмет сохранён",
    )


# ==================== РЕДАКТИРОВАНИЕ И ВЫДАЧА ====================


async def show_item(session, draft, item, send, settings, admin):
    if item is None or (item.status != "stored" and not admin):
        return await send("Предмет недоступен или уже выдан.", answer="Предмет недоступен")
    title = item.description if admin else public_description(item.description)
    timezone = ZoneInfo(settings.app_timezone)
    text = (
        f"📦 Предмет №{item.id}\n\n📝 {title}\n"
        f"📅 Добавлен: {item.created_at.astimezone(timezone):%d.%m.%Y}"
    )
    rows = []
    if admin:
        text += "\n📌 Статус: " + ("На хранении" if item.status == "stored" else "Выдан")
        if item.issued_at:
            text += f"\n📅 Выдан: {item.issued_at.astimezone(timezone):%d.%m.%Y %H:%M}"
        action, label = (
            ("issue", "✅ Отметить выданным")
            if item.status == "stored"
            else ("restore", "↩️ Вернуть в список хранения")
        )
        rows = [
            [(label, f"found:{action}:{item.id}:{item.version}")],
            [("✏️ Изменить описание", f"found:editdesc:{item.id}:{item.version}")],
            [("📷 Заменить фото", f"found:editphoto:{item.id}:{item.version}")],
        ]
    else:
        text += (
            "\n\n📍 Вещь находится на школьной вахте.\n"
            "Если это ваша вещь, подойдите на вахту и обратитесь к сотруднику школы."
        )
    photo = item.photo_ref if admin or title not in SENSITIVE_TITLES else None
    if photo is None:
        text += "\n\nФотографию и подробности документа или ключей уточните на вахте."
    rows += [
        [("⬅️ К результатам", f"found:list:{draft.view}:{draft.page}")],
        [("🔎 Новый поиск", "found:search")],
        HOME,
    ]
    await send(text, keyboard(rows), photo, answer="Карточка открыта")


async def change_status(session, draft, event, send):
    confirmation = draft.confirmation
    if not confirmation or event["payload"] != "found:commit:" + confirmation["nonce"]:
        return await send(answer="Подтверждение устарело")
    item = await session.get(FoundItem, confirmation["id"], with_for_update=True)
    draft.confirmation = None
    if not item or item.version != confirmation["version"] or item.status == confirmation["status"]:
        return await send(
            "Предмет уже изменён. Откройте карточку заново.", answer="Кнопка устарела"
        )
    details = {
        "previous_status": item.status,
        "previous_issued_at": item.issued_at.isoformat() if item.issued_at else None,
        "previous_issued_by": item.issued_by,
    }
    item.status, item.version = confirmation["status"], item.version + 1
    item.issued_at = now() if item.status == "issued" else None
    item.issued_by = event["user_id"] if item.status == "issued" else None
    session.add(
        FoundEvent(
            item_id=item.id,
            actor_id=event["user_id"],
            action="issued" if item.status == "issued" else "restored",
            details=details,
        )
    )
    await send(
        f"✅ Предмет №{item.id} "
        + ("выдан." if item.status == "issued" else "возвращён в список хранения."),
        keyboard(
            [
                [("📦 Предметы на хранении", "found:list:stored:0")],
                [("📁 Выданные предметы", "found:list:issued:0")],
                HOME,
            ]
        ),
        answer="Статус обновлён",
    )


# ==================== ОБРАБОТКА СООБЩЕНИЙ ====================


async def found_message(session, user, draft, event, send, settings, photos):
    text = " ".join(event["text"].split())
    if draft.searching:
        if event["attachments"] or not 0 < len(text) <= 200:
            return await send(
                "Напишите название вещи текстом, до 200 символов.",
                keyboard([[("❌ Отмена", "found:menu")]]),
            )
        draft.query, draft.view, draft.page, draft.searching = text, "search", 0, False
        return await list_items(
            session, draft, send, settings, guard_allowed(user, event, settings)
        )
    if not guard_allowed(user, event, settings):
        draft.paused = True
        return await send("Это действие доступно только вахтёрше в настроенном личном диалоге.")
    if draft.stage == "photo":
        if len(event["attachments"]) != 1 or event["attachments"][0].get("type") != "image":
            return await show_form(session, draft, send, "Пришлите ровно одно изображение.\n")
        try:
            draft.photo_ref = await photos.receive(event["attachments"][0])
        except (InvalidPhoto, httpx.HTTPError, OSError):
            return await show_form(
                session,
                draft,
                send,
                "Не удалось сохранить фото. Пришлите новое изображение до 10 МБ.\n",
            )
        draft.stage = "confirm" if draft.operation == "editphoto" else "description"
    elif draft.stage == "description":
        if event["attachments"] or not 0 < len(text) <= 120:
            return await show_form(
                session, draft, send, "Нужно непустое название до 120 символов.\n"
            )
        draft.description, draft.suggestion = text, suggested_title(text)
        draft.stage = "confirm"
    else:
        return await show_form(
            session, draft, send, "Используйте кнопки подтверждения или возврата.\n"
        )
    draft.version += 1
    await show_form(session, draft, send)


# ==================== ОБРАБОТКА CALLBACK ====================


async def found_callback(session, user, draft, event, send, settings, photos):
    if len(event["payload"]) > 200:
        return await send(answer="Некорректная кнопка")
    admin = guard_allowed(user, event, settings)
    parts = event["payload"].split(":")
    action = parts[1] if len(parts) > 1 else ""
    public = {"menu", "home", "search", "list", "open"}
    if action not in public and not admin:
        return await send(
            "Управление бюро находок доступно только вахтёрше в подтверждённом личном диалоге.",
            answer="Недостаточно прав",
        )
    if draft is None:
        draft = FoundDraft(user_id=event["user_id"])
        session.add(draft)
        await session.flush()
    if action in public:
        draft.searching, draft.confirmation = False, None
        if draft.stage != "menu" and not draft.paused:
            draft.paused, draft.version = True, draft.version + 1
    if action in ("home", "menu") and len(parts) == 2:
        if action == "home":
            return await send(
                "Выберите нужный раздел:",
                await main_menu(session, event["user_id"], settings),
                answer="Главное меню",
            )
        return await show_menu(draft, send, admin)
    if action == "search" and len(parts) == 2:
        draft.searching = True
        return await send(
            "🔎 Напишите название потерянной вещи.\n\n"
            "Например: Чёрный пенал, Синяя кофта, Наушники, Сменка.\n"
            "Можно писать своими словами.",
            keyboard([[("❌ Отмена", "found:menu")], HOME]),
            answer="Введите название",
        )
    if (
        action == "list"
        and len(parts) == 4
        and parts[2] in ("stored", "issued", "search")
        and parts[3].isdigit()
        and len(parts[3]) < 9
    ):
        if parts[2] == "issued" and not admin:
            return await send(answer="Недостаточно прав")
        draft.view, draft.page = parts[2], int(parts[3])
        return await list_items(session, draft, send, settings, admin)
    if action == "open" and len(parts) == 3 and parts[2].isdigit() and 0 < int(parts[2]) < 2**63:
        item = await session.get(FoundItem, int(parts[2]))
        return await show_item(session, draft, item, send, settings, admin)
    if action == "add" and len(parts) == 2:
        if draft.stage == "menu":
            draft.stage, draft.operation, draft.nonce = "photo", "add", uuid4().hex
        draft.paused, draft.searching, draft.version = False, False, draft.version + 1
        return await show_form(session, draft, send)
    if action in ("editdesc", "editphoto", "issue", "restore") and len(parts) == 4:
        if not all(p.isdigit() and 0 < int(p) < 2**63 for p in parts[2:]):
            return await send(answer="Некорректная кнопка")
        item = await session.get(FoundItem, int(parts[2]), with_for_update=True)
        if item is None or item.version != int(parts[3]):
            return await send(answer="Кнопка устарела. Откройте карточку заново")
        if action in ("issue", "restore"):
            target = "issued" if action == "issue" else "stored"
            if item.status == target:
                return await send(answer="Статус уже установлен")
            nonce = uuid4().hex
            draft.confirmation = {
                "id": item.id,
                "version": item.version,
                "status": target,
                "nonce": nonce,
            }
            draft.paused, draft.searching = True, False
            question = (
                f"Вы действительно выдали предмет №{item.id}?"
                if target == "issued"
                else f"Вернуть предмет №{item.id} в список хранения?"
            )
            label = "✅ Да, предмет выдан" if target == "issued" else "↩️ Да, вернуть на хранение"
            return await send(
                question,
                keyboard(
                    [[(label, "found:commit:" + nonce)], [("❌ Отмена", f"found:open:{item.id}")]]
                ),
                answer="Требуется подтверждение",
            )
        if draft.stage != "menu":
            return await send(
                "Есть сохранённая форма. Продолжите или отмените её.",
                keyboard([[("Продолжить форму", "found:add")], HOME]),
                answer="Черновик сохранён",
            )
        draft.item_id, draft.item_version, draft.operation = item.id, item.version, action
        draft.description, draft.photo_ref = item.description, item.photo_ref
        draft.stage = "description" if action == "editdesc" else "photo"
        draft.paused, draft.searching, draft.nonce = False, False, uuid4().hex
        draft.version += 1
        return await show_form(session, draft, send)
    if action == "commit" and len(parts) == 3:
        return await change_status(session, draft, event, send)
    if (
        len(parts) != 4
        or parts[2] != draft.nonce
        or parts[3] != str(draft.version)
        or draft.paused
        or draft.stage == "menu"
    ):
        if draft.stage != "menu" and not draft.paused:
            return await show_form(session, draft, send, "Кнопка устарела. Текущий шаг:\n")
        return await show_menu(draft, send, admin)
    if action == "cancel":
        draft.stage, draft.paused, draft.searching = "menu", False, False
        draft.photo_ref, draft.description, draft.suggestion, draft.confirmation = (
            None,
            None,
            None,
            None,
        )
        draft.item_id, draft.item_version = None, None
        draft.version += 1
        return await show_menu(draft, send, admin)
    if action == "save" and draft.stage == "confirm":
        return await save_form(session, draft, event, send, photos)
    if action == "suggest" and draft.stage == "confirm" and draft.suggestion:
        draft.description, draft.suggestion = draft.suggestion, None
    elif action in ("photo", "description") and draft.stage == "confirm":
        draft.stage = action
    elif action == "back":
        if draft.stage == "photo" or (
            draft.stage == "description" and draft.operation == "editdesc"
        ):
            draft.paused = True
            draft.version += 1
            return await show_menu(draft, send, admin)
        draft.stage = (
            "photo"
            if draft.stage == "description" or draft.operation == "editphoto"
            else "description"
        )
    elif action == "next" and draft.stage in ("photo", "description"):
        if not (draft.photo_ref if draft.stage == "photo" else draft.description):
            return await show_form(session, draft, send, "Сначала заполните этот шаг.\n")
        draft.stage = (
            "description"
            if draft.stage == "photo" and draft.operation != "editphoto"
            else "confirm"
        )
    else:
        return await send(answer="Действие недоступно")
    draft.version += 1
    await show_form(session, draft, send)


# ==================== ФОНОВАЯ ГЕНЕРАЦИЯ ПОИСКОВЫХ ВАРИАНТОВ ====================


async def index_one(db, client):
    if client.http is None:
        return False
    async with db.sessions() as session, session.begin():
        item = await session.scalar(
            select(FoundItem)
            .where(
                FoundItem.keywords_for.is_distinct_from(FoundItem.description),
                FoundItem.next_retry_at <= now(),
            )
            .order_by(FoundItem.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            return False
        identity, description = item.id, item.description
        item.next_retry_at = now() + timedelta(minutes=2)
    try:
        keywords = await client.keywords(public_description(description))
        source_colours = set(terms(description)) & COLOURS.keys()
        keywords = [
            word
            for word in keywords
            if (set(terms(word)) & COLOURS.keys()) <= source_colours
            and set(re.findall(r"\d+", word)) <= set(re.findall(r"\d+", description))
        ]
        error = None
    except Exception as exc:
        keywords, error = [], safe_error(exc)
    async with db.sessions() as session, session.begin():
        item = await session.get(FoundItem, identity, with_for_update=True)
        if item is None or item.description != description or item.keywords_for == description:
            return True
        if error:
            item.attempts += 1
            item.last_error = error
            item.next_retry_at = now() + timedelta(seconds=min(3600, 2 ** min(item.attempts, 12)))
        else:
            item.keywords, item.keywords_for, item.last_error = keywords, description, None
            item.attempts = 0
    return True
