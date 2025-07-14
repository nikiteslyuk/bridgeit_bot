import logging
import os
import uuid
import mimetypes
import json
import datetime
import humanize
from functools import wraps
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)
from telegram import BotCommand
from telegram.error import BadRequest
from telegram.constants import ParseMode

from telegram.request import HTTPXRequest

from logic import BridgeLogic, SUIT_ICONS
from detection import BridgeCardDetector



os.makedirs("img", exist_ok=True)

# TOKEN = os.getenv("TG_TOKEN")
TOKEN = "7976805123:AAHpYOm43hazvkXUlDY-q4X9US18upq9uak"
AUTHORIZED_ID = [375025446, 855302541, 5458141225]
UNLIMITED_ID = [375025446, 855302541]
logging.basicConfig(level=logging.INFO)
req = HTTPXRequest(connection_pool_size=10, connect_timeout=10.0, read_timeout=60.0, write_timeout=60.0, pool_timeout=10.0)


# === ОГРАНИЧЕНИЯ ================================================================
CONTEXT_TTL_MIN = 5

PHOTO_LIMIT_COUNT = 1
PHOTO_LIMIT_INTERVAL_MIN = 20
PBN_LIMIT_COUNT = 1
PBN_LIMIT_INTERVAL_MIN = 20
CACHED_PHOTO_DATABASE_NAME = "photo_requests.json"
CACHED_PBN_DATABASE_NAME = "pbn_requests.json"


# === СОСТОЯНИЯ ================================================================
STATE_AWAIT_PBN = "await_pbn"
STATE_AWAIT_PHOTO = "await_photo"

# === ДОБАВОЧНЫЕ СОСТОЯНИЯ =====================================================
STATE_ADD_CARD_SELECT_CARD   = "add_card_select_card"
STATE_ADD_CARD_SELECT_HAND   = "add_card_select_hand"
STATE_MOVE_CARD_SELECT_HAND  = "move_card_select_hand"
STATE_MOVE_CARD_SELECT_CARD  = "move_card_select_card"
STATE_MOVE_CARD_SELECT_DEST  = "move_card_select_dest"
STATE_CONTRACT_CHOOSE_DENOM = "contract_choose_denom"
STATE_CONTRACT_CHOOSE_FIRST = "contract_choose_first"


SUITS = ("S", "H", "D", "C")
RANKS = ("A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2")

# === ОГРАНИЧЕНИЕ ВРЕМЕНИ ЖИЗНИ КОНТЕКСТА ======================================

def _touch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновляем отметку последнего действия пользователя."""
    context.user_data['last_access'] = datetime.datetime.now()


def _expire_if_needed(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляем расклад/детектор и прочий state, если истёк TTL."""
    last: datetime.datetime | None = context.user_data.get('last_access')
    if last and (datetime.datetime.now() - last
                 > datetime.timedelta(minutes=CONTEXT_TTL_MIN)):
        for key in ('logic', 'detector', 'state', 'active_msg_id',
                    'show_funcs', 'highlight_moves', 'contract_set',
                    'chosen_denom', 'pending_card', 'pending_hand_src'):
            context.user_data.pop(key, None)


