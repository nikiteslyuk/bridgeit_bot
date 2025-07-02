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



from logic import BridgeLogic
from detection import BridgeCardDetector
os.makedirs("img", exist_ok=True)

# TOKEN = os.getenv("TG_TOKEN")
TOKEN = "7976805123:AAHpYOm43hazvkXUlDY-q4X9US18upq9uak"
AUTHORIZED_ID = [375025446, 855302541, 5458141225]
UNLIMITED_PHOTO_ID = [375025446, 855302541]
logging.basicConfig(level=logging.INFO)
ANALYSIS_COMMANDS = [
    ("Показать расклад", "display"),
    ("Текущий игрок", "current_player"),
    ("Отмена последней взятки", "undo_last_trick"),
    ("Показать историю", "show_history"),
    ("Показать DD-таблицу", "dd_table"),
    ("Сыграть оптимально", "play_optimal_card"),
    ("Сыграть оптимальную взятку", "play_optimal_trick"),
    ("Сыграть оптимально до конца", "play_optimal_to_end"),
    ("Показать текущую руку", "show_current_hand"),
]
req = HTTPXRequest(connection_pool_size=10, connect_timeout=10.0, read_timeout=60.0, write_timeout=60.0, pool_timeout=10.0)
ANALYSIS_CMDS_PER_PAGE = 4
CACHED_REQUESTS_DATABASE_NAME = "users_requests.json"


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


def require_fresh_window(handler):
    """Старое окно → молча переписываем текст и убираем клавиатуру."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        last_id: int | None = context.user_data.get("active_msg_id")

        if last_id is None or query.message.message_id != last_id:
            try:
                await query.edit_message_text(
                    "⚠️ Это неактуальное окно.\n"
                    "Используйте кнопки из самого последнего сообщения.",
                    reply_markup=None,
                )
            except BadRequest:
                pass
            await query.answer()
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


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я понимаю только команды.\n"
        "Пожалуйста, общайся со мной на языке команд."
    )

# === ХЕЛПЕРЫ ДЛЯ КЛАВИАТУР ====================================================

def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🃏 Анализ расклада", callback_data="menu_analyze")],
        [InlineKeyboardButton("📜 Политика конфиденциальности", callback_data="menu_privacy")],
        [InlineKeyboardButton("🙏 Благодарность создателю", callback_data="menu_thanks")],
    ])


def analyze_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Распознать по фото", callback_data="input_photo")],
        [InlineKeyboardButton("📄 Распознать по PBN", callback_data="input_pbn")],
        [InlineKeyboardButton("🎲 Сгенерировать сдачу", callback_data="generate_deal")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
    ])


def back_to_analyze_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_analyze")],
    ])


def analyze_result_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏪ Повернуть против часовой", callback_data="rotate_ccw"),
            InlineKeyboardButton("Повернуть по часовой ⏩", callback_data="rotate_cw"),
        ],
        [
            InlineKeyboardButton("📄 Вывести PBN-строку", callback_data="to_pbn"),
            InlineKeyboardButton("Принять сдачу ✅", callback_data="accept_result"),
        ]
    ])


def analysis_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total = len(ANALYSIS_COMMANDS)
    per_page = ANALYSIS_CMDS_PER_PAGE
    start = page * per_page
    end = start + per_page
    btns = [
        [InlineKeyboardButton(text, callback_data=f"analysis_{cmd}")]
        for text, cmd in ANALYSIS_COMMANDS[start:end]
    ]

    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"analysis_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"analysis_page_{page+1}"))
    if nav:
        btns.append(nav)
    return InlineKeyboardMarkup(btns)

# === СОСТОЯНИЯ ================================================================
STATE_AWAIT_PBN = "await_pbn"
STATE_AWAIT_PHOTO = "await_photo"

# === СОЗДАТЬ / ОБНОВИТЬ BridgeLogic ДЛЯ ПОЛЬЗОВАТЕЛЯ ========================== / ОБНОВИТЬ BridgeLogic ДЛЯ ПОЛЬЗОВАТЕЛЯ ==========================

def set_logic_from_pbn(context: ContextTypes.DEFAULT_TYPE, pbn: str) -> BridgeLogic:
    """Создаём новый BridgeLogic(PBN) и кладём в user_data.

    Если PBN некорректен, пробрасываем ValueError наружу, чтобы вызвать
    красивое сообщение об ошибке.
    """
    logic = BridgeLogic(pbn)  # может выбросить ValueError
    context.user_data["logic"] = logic
    return logic


# === КОМАНДЫ ================================================================== 

@require_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    sent = await update.message.reply_text(
        "Я нахожусь на стадии закрытого бета‑тестирования.\n"
        "Тыкай на кнопки, ищи баги и пиши создателю: @bridgeit_support!",
        reply_markup=main_menu_markup(),
    )
    context.user_data["active_msg_id"] = sent.message_id


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


@require_auth
async def add_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text(
            "⛔ Эта команда доступна только при редактировании распознанной сдачи.\n"
            "Сначала распознай сдачу по фото или выбери PBN."
        )
        return

    if not context.args:
        await update.message.reply_text("Формат: /addcard <карта> <рука> (например: /addcard 4h W)")
        return

    try:
        detector.add(" ".join(context.args))
        sent = await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup(),
        )
        context.user_data["active_msg_id"] = sent.message_id
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def move_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text("⛔ Эта команда доступна только при редактировании распознанной сдачи.")
        return

    if not context.args:
        await update.message.reply_text("Формат: /movecard <карта> <куда> (например: /movecard 4h N)")
        return

    try:
        detector.move(" ".join(context.args))
        sent = await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup(),
        )
        context.user_data["active_msg_id"] = sent.message_id
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def remove_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text("⛔ Эта команда доступна только при редактировании распознанной сдачи.")
        return

    if not context.args:
        await update.message.reply_text("Формат: /removecard <карта> <рука> (например: /removecard 4h W)")
        return

    try:
        detector.remove(" ".join(context.args))
        sent = await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup(),
        )
        context.user_data["active_msg_id"] = sent.message_id
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def clockwise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text("⛔ Эта команда доступна только при редактировании сдачи.")
        return

    detector.clockwise()
    sent = await update.message.reply_text(
        _pre(detector.preview()),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analyze_result_markup(),
    )
    context.user_data["active_msg_id"] = sent.message_id


@require_auth
async def uclockwise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text("⛔ Эта команда доступна только при редактировании сдачи.")
        return

    detector.uclockwise()
    sent = await update.message.reply_text(
        _pre(detector.preview()),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analyze_result_markup(),
    )
    context.user_data["active_msg_id"] = sent.message_id


@require_auth
async def pbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text("⛔ Эта команда доступна только при редактировании сдачи.")
        return
    try:
        pbn_str = detector.to_pbn()
        await update.message.reply_text(f"PBN:\n{_pre(pbn_str)}", parse_mode=ParseMode.MARKDOWN)

        sent = await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup(),
        )
        context.user_data["active_msg_id"] = sent.message_id
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text(
            "⛔ Эта команда доступна только при редактировании сдачи."
        )
        return
    try:
        pbn = detector.to_pbn()
        logic = set_logic_from_pbn(context, pbn)
        context.user_data.pop("detector", None)
        context.user_data["contract_set"] = False
        await update.message.reply_text(
            "Расклад принят. Сделайте команду /setcontract <контракт> <первая_рука> (например: /setcontract 3NT N)",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def cmd_setcontract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Формат: /setcontract <контракт/масть> <первая_рука> (пример: /setcontract 3NT N)"
        )
        return

    try:
        contract, first = context.args[0], context.args[1]
        logic.set_contract(contract, first)
        context.user_data["contract_set"] = True

        sent = await update.message.reply_text(
            _pre(logic.display()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analysis_keyboard(0),
        )
        context.user_data["active_msg_id"] = sent.message_id

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


# ---------------------------------------------------------------------------
# Показать расклад
# ---------------------------------------------------------------------------
@require_auth
async def cmd_display(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = _pre(logic.display())
    idx = [c[1] for c in ANALYSIS_COMMANDS].index("display")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Double-dummy таблица
# ---------------------------------------------------------------------------
@require_auth
async def cmd_ddtable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if not context.user_data.get("contract_set"):
    #     await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
    #     return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = _pre(logic.dd_table())
    idx = [c[1] for c in ANALYSIS_COMMANDS].index("dd_table")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Текущий игрок
# ---------------------------------------------------------------------------
@require_auth
async def cmd_currentplayer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = f"Текущий игрок: {logic.current_player()}"
    idx = [c[1] for c in ANALYSIS_COMMANDS].index("current_player")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Оптимальный ход
# ---------------------------------------------------------------------------
@require_auth
async def cmd_optimalmove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    try:

        text = f"Оптимальный ход: {logic.optimal_move()}"
    except Exception as e:
        text = f"Ошибка: {e}"

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("play_optimal_card")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Показать варианты ходов
# ---------------------------------------------------------------------------
@require_auth
async def cmd_showmoveoptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    try:
        text = _pre(logic.show_move_options())
    except Exception as e:
        text = f"Ошибка: {e}"

    sent = await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analysis_keyboard(0),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Сыграть карту
# ---------------------------------------------------------------------------
@require_auth
async def cmd_playcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    if not context.args:
        await update.message.reply_text("Формат: /playcard <карта> (пример: /playcard 7h)")
        return

    try:
        text = logic.play_card(context.args[0])
    except Exception as e:
        text = f"Ошибка: {e}"

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("play_optimal_card")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Сыграть взятку
# ---------------------------------------------------------------------------
@require_auth
async def cmd_playtrick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    if len(context.args) < 4:
        await update.message.reply_text("Формат: /playtrick <карта1> <карта2> <карта3> <карта4>")
        return

    try:
        text = logic.play_trick(" ".join(context.args[:4]))
    except Exception as e:
        text = f"Ошибка: {e}"

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("play_optimal_trick")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Отменить последнюю взятку
# ---------------------------------------------------------------------------
@require_auth
async def cmd_undolasttrick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = logic.undo_last_trick()

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("undo_last_trick")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Перейти к взятке
# ---------------------------------------------------------------------------
@require_auth
async def cmd_gototrick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Формат: /gototrick <номер>")
        return

    try:
        text = logic.goto_trick(int(context.args[0]))
    except Exception as e:
        text = f"Ошибка: {e}"

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(0),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Показать историю
# ---------------------------------------------------------------------------
@require_auth
async def cmd_showhistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = _pre(logic.show_history())

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("show_history")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Доиграть до конца оптимально
# ---------------------------------------------------------------------------
@require_auth
async def cmd_playoptimaltoend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    logic.play_optimal_to_end()
    text = "Доигрыш до конца выполнен."

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("play_optimal_to_end")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Показать текущую руку
# ---------------------------------------------------------------------------
@require_auth
async def cmd_showcurrenthand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = _pre(logic.show_current_hand())

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("show_current_hand")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Перейти к карте во взятке
# ---------------------------------------------------------------------------
@require_auth
async def cmd_gotocard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Формат: /gotocard <номер_взятки> <номер_карты>")
        return

    try:
        text = logic.goto_card(int(context.args[0]), int(context.args[1]))
    except Exception as e:
        text = f"Ошибка: {e}"

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(0),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Сыграть оптимальную карту
# ---------------------------------------------------------------------------
@require_auth
async def cmd_playoptimalcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = logic.play_optimal_card()

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("play_optimal_card")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Сыграть оптимальную взятку
# ---------------------------------------------------------------------------
@require_auth
async def cmd_playoptimaltrick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    text = logic.play_optimal_trick()

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("play_optimal_trick")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id


# ---------------------------------------------------------------------------
# Сыграть N оптимальных взяток
# ---------------------------------------------------------------------------
@require_auth
async def cmd_playoptimaltricks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Формат: /playoptimaltricks <сколько_взяток>")
        return

    try:
        n = int(context.args[0])
        text = logic.play_optimal_tricks(n) or "Взятки разыграны."
    except Exception as e:
        text = f"Ошибка: {e}"

    idx = [c[1] for c in ANALYSIS_COMMANDS].index("play_optimal_trick")
    page = idx // ANALYSIS_CMDS_PER_PAGE

    sent = await update.message.reply_text(
        text,
        reply_markup=analysis_keyboard(page),
    )
    context.user_data["active_msg_id"] = sent.message_id

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===================================================


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

@require_auth
@require_fresh_window
@ignore_telegram_edit_errors
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Главное и сервисные меню.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    # ---------- пункт «🃏 Анализ расклада» ----------
    if data == "menu_analyze":
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "Выберите способ ввода расклада:",
            reply_markup=analyze_menu_markup(),
        )
        return

    # ---------- «Политика» и «Благодарность» -------
    if data in {"menu_privacy", "menu_thanks"}:
        await query.edit_message_text(
            "Функция находится в разработке 🚧",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]]
            ),
        )
        return

    # ---------- «Сгенерировать сдачу» --------------
    if data == "generate_deal":
        await query.edit_message_text(
            "Функция находится в разработке 🚧",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="back_analyze")]]
            ),
        )
        return

    # ---------- ввод PBN-строки ---------------------
    if data == "input_pbn":
        context.user_data["state"] = STATE_AWAIT_PBN
        await query.edit_message_text(
            "Введите PBN-строку расклада:",
            reply_markup=back_to_analyze_markup(),
        )
        return

    # ---------- ввод фото ---------------------------
    if data == "input_photo":
        context.user_data["state"] = STATE_AWAIT_PHOTO
        await query.edit_message_text(
            "Пришлите фото расклада для распознавания:",
            reply_markup=back_to_analyze_markup(),
        )
        return

    # ---------- «Назад» из analyze → главное меню ---
    if data == "back_main":
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "Я нахожусь на стадии закрытого бета-тестирования.\n"
            "Тыкай на кнопки, ищи баги и пиши создателю!",
            reply_markup=main_menu_markup(),
        )
        return

    # ---------- «Назад» из подменю → меню анализа ---
    if data == "back_analyze":
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "Выберите способ ввода расклада:",
            reply_markup=analyze_menu_markup(),
        )
        return

    # ---------- fallback ----------------------------
    await query.answer("Неизвестная кнопка", show_alert=True)


@require_auth
@require_fresh_window
@ignore_telegram_edit_errors
async def analyze_result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await query.edit_message_text("Ошибка: расклад не найден.")
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
            await query.message.reply_text(
                "Расклад принят. Сделайте команду /setcontract <контракт> <первая_рука> (например: /setcontract 3NT N)",
                parse_mode=None,
            )
        except Exception as e:
            await query.message.reply_text(f"Ошибка: {e}")


@require_auth
@require_fresh_window
@ignore_telegram_edit_errors
async def analysis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if not context.user_data.get("contract_set"):
        await query.edit_message_text(
            "⚠️ Сначала задайте контракт командой /setcontract.",
            reply_markup=None,
        )
        return

    logic: BridgeLogic = context.user_data.get("logic")
    if logic is None:
        await query.edit_message_text(
            "⚠️ Сначала загрузите сдачу заново.",
            reply_markup=None,
        )
        return

    if data.startswith("analysis_page_"):
        page = int(data.split("_")[-1])
        await query.edit_message_reply_markup(reply_markup=analysis_keyboard(page))
        return

    if data.startswith("analysis_"):
        cmd = data[len("analysis_"):]
        try:
            result = getattr(logic, cmd)()
        except Exception as e:
            result = f"Ошибка: {e}"

        try:
            idx  = [c[1] for c in ANALYSIS_COMMANDS].index(cmd)
            page = idx // ANALYSIS_CMDS_PER_PAGE
        except ValueError:
            page = 0

        await query.edit_message_text(
            _pre(result) if result else "Готово.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analysis_keyboard(page),
        )


# === ТЕКСТОВЫЙ ВВОД ============================================================

@require_auth
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

@require_auth
async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото/файла-изображения при STATE_AWAIT_PHOTO"""
    if context.user_data.get("state") != STATE_AWAIT_PHOTO:
        return  # не в режиме ожидания фото

    msg = update.message

    # Определяем, что пришло — фото или документ-изображение
    if msg.photo:
        file = await msg.photo[-1].get_file()
        ext = '.jpg'
    elif msg.document and msg.document.mime_type.startswith("image/"):
        doc = msg.document
        # Определяем расширение по MIME и по имени, приоритетно MIME
        ext = mimetypes.guess_extension(doc.mime_type) or os.path.splitext(doc.file_name)[1]
        ext = ext.lower()
        allowed_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff'}
        if ext not in allowed_exts:
            await msg.reply_text(
                f"Извините, я не умею распознавать файлы с расширением «{ext}».\n"
                "Пожалуйста, пришлите изображение в одном из форматов: "
                "JPG, JPEG, PNG, BMP, GIF или TIFF."
            )
            return
        file = await doc.get_file()
    else:
        # ни фото, ни документ-изображение
        await msg.reply_text(
            "Пожалуйста, отправьте изображение в одном из форматов: JPG, PNG, BMP, GIF или TIFF."
        )
        return

    chat_id = str(msg.chat_id)
    uid = update.effective_user.id
    if uid not in UNLIMITED_PHOTO_ID:
        if os.path.exists(CACHED_REQUESTS_DATABASE_NAME):
            with open(CACHED_REQUESTS_DATABASE_NAME, "r") as json_file:
                database = json.load(json_file)
        else:
            database = {}
        datetime_now = datetime.datetime.now()
        if chat_id in database:
            current_diff = datetime_now - datetime.datetime.fromisoformat(database[chat_id][0])
            remaining_time = datetime.timedelta(minutes=10) - current_diff
            if len(database[chat_id]) > 2 or (len(database[chat_id]) == 2 and remaining_time.total_seconds() > 0):
                await msg.reply_text(
                    f"Превышено ограничение на распознавание фото. Следующее распознавание будет доступно через {await russian_precisedelta(remaining_time)}"
                )
                return
            else:
                if len(database[chat_id]) == 2:
                    database[chat_id].pop(0)
                database[chat_id].append(datetime_now.isoformat())
        else:
            database[chat_id] = [datetime_now.isoformat()]
        with open(CACHED_REQUESTS_DATABASE_NAME, "w") as json_file:
            json.dump(database, json_file)

    # Генерируем уникальные имена с расширением
    input_filename = generate_filename()
    output_filename = generate_filename()

    # Скачиваем входной файл
    path = await file.download_to_drive(input_filename)
    await msg.reply_text("Фото принято. Приступаю к распознаванию карт...")

    try:
        detector = BridgeCardDetector(path)
        detector.visualize(output_filename)
        result = detector.preview()

        # Отправляем результат пользователю
        with open(output_filename, "rb") as out_img:
            await msg.reply_photo(photo=out_img)
        sent = await msg.reply_text(
            _pre(result),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup()
        )
        context.user_data["active_msg_id"] = sent.message_id
        context.user_data["detector"] = detector


    except Exception as e:
        await msg.reply_text(f"Ошибка распознавания фото: {e}")
    finally:
        # Удаляем временные файлы
        for fn in (input_filename, output_filename):
            try:
                os.remove(fn)
            except OSError:
                pass

        # Сбрасываем состояние
        context.user_data.pop("state", None)