def with_expire(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        _expire_if_needed(context)
        result = await handler(update, context)
        _touch(context)
        return result
    return wrapper


def chunk(seq, size=7):
    """Разбивает последовательность на куски не больше size элементов."""
    for i in range(0, len(seq), size):
        yield seq[i:i+size]


def _pretty(card: str) -> str:
    """'AS' → '♠A', 'TD' → '♦10' (иконки берём из SUIT_ICONS)"""
    rank = "10" if card[0] == "T" else card[0]
    return f"{SUIT_ICONS[card[1]]}{rank}"

# === АВТОРИЗАЦИЯ ==============================================================

def require_auth(handler_func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid not in AUTHORIZED_ID:
            if update.message:
                await update.message.reply_text(
                    "❌ Вы не можете принять участие в бета‑тестировании. Возвращайтесь позже!"
                )
            elif update.callback_query:
                await update.callback_query.answer("Доступ запрещён", show_alert=True)
            return
        return await handler_func(update, context)

    return wrapper


def ignore_telegram_edit_errors(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except BadRequest as e:
            if "Message is not modified" in str(e) or "message to edit not found" in str(e):
                return
            raise
    return wrapper


async def _show_active_window(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (context.user_data.get("logic") or
            context.user_data.get("detector") or
            context.user_data.get("state")):
        return

    chat_id = update.effective_chat.id

    logic: BridgeLogic | None = context.user_data.get("logic")
    contract_set = context.user_data.get("contract_set", False)
    state = context.user_data.get("state")

    if logic and contract_set:
        kb = make_board_keyboard(
            logic,
            context.user_data.get("show_funcs", False),
            context.user_data.get("highlight_moves", False),
        )
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=_pre(logic.display()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        context.user_data["active_msg_id"] = sent.message_id

    elif logic and not contract_set:
        if state == STATE_CONTRACT_CHOOSE_FIRST:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text="Кто делает первый ход?",
                reply_markup=contract_first_keyboard(),
            )
        else:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text="Выберите деноминацию контракта:",
                reply_markup=contract_denom_keyboard(),
            )
        context.user_data["active_msg_id"] = sent.message_id

    else:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text="Выберите действие:",
            reply_markup=main_menu_markup(),
        )
        context.user_data["active_msg_id"] = sent.message_id


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Я понимаю только команды.\n"
        "Пожалуйста, общайся со мной на языке команд."
    )
    await _show_active_window(update, context)


def require_fresh_window(handler):
    """Декоратор: если это неактуальное окно — показываем сообщение и молча выходим."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        last_id = context.user_data.get("active_msg_id")

        if last_id is None or query.message.message_id != last_id:
            try:
                await query.edit_message_text(
                    "⚠️ Это неактуальное окно.\n"
                    "Используйте кнопки из самого последнего сообщения.",
                    reply_markup=None,
                )
            except BadRequest:
                pass
            try:
                await query.answer()
            except BadRequest:
                pass

            return

        return await handler(update, context)
    return wrapper


def generate_filename() -> str:
    """
    Возвращает случайное имя файла с расширением .jpg,
    например: 'a1b2c3d4e5f6.jpg'
    """
    return f"img/{uuid.uuid4().hex}.jpg"


async def ignore_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Просто ничего не делать
    return

# === УТИЛИТЫ ОТПРАВКИ СООБЩЕНИЙ =============================================

def _pre(text: str) -> str:
    """Оборачиваем в тройные бэктики, чтобы Telegram показал моно‑шрифт."""
    return f"```\n{text}\n```"


async def safe_send(chat_func, text: str, **kwargs):
    """Безопасно отправляет длинные сообщения (разбивает >4096 символов)."""
    MAX_LEN = 4000  # запас под форматирование
    for i in range(0, len(text), MAX_LEN):
        chunk = text[i : i + MAX_LEN]
        await chat_func(chunk, **kwargs)

# === ХЕЛПЕРЫ ДЛЯ КЛАВИАТУР ====================================================

async def _show_history_with_back(query, logic):
    txt = _pre(logic.show_history())
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="act_back")]])
    await query.edit_message_text(text=txt, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)


def goto_trick_keyboard(total: int) -> InlineKeyboardMarkup:
    rows = []
    nums = [str(i) for i in range(1, total + 1)]
    for part in chunk(nums, 4):
        rows.append([InlineKeyboardButton(n, callback_data=f"goto_trick_{n}") for n in part])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="goto_cancel")])
    return InlineKeyboardMarkup(rows)


def goto_card_keyboard(cards: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(lbl, callback_data=f"goto_card_{idx + 1}") for idx, lbl in enumerate(cards)]]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="goto_cancel")])
    return InlineKeyboardMarkup(rows)


def make_board_keyboard(logic: BridgeLogic, show_funcs: bool = False, highlight: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton("🗑️ Отменить ход", callback_data="act_undo"),
        InlineKeyboardButton("⭐ Оптимальный ход", callback_data="act_optimal"),
    ])
    if show_funcs:
        rows.append([
            InlineKeyboardButton("⏭️ Доиграть до конца", callback_data="act_playtoend"),
        ])
        rows.append([
            InlineKeyboardButton("📜 История",           callback_data="act_history"),
            InlineKeyboardButton("📊 DD-таблица",        callback_data="act_ddtable"),
        ])
        rows.append([
            InlineKeyboardButton(
                "🔦 Подсветить ходы" if not highlight else "🚫 Скрыть подсветку",
                callback_data="act_highlight",
            ),
            InlineKeyboardButton("⤴️ К карте", callback_data="act_gotocard"),
        ])
    else:
        moves = logic.legal_moves()
        trick_map = logic.move_options() if highlight else {}
        for suit in SUITS:
            suit_cards = [c for c in moves if c.endswith(suit)]
            suit_cards.sort(key=lambda c: RANKS.index(c[0]))
            for part in chunk(suit_cards, 7):
                btns = []
                for c in part:
                    label = f"{_pretty(c)}: {trick_map[c]}" if c in trick_map else _pretty(c)
                    btns.append(InlineKeyboardButton(label, callback_data=f"play_{c}"))
                rows.append(btns)
    toggle_label = "🛠️ Опции" if not show_funcs else "🃏 Карты"
    rows.append([InlineKeyboardButton(toggle_label, callback_data="act_toggle")])
    return InlineKeyboardMarkup(rows)


def card_keyboard(cards: list[str]) -> InlineKeyboardMarkup:
    """Строит клавиатуру из списка карт с символами мастей."""
    rows = []
    for suit in SUITS:
        suit_cards = [c for c in cards if c.endswith(suit)]
        suit_cards.sort(key=lambda c: RANKS.index(c[0]))  # A-K-Q-…-2
        for part in chunk(suit_cards, 7):
            rows.append([
                InlineKeyboardButton(_pretty(c), callback_data=f"sel_card_{c}")
                for c in part
            ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cancel_add_move")])
    return InlineKeyboardMarkup(rows)


def hand_keyboard(prompt_back: str = "⬅️ Назад", back_data: str = "cancel_add_move") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("⬆️ North",  callback_data="hand_N"),
            InlineKeyboardButton("➡️ East",   callback_data="hand_E"),
        ],
        [
            InlineKeyboardButton("⬅️ West",   callback_data="hand_W"),
            InlineKeyboardButton("⬇️ South",  callback_data="hand_S"),
        ],
        [InlineKeyboardButton(prompt_back, callback_data=back_data)]
    ]
    return InlineKeyboardMarkup(rows)


def contract_denom_keyboard() -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("♣", callback_data="denom_C"),
        InlineKeyboardButton("♦", callback_data="denom_D"),
        InlineKeyboardButton("♥", callback_data="denom_H"),
        InlineKeyboardButton("♠", callback_data="denom_S"),
        InlineKeyboardButton("NT", callback_data="denom_NT"),
    ]]
    return InlineKeyboardMarkup(rows)


def contract_first_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("⬆️ North",  callback_data="first_N"),
            InlineKeyboardButton("➡️ East",   callback_data="first_E"),
        ],
        [
            InlineKeyboardButton("⬅️ West",   callback_data="first_W"),
            InlineKeyboardButton("⬇️ South",  callback_data="first_S"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Анализ расклада по фото", callback_data="input_photo")],
        [InlineKeyboardButton("📄 Анализ расклада по PBN",  callback_data="input_pbn")],
        [InlineKeyboardButton("📘 Документация",       callback_data="menu_docs")],
    ])


def analyze_result_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("↩️ По часовой", callback_data="rotate_cw"),
            InlineKeyboardButton("↪️ Против часовой", callback_data="rotate_ccw"),
        ],
        [
            InlineKeyboardButton("➕ Добавить карту",           callback_data="add_card_start"),
            InlineKeyboardButton("🔀 Переместить карту",        callback_data="move_card_start"),
        ],
        [
            InlineKeyboardButton("✅ Принять расклад",            callback_data="accept_result"),
        ],
    ])

# === СОЗДАТЬ / ОБНОВИТЬ BridgeLogic ДЛЯ ПОЛЬЗОВАТЕЛЯ ===========================

def set_logic_from_pbn(context: ContextTypes.DEFAULT_TYPE, pbn: str) -> BridgeLogic:
    """
    Создаёт новый BridgeLogic(PBN), кладёт в user_data и
    гарантированно обнуляет «визуальные» флаги (подсветка, функции и т. д.).
    """
    logic = BridgeLogic(pbn)

    context.user_data["logic"] = logic

    context.user_data["highlight_moves"] = False
    context.user_data["show_funcs"] = False

    return logic


# === КОМАНДЫ ==================================================================
def get_help_text() -> str:
    return """*Команды*
    
    1. /start — запустить бота и открыть главное меню
    2. /pbn — вывести PBN-строку текущего расклада
    3. /help — показать это сообщение
    4. /id — узнать свой Telegram ID


*Анализ расклада*

    1. 📷 По фото — выберите «Анализ по фото» и отправьте снимок расклада  
    • На фото действует лимит запросов и «кулдаун»  
    • Номиналы карт должны быть хорошо видны 
    • Если в трёх руках по 13 карт, а в четвёртой меньше, недостающие карты автоматически добавятся именно в эту руку
    2. 📄 По PBN — выберите «Анализ по PBN» и отправьте одну PBN-строку
    • Формат PBN: сторона света (буква): рука1 рука2 рука3 рука4  
    • Буква перед двоеточием указывает, чья рука идет первой (N/E/S/W)  
    • Остальные руки идут по часовой стрелке  
    • Рука пишется как четыре группы через точки: пики.червы.бубны.трефы  
    • Пример: W:T652.7652.Q6.AKJ 3.3.T97532.Q9853 Q4.AKQ984.AK4.76 AKJ987.JT.J8.T42


*Меню редактирования фото*

    1. ↩️ Повернуть по часовой — повернуть расклад по часовой стрелке
    2. ↪️ Повернуть против часовой — повернуть расклад против часовой
    3. ➕ Добавить карту — выбрать потерянную карту и руку для добавления
    4. 🔀 Переместить карту — переложить карту в другую руку
    5. ✅ Принять расклад — завершить правки и перейти к выбору контракта


*После принятия расклада*

    1. Выберите деноминацию: ♣ ♦ ♥ ♠ NT
    2. Укажите, кто делает первый ход: N E S W


*Режим анализа*

    На экране показан список всех легальных ходов текущего игрока; стрелка указывает, чей ход
    
    1. Основные кнопки (всегда на экране)
    1.1 🗑️ Отменить ход — откатить последнюю сыгранную карту
    1.2 ⭐ Оптимальный ход — сделать лучший ход по DD-анализу
    1.3 🛠️ Опции / 🃏 Карты — переключить клавиатуру функций ↔ карт
        
    2. Клавиатура функций
    2.1 ⏭️ Доиграть до конца — оптимально по DD разыграть все оставшиеся карты и открыть историю
    2.2 📜 История — показать все взятки; ходы *вашего* игрока помечены **^^^** (оптимальные ходы не маркируются); формат карт *N♥A* — туз ♥ с руки N
    2.3 📊 DD-таблица — таблица Double-Dummy для всех деноминаций
    2.4 🔦 Подсветить ходы / 🚫 Скрыть — показывать число взяток для линии текущего игрока (NS или EW) под каждой доступной картой
    2.5 ⤴️ К карте — перемотать к выбранной карте в истории

*Подсказка*
    1. Чтобы начать новый расклад или вернуться в главное меню, нажмите /start"""


@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_help_text(), parse_mode=ParseMode.MARKDOWN)
    await _show_active_window(update, context)

@with_expire
@require_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "logic", "detector", "state", "active_msg_id",
        "show_funcs", "highlight_moves", "contract_set",
        "chosen_denom", "pending_card", "pending_hand_src"
    ):
        context.user_data.pop(key, None)

    sent = await update.message.reply_text(
        "Привет! Я Бриджит — кроссплатформенный ультимативный анализатор бриджевых сдач.\n"
        "Я нахожусь на стадии закрытого бета-тестирования до 23 июля. "
        "О любых неполадках/неточностях/пожеланиях пишите создателю (аккаунт в описании).\n\n"
        "Чем займёмся на этот раз?",
        reply_markup=main_menu_markup(),
    )
    context.user_data["active_msg_id"] = sent.message_id


@with_expire
@require_auth
async def cmd_pbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = update.effective_user.id
    if uid not in UNLIMITED_ID:
        if os.path.exists(CACHED_PBN_DATABASE_NAME):
            with open(CACHED_PBN_DATABASE_NAME, "r") as jf:
                database = json.load(jf)
        else:
            database = {}
        interval = datetime.timedelta(minutes=PBN_LIMIT_INTERVAL_MIN)
        now = datetime.datetime.now()
        recent = [datetime.datetime.fromisoformat(t) for t in database.get(chat_id, []) if now - datetime.datetime.fromisoformat(t) < interval]
        if len(recent) >= PBN_LIMIT_COUNT:
            wait = interval - (now - min(recent))
            await update.message.reply_text(f"🚫 Превышен лимит. Следующий запрос PBN через {await russian_precisedelta(wait)}.")
            return
        recent.append(now)
        database[chat_id] = [t.isoformat() for t in recent]
        with open(CACHED_PBN_DATABASE_NAME, "w") as jf:
            json.dump(database, jf)
    detector: BridgeCardDetector | None = context.user_data.get("detector")
    logic: BridgeLogic | None = context.user_data.get("logic")
    if detector:
        try:
            pbn = detector.to_pbn()
            await update.message.reply_text(f"PBN (N, E, S, W):\n{_pre(pbn)}", parse_mode=ParseMode.MARKDOWN)
            sent = await update.message.reply_text(_pre(detector.preview()), parse_mode=ParseMode.MARKDOWN, reply_markup=analyze_result_markup())
            context.user_data["active_msg_id"] = sent.message_id
            return
        except Exception as e:
            await update.message.reply_text(f"Ошибка получения PBN из распознавания: {e}")
            return
    if logic and context.user_data.get("contract_set"):
        try:
            pbn = logic.to_pbn()
            await update.message.reply_text(f"PBN (N, E, S, W):\n{_pre(pbn)}", parse_mode=ParseMode.MARKDOWN)
            context.user_data["show_funcs"] = False
            board_view = _pre(logic.display())
            kb = make_board_keyboard(logic, False, context.user_data.get("highlight_moves", False))
            sent = await update.message.reply_text(board_view, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            context.user_data["active_msg_id"] = sent.message_id
            return
        except Exception as e:
            await update.message.reply_text(f"Ошибка получения PBN из логики: {e}")
            return
    await update.message.reply_text("❌ Нет активного расклада для вывода PBN.")
    await _show_active_window(update, context)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def russian_precisedelta(delta: datetime.timedelta):
    humanize.i18n.activate("ru_RU")
    humanized = humanize.precisedelta(delta, minimum_unit='seconds', format='%0.0f')
    if " и " in humanized:
        minutes, seconds = humanized.split(" и ")
        if minutes[-1] == "а":
            minutes = minutes[:-1] + "у"
        if seconds[-1] == "а":
            seconds = seconds[:-1] + "у"
        return minutes + " и " + seconds
    if humanized[-1] == "а":
        humanized = humanized[:-1] + "у"
    return humanized

# === CALLBACK‑КНОПКИ ===========================================================

@with_expire
@require_fresh_window
@ignore_telegram_edit_errors
async def goto_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    logic: BridgeLogic | None = context.user_data.get("logic")
    if logic is None:
        await query.answer("Нет активной сдачи.", show_alert=True)
        return

    if data == "act_gotocard":
        tricks, unknown = logic.history_matrix()
        if not tricks:
            await query.answer("Взяток нет")
            return
        context.user_data.update(state="goto_trick_select", unknown_shift=unknown)
        txt = _pre("\n".join(logic.history_plain_lines()) + "\n\nВыберите номер взятки:")
        await query.edit_message_text(
            txt,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=goto_trick_keyboard(len(tricks)),
        )
        return

    state = context.user_data.get("state")

    if state == "goto_trick_select" and data.startswith("goto_trick_"):
        tno = int(data.split("_")[-1])
        tricks, _ = logic.history_matrix()
        cards = tricks[tno - 1]
        context.user_data.update(state="goto_card_select", pending_trick_no=tno)
        single_line = logic.history_plain_lines()[tno - 1]
        txt = _pre(f"{single_line}\n\nВзятка {tno}. Выберите карту:")
        await query.edit_message_text(
            txt,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=goto_card_keyboard(cards),
        )
        return

    if state == "goto_card_select" and data.startswith("goto_card_"):
        cno = int(data.split("_")[-1])
        tno = context.user_data.get("pending_trick_no")
        shift = context.user_data.get("unknown_shift", 0)
        try:
            logic.goto_card(shift + tno, cno)
        except ValueError as e:
            await query.answer(str(e), show_alert=True)
            return
        for k in ("state", "pending_trick_no", "unknown_shift"):
            context.user_data.pop(k, None)
        kb = make_board_keyboard(
            logic,
            False,
            context.user_data.get("highlight_moves", False),
        )
        await query.edit_message_text(
            _pre(logic.display()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        await query.answer("Переместились")
        return

    if data == "goto_cancel":
        if state == "goto_card_select":
            # вернуться к выбору взятки
            context.user_data["state"] = "goto_trick_select"
            context.user_data.pop("pending_trick_no", None)
            tricks, _ = logic.history_matrix()
            txt = _pre("\n".join(logic.history_plain_lines()) + "\n\nВыберите номер взятки:")
            await query.edit_message_text(
                txt,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=goto_trick_keyboard(len(tricks)),
            )
            await query.answer()
            return
        for k in ("state", "unknown_shift"):
            context.user_data.pop(k, None)
        kb = make_board_keyboard(
            logic,
            context.user_data.get("show_funcs", False),
            context.user_data.get("highlight_moves", False),
        )
        await query.edit_message_text(
            _pre(logic.display()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        await query.answer("Отменено")

@with_expire
@require_fresh_window
@ignore_telegram_edit_errors
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    if data == "menu_docs":
        await query.edit_message_text(
            get_help_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]]),
        )
        return

    if data == "input_pbn":
        context.user_data["state"] = STATE_AWAIT_PBN
        await query.edit_message_text(
            "📄 Пришлите PBN-строку расклада:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]]),
        )
        return

    if data == "input_photo":
        context.user_data["state"] = STATE_AWAIT_PHOTO
        await query.edit_message_text(
            "📷 Пришлите фото расклада для распознавания:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]]),
        )
        return

    if data == "back_main":
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "Выберите действие:",
            reply_markup=main_menu_markup(),
        )
        return

@with_expire
@require_fresh_window
@ignore_telegram_edit_errors
async def analyze_result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    detector: BridgeCardDetector | None = context.user_data.get("detector")

    # --- если расклад уже принят -------------------------------------------
    if detector is None:
        await query.answer(
            "Расклад уже принят.\nЗагрузите новый, чтобы снова редактировать.",
            show_alert=True
        )
        return

    if data == "rotate_ccw":
        detector.uclockwise()
        result = detector.preview()
        await query.edit_message_text(
            _pre(result),
            reply_markup=analyze_result_markup(),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "rotate_cw":
        detector.clockwise()
        result = detector.preview()
        await query.edit_message_text(
            _pre(result),
            reply_markup=analyze_result_markup(),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "to_pbn":
        try:
            pbn = detector.to_pbn()
            sent1 = await query.message.reply_text(
                f"PBN:\n{_pre(pbn)}",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data["active_msg_id"] = sent1.message_id
            sent2 = await query.message.reply_text(
                _pre(detector.preview()),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=analyze_result_markup()
            )
            context.user_data["active_msg_id"] = sent2.message_id
        except RuntimeError as e:
            await query.message.reply_text(f"Ошибка: {e}")

    elif data == "accept_result":
        try:
            pbn = detector.to_pbn()
            logic = set_logic_from_pbn(context, pbn)

            context.user_data.pop("detector", None)
            context.user_data["contract_set"] = False
            context.user_data["state"] = STATE_CONTRACT_CHOOSE_DENOM
            context.user_data["chosen_denom"] = None

            await query.edit_message_text(
                "Выберите деноминацию контракта:",
                reply_markup=contract_denom_keyboard(),
                parse_mode=ParseMode.MARKDOWN  # если нужно
            )

        except ValueError as e:
            await query.answer(str(e), show_alert=True)
        except Exception as e:
            await query.message.reply_text(f"Ошибка: {e}")

@with_expire
@require_fresh_window
@ignore_telegram_edit_errors
async def add_move_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    detector: BridgeCardDetector | None = context.user_data.get("detector")

    # --- если расклад уже принят -------------------------------------------
    if detector is None:
        await query.answer(
            "Расклад уже принят.\nРедактирование недоступно.",
            show_alert=True
        )
        return

    # --- отмена операции -----------------------------------------------------
    if data == "cancel_add_move":
        for k in ("state", "pending_card", "pending_hand_src"):
            context.user_data.pop(k, None)

        await query.edit_message_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup(),
        )
        await query.answer("Операция отменена")
        return

    # --- начало «добавить карту» --------------------------------------------
    if data == "add_card_start":
        lost = detector.lost_cards()
        if not lost:
            await query.answer("Нет потерянных карт")
            return
        context.user_data["state"] = STATE_ADD_CARD_SELECT_CARD
        await query.edit_message_text(
            "Выберите карту, которую нужно добавить:",
            reply_markup=card_keyboard(lost),
        )
        return

    # --- начало «переместить карту» -----------------------------------------
    if data == "move_card_start":
        context.user_data["state"] = STATE_MOVE_CARD_SELECT_HAND
        await query.edit_message_text(
            "Из какой руки переместить карту?",
            reply_markup=hand_keyboard(),
        )
        return

    # -----------------------------------------------------------------------
    state = context.user_data.get("state")

    # === ADD-CARD: выбрана карта ============================================
    if state == STATE_ADD_CARD_SELECT_CARD and data.startswith("sel_card_"):
        context.user_data["pending_card"] = data.replace("sel_card_", "")
        context.user_data["state"] = STATE_ADD_CARD_SELECT_HAND
        await query.edit_message_text(
            f"Куда положить {context.user_data['pending_card']}?",
            reply_markup=hand_keyboard(),
        )
        return

    # === ADD-CARD: выбрана рука (конец) =====================================
    if state == STATE_ADD_CARD_SELECT_HAND and data.startswith("hand_"):
        hand = data[-1]                      # N/E/S/W
        card = context.user_data.pop("pending_card")
        context.user_data.pop("state", None)
        try:
            detector.add(f"{card} {hand}")
            await query.edit_message_text(
                _pre(detector.preview()),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=analyze_result_markup(),
            )
        except Exception as e:
            await query.answer(f"Ошибка: {e}", show_alert=True)
        return

    # === MOVE-CARD: выбрана исходная рука ===================================
    if state == STATE_MOVE_CARD_SELECT_HAND and data.startswith("hand_"):
        hand_src = data[-1]
        cards_in_hand = detector.hand_cards(hand_src)   # список карт в руке
        if not cards_in_hand:
            await query.answer("В этой руке нет карт")
            return
        context.user_data["pending_hand_src"] = hand_src
        context.user_data["state"] = STATE_MOVE_CARD_SELECT_CARD
        await query.edit_message_text(
            f"Выберите карту из руки {hand_src}:",
            reply_markup=card_keyboard(cards_in_hand),
        )
        return

    # === MOVE-CARD: выбрана карта ===========================================
    if state == STATE_MOVE_CARD_SELECT_CARD and data.startswith("sel_card_"):
        context.user_data["pending_card"] = data.replace("sel_card_", "")
        context.user_data["state"] = STATE_MOVE_CARD_SELECT_DEST
        await query.edit_message_text(
            "В какую руку переместить карту?",
            reply_markup=hand_keyboard(),
        )
        return

    # === MOVE-CARD: выбрана целевая рука (конец) ============================
    if state == STATE_MOVE_CARD_SELECT_DEST and data.startswith("hand_"):
        hand_dst = data[-1]
        card     = context.user_data.pop("pending_card")
        hand_src = context.user_data.pop("pending_hand_src")
        context.user_data.pop("state", None)
        try:
            detector.move(f"{card} {hand_dst}")
            await query.edit_message_text(
                _pre(detector.preview()),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=analyze_result_markup(),
            )
        except Exception as e:
            await query.answer(f"Ошибка: {e}", show_alert=True)
        return

@with_expire
@require_fresh_window
@ignore_telegram_edit_errors
async def play_card_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    logic: BridgeLogic | None = context.user_data.get("logic")
    if logic is None:
        await query.answer("Нет активной сдачи.", show_alert=True)
        return
    card_code = query.data.replace("play_", "")
    try:
        notice = logic.play_card(card_code)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return
    await query.answer(notice, show_alert=False)
    if not logic.legal_moves():
        await _show_history_with_back(query, logic)
        return
    context.user_data["show_funcs"] = False
    board_view = _pre(logic.display())
    kb = make_board_keyboard(logic, False, context.user_data.get("highlight_moves", False))
    await query.edit_message_text(board_view, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

@with_expire
@require_fresh_window
@ignore_telegram_edit_errors
async def analysis_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    logic: BridgeLogic | None = context.user_data.get("logic")
    if logic is None:
        await query.answer("Нет активной сдачи.", show_alert=True)
        return
    if data == "act_highlight":
        flag = context.user_data.get("highlight_moves", False)
        context.user_data["highlight_moves"] = not flag
        context.user_data["show_funcs"] = False
        kb = make_board_keyboard(logic, False, not flag)
        board_view = _pre(logic.display())
        await query.edit_message_text(board_view, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        await query.answer("Подсветка ходов включена" if not flag else "Подсветка выключена")
        return
    need_redraw = True
    if data == "act_optimal":
        txt = logic.play_optimal_card()
        await query.answer(txt, show_alert=False)
        if not logic.legal_moves():
            await _show_history_with_back(query, logic)
            return
    elif data == "act_undo":
        txt = logic.undo_last_card()
        await query.answer(txt, show_alert=False)
    elif data == "act_playtoend":
        logic.play_optimal_to_end()
        await query.answer("Доиграли до конца", show_alert=False)
        await _show_history_with_back(query, logic)
        return
    elif data == "act_toggle":
        context.user_data["show_funcs"] = not context.user_data.get("show_funcs", False)
        kb = make_board_keyboard(logic, context.user_data["show_funcs"], context.user_data.get("highlight_moves", False))
        await query.edit_message_reply_markup(reply_markup=kb)
        return
    elif data == "act_history":
        txt = _pre(logic.show_history())
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="act_back")]])
        await query.edit_message_text(text=txt, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)
        return
    elif data == "act_back":
        board_view = _pre(logic.display())
        kb = make_board_keyboard(logic, context.user_data.get("show_funcs", False), context.user_data.get("highlight_moves", False))
        await query.edit_message_text(text=board_view, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return
    elif data == "act_ddtable":
        txt = _pre(logic.dd_table())
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="act_back")]])
        await query.edit_message_text(text=txt, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)
        return
    if need_redraw:
        main_msg_id = context.user_data.get("active_msg_id")
        if main_msg_id:
            board_view = _pre(logic.display())
            kb = make_board_keyboard(logic, context.user_data.get("show_funcs", False), context.user_data.get("highlight_moves", False))
            await context.bot.edit_message_text(chat_id=query.message.chat_id, message_id=main_msg_id, text=board_view, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# === Flow выбора контракта ================================================

@with_expire
@require_fresh_window
@ignore_telegram_edit_errors
async def contract_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    logic: BridgeLogic | None = context.user_data.get("logic")
    if logic is None:
        await query.answer("Нет расклада", show_alert=True)
        return
    if data.startswith("denom_"):
        token = data.split("_", 1)[1]
        context.user_data["chosen_denom"] = token
        context.user_data["state"] = STATE_CONTRACT_CHOOSE_FIRST
        await query.edit_message_text("Выберите кто делает первый ход:", reply_markup=contract_first_keyboard())
        return
    if data.startswith("first_"):
        first = data.split("_", 1)[1]
        denom_token = context.user_data.get("chosen_denom")
        context.user_data.pop("state", None)
        contract_str = "NT" if denom_token == "NT" else denom_token
        try:
            logic.set_contract(contract_str, first)
            context.user_data["contract_set"] = True
        except Exception as e:
            await query.edit_message_text(f"Ошибка: {e}")
            return
        await query.edit_message_text("⏳ Приступаю к анализу...")
        context.user_data["show_funcs"] = False
        board_view = _pre(logic.display())
        kb = make_board_keyboard(
            logic,
            False,
            context.user_data.get("highlight_moves", False),
        )
        sent = await query.message.reply_text(board_view, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        context.user_data["active_msg_id"] = sent.message_id


# === ТЕКСТОВЫЙ ВВОД ============================================================

async def handle_pbn_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != STATE_AWAIT_PBN:
        await unknown_message(update, context)
        return

    try:
        detector = BridgeCardDetector.from_pbn(update.message.text.strip())
        context.user_data["detector"] = detector
        context.user_data.pop("state", None)

        sent = await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup(),
        )
        context.user_data["active_msg_id"] = sent.message_id

    except ValueError as ve:
        await update.message.reply_text(f"Некорректный PBN: {ve}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка анализа: {e}")

# === ОБРАБОТКА ФОТО ============================================================

async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != STATE_AWAIT_PHOTO:
        await update.message.reply_text("⚠️ Для отправки фотографий выберите соответствующую функцию в меню.")
        await _show_active_window(update, context)
        return
    msg = update.message
    if msg.photo:
        file = await msg.photo[-1].get_file()
        ext = ".jpg"
    elif msg.document and msg.document.mime_type.startswith("image/"):
        doc = msg.document
        ext = mimetypes.guess_extension(doc.mime_type) or os.path.splitext(doc.file_name)[1]
        ext = ext.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff"}:
            await msg.reply_text(f"Извините, не умею распознавать файлы «{ext}».\nПришлите изображение в формате: JPG, JPEG, PNG, BMP, GIF или TIFF.")
            return
        file = await doc.get_file()
    else:
        await msg.reply_text("Отправьте изображение (JPG, PNG, BMP, GIF или TIFF).")
        return
    chat_id = str(msg.chat_id)
    uid = update.effective_user.id
    if uid not in UNLIMITED_ID:
        if os.path.exists(CACHED_PHOTO_DATABASE_NAME):
            with open(CACHED_PHOTO_DATABASE_NAME, "r") as jf:
                database = json.load(jf)
        else:
            database = {}
        interval = datetime.timedelta(minutes=PHOTO_LIMIT_INTERVAL_MIN)
        now = datetime.datetime.now()
        recent = [datetime.datetime.fromisoformat(t) for t in database.get(chat_id, []) if now - datetime.datetime.fromisoformat(t) < interval]
        if len(recent) >= PHOTO_LIMIT_COUNT:
            wait = interval - (now - min(recent))
            await msg.reply_text(f"🚫 Превышен лимит. Следующее распознавание через {await russian_precisedelta(wait)}.")
            return
        recent.append(now)
        database[chat_id] = [t.isoformat() for t in recent]
        with open(CACHED_PHOTO_DATABASE_NAME, "w") as jf:
            json.dump(database, jf)
    inp = generate_filename()
    out = generate_filename()
    path = await file.download_to_drive(inp)
    await msg.reply_text("⏳ Фото принято. Распознаю карты...")
    try:
        detector = BridgeCardDetector(path)
        detector.visualize(out)
        preview = detector.preview()
        with open(out, "rb") as img:
            await msg.reply_photo(photo=img)
        sent = await msg.reply_text(_pre(preview), parse_mode=ParseMode.MARKDOWN, reply_markup=analyze_result_markup())
        context.user_data["active_msg_id"] = sent.message_id
        context.user_data["detector"] = detector
    except Exception as e:
        await msg.reply_text(f"Ошибка распознавания: {e}")
    finally:
        for fn in (inp, out):
            try:
                os.remove(fn)
            except OSError:
                pass
        context.user_data.pop("state", None)

# === ГЛАВНАЯ ФУНКЦИЯ ===========================================================

def post_init(application: Application):
    return application.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("pbn", "PBN-строка текущего расклада"),
        BotCommand("help", "Показать документацию"),
        BotCommand("id", "Узнать свой Telegram-ID"),
    ])


def main():
    app = Application.builder().token(TOKEN).request(req).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, ignore_edit))

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pbn", cmd_pbn))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", show_id))

    # Кнопки меню и навигации
    app.add_handler(CallbackQueryHandler(menu_handler, pattern="^(menu_docs|input_pbn|input_photo|back_main)$"))
    app.add_handler(CallbackQueryHandler(add_move_flow_handler, pattern="^(add_card_start|move_card_start|sel_card_.*|hand_[NESW]|cancel_add_move)$"))

    # Кнопки анализа расклада (после распознавания)
    app.add_handler(CallbackQueryHandler(analyze_result_handler, pattern="^(rotate_cw|rotate_ccw|accept_result)$"))

    app.add_handler(CallbackQueryHandler(contract_flow_handler, pattern="^(denom_[CDHS]|denom_NT|first_[NESW])$"))

    # === Вот этот хендлер только если В РЕЖИМЕ PBN ===
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
        handle_pbn_input
    ))

    # Фото и документы-картинки
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo_input))

    app.add_handler(CallbackQueryHandler(play_card_handler, pattern="^play_"))
    app.add_handler(CallbackQueryHandler(
        analysis_action_handler,
        pattern="^act_(optimal|undo|toggle|history|ddtable|playtoend|back|highlight)$"
    ))
    app.add_handler(CallbackQueryHandler(
        goto_flow_handler,
        pattern="^(act_gotocard|goto_trick_\\d+|goto_card_\\d+|goto_cancel)$"))

    # === Последний — ловит вообще всё ===
    app.add_handler(MessageHandler(filters.ALL, unknown_message))

    logging.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