# === ГЛАВНАЯ ФУНКЦИЯ ===========================================================

def post_init(application: Application):
    return application.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("id", "Узнать свой Telegram-ID"),
        BotCommand("addcard", "Добавить карту в руку"),
        BotCommand("movecard", "Переместить карту в руку"),
        BotCommand("removecard", "Удалить карту из руки"),
        BotCommand("clockwise", "Повернуть сдачу по часовой"),
        BotCommand("uclockwise", "Повернуть сдачу против часовой"),
        BotCommand("pbn", "Вывести PBN-строку текущей сдачи"),
        BotCommand("accept", "Принять текущую сдачу"),
        BotCommand("display", "Показать текущий расклад карт"),
        BotCommand("ddtable", "Показать double-dummy таблицу"),
        BotCommand("setcontract", "Задать контракт и первую руку (напр: /setcontract 3NT N)"),
        BotCommand("currentplayer", "Кто сейчас ходит"),
        BotCommand("optimalmove", "Оптимальный ход для текущего игрока"),
        BotCommand("showmoveoptions", "Все варианты ходов для текущей руки"),
        BotCommand("playcard", "Сделать ход картой (напр: /playcard 7h)"),
        BotCommand("playtrick", "Разыграть взятку 4 картами (напр: /playtrick 7h 4c 3d 8s)"),
        BotCommand("undolasttrick", "Откатить последнюю взятку"),
        BotCommand("gototrick", "Откатиться к началу взятки (напр: /gototrick 5)"),
        BotCommand("showhistory", "Показать историю розыгрыша"),
        BotCommand("playoptimaltoend", "Доиграть сдачу до конца оптимально"),
        BotCommand("showcurrenthand", "Показать текущую руку"),
        BotCommand("gotocard", "Откатиться к карте во взятке (напр: /gotocard 7 2)"),
        BotCommand("playoptimalcard", "Сделать оптимальный ход"),
        BotCommand("playoptimaltrick", "Разыграть одну оптимальную взятку"),
        BotCommand("playoptimaltricks", "Разыграть несколько оптимальных взяток (напр: /playoptimaltricks 3)"),
    ])


def main():
    app = Application.builder().token(TOKEN).request(req).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, ignore_edit))

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("addcard", add_card))
    app.add_handler(CommandHandler("movecard", move_card))
    app.add_handler(CommandHandler("removecard", remove_card))
    app.add_handler(CommandHandler("clockwise", clockwise))
    app.add_handler(CommandHandler("uclockwise", uclockwise))
    app.add_handler(CommandHandler("pbn", pbn))
    app.add_handler(CommandHandler("accept", accept))
    app.add_handler(CommandHandler("display", cmd_display))
    app.add_handler(CommandHandler("ddtable", cmd_ddtable))
    app.add_handler(CommandHandler("setcontract", cmd_setcontract))
    app.add_handler(CommandHandler("currentplayer", cmd_currentplayer))
    app.add_handler(CommandHandler("optimalmove", cmd_optimalmove))
    app.add_handler(CommandHandler("showmoveoptions", cmd_showmoveoptions))
    app.add_handler(CommandHandler("playcard", cmd_playcard))
    app.add_handler(CommandHandler("playtrick", cmd_playtrick))
    app.add_handler(CommandHandler("undolasttrick", cmd_undolasttrick))
    app.add_handler(CommandHandler("gototrick", cmd_gototrick))
    app.add_handler(CommandHandler("showhistory", cmd_showhistory))
    app.add_handler(CommandHandler("playoptimaltoend", cmd_playoptimaltoend))
    app.add_handler(CommandHandler("showcurrenthand", cmd_showcurrenthand))
    app.add_handler(CommandHandler("gotocard", cmd_gotocard))
    app.add_handler(CommandHandler("playoptimalcard", cmd_playoptimalcard))
    app.add_handler(CommandHandler("playoptimaltrick", cmd_playoptimaltrick))
    app.add_handler(CommandHandler("playoptimaltricks", cmd_playoptimaltricks))

    # Кнопки меню и навигации
    app.add_handler(CallbackQueryHandler(menu_handler, pattern="^(menu_analyze|menu_privacy|menu_thanks|input_pbn|input_photo|back_main|back_analyze|generate_deal)$"))

    # Кнопки анализа расклада (после распознавания)
    app.add_handler(CallbackQueryHandler(analyze_result_handler, pattern="^(rotate_cw|rotate_ccw|accept_result|to_pbn)$"))

    app.add_handler(CallbackQueryHandler(analysis_handler, pattern="^analysis_"))

    # === Вот этот хендлер только если В РЕЖИМЕ PBN ===
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
        handle_pbn_input
    ))

    # Фото и документы-картинки
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo_input))

    # === Последний — ловит вообще всё ===
    app.add_handler(MessageHandler(filters.ALL, unknown_message))

    logging.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
