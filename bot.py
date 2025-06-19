import logging
import os
import uuid
import mimetypes
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

# --- ваши модули ---
from logic import BridgeLogic  # ← твой класс анализа PBN
from detection import BridgeCardDetector  # пока не используется

os.makedirs("img", exist_ok=True)

TOKEN = os.getenv("TG_TOKEN")
AUTHORIZED_ID = [375025446, 924088517, 474652623]
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

# Максимум 4 кнопки на экран + Стрелки
ANALYSIS_CMDS_PER_PAGE = 4


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
STATE_AWAIT_PBN = "await_pbn"  # ждём ввод PBN-строки
STATE_AWAIT_PHOTO = "await_photo"  # ждём фото для распознавания

# === СОЗДАТЬ / ОБНОВИТЬ BridgeLogic ДЛЯ ПОЛЬЗОВАТЕЛЯ ========================== / ОБНОВИТЬ BridgeLogic ДЛЯ ПОЛЬЗОВАТЕЛЯ ==========================

def set_logic_from_pbn(context: ContextTypes.DEFAULT_TYPE, pbn: str) -> BridgeLogic:
    """Создаём новый BridgeLogic(PBN) и кладём в user_data.

    Если PBN некорректен, пробрасываем ValueError наружу, чтобы вызвать
    красивое сообщение об ошибке.
    """
    logic = BridgeLogic(pbn)  # может выбросить ValueError
    context.user_data["logic"] = logic
    return logic


# === КОМАНДЫ ================================================================== ==================================================================

@require_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)  # сброс FSM при старте
    await update.message.reply_text(
        "Я нахожусь на стадии закрытого бета‑тестирования.\n"
        "Тыкай на кнопки, ищи баги и пиши создателю: @bridgeit_support!",
        reply_markup=main_menu_markup(),
    )


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

    arg = " ".join(context.args)
    try:
        detector.add(arg)
        # Показываем обновлённую сдачу и кнопки редактирования
        await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup()
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def move_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text(
            "⛔ Эта команда доступна только при редактировании распознанной сдачи."
        )
        return

    if not context.args:
        await update.message.reply_text("Формат: /movecard <карта> <куда> (например: /movecard 4h N)")
        return

    arg = " ".join(context.args)
    try:
        detector.move(arg)
        await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup()
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def remove_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text(
            "⛔ Эта команда доступна только при редактировании распознанной сдачи."
        )
        return

    if not context.args:
        await update.message.reply_text("Формат: /removecard <карта> <рука> (например: /removecard 4h W)")
        return

    arg = " ".join(context.args)
    try:
        detector.remove(arg)
        await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup()
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

@require_auth
async def clockwise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text(
            "⛔ Эта команда доступна только при редактировании сдачи."
        )
        return
    detector.clockwise()
    await update.message.reply_text(
        _pre(detector.preview()),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analyze_result_markup()
    )


@require_auth
async def uclockwise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text(
            "⛔ Эта команда доступна только при редактировании сдачи."
        )
        return
    detector.uclockwise()
    await update.message.reply_text(
        _pre(detector.preview()),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=analyze_result_markup()
    )


@require_auth
async def pbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detector: BridgeCardDetector = context.user_data.get("detector")
    if not detector:
        await update.message.reply_text(
            "⛔ Эта команда доступна только при редактировании сдачи."
        )
        return
    try:
        pbn = detector.to_pbn()
        await update.message.reply_text(
            f"PBN:\n{_pre(pbn)}",
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(
            _pre(detector.preview()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup()
        )
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
        # Фиксируем флаг, что контракт еще не задан
        context.user_data["contract_set"] = False
        await update.message.reply_text(
            "Расклад принят. Сделайте команду /setcontract <контракт> <первая_рука> (например: /setcontract 3NT N)",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def cmd_display(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    await update.message.reply_text(
        _pre(logic.display()),
        parse_mode=ParseMode.MARKDOWN
    )


@require_auth
async def cmd_ddtable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    await update.message.reply_text(
        _pre(logic.dd_table()), 
        parse_mode=ParseMode.MARKDOWN
    )


@require_auth
async def cmd_setcontract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setcontract <контракт/масть> <первая_рука> (пример: /setcontract 3NT N)")
        return
    try:
        contract = context.args[0]
        first = context.args[1]
        result = logic.set_contract(contract, first)
        context.user_data["contract_set"] = True
        await update.message.reply_text(
            "Можете приступать к анализу.",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Показываем расклад и меню анализа
        await update.message.reply_text(
            _pre(logic.display()),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analysis_keyboard(0)
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def cmd_currentplayer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    pl = logic.current_player()
    await update.message.reply_text(f"Текущий игрок: {pl.abbr}")


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
        card = logic.optimal_move()
        await update.message.reply_text(f"Оптимальный ход: {card}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


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
        await update.message.reply_text(_pre(logic.show_move_options()), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


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
        card_str = context.args[0]
        res = logic.play_card(card_str)
        await update.message.reply_text(res)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def cmd_playtrick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    # Пример: /playtrick 7h 4c 3d 8s
    if len(context.args) < 4:
        await update.message.reply_text("Формат: /playtrick <карта1> <карта2> <карта3> <карта4>")
        return
    try:
        trick = " ".join(context.args[:4])
        res = logic.play_trick(trick)
        await update.message.reply_text(res)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def cmd_undolasttrick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    res = logic.undo_last_trick()
    await update.message.reply_text(res)


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
        no_ = int(context.args[0])
        res = logic.goto_trick(no_)
        await update.message.reply_text(res)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def cmd_showhistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    await update.message.reply_text(_pre(logic.show_history()), parse_mode=ParseMode.MARKDOWN)


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
    await update.message.reply_text("Доигрыш до конца выполнен.")


@require_auth
async def cmd_showcurrenthand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    await update.message.reply_text(_pre(logic.show_current_hand()), parse_mode=ParseMode.MARKDOWN)


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
        trick_no = int(context.args[0])
        card_no = int(context.args[1])
        res = logic.goto_card(trick_no, card_no)
        await update.message.reply_text(res)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@require_auth
async def cmd_playoptimalcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    res = logic.play_optimal_card()
    await update.message.reply_text(res)


@require_auth
async def cmd_playoptimaltrick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await update.message.reply_text("Сначала задайте контракт командой /setcontract.")
        return
    logic: BridgeLogic = context.user_data.get("logic")
    if not logic:
        await update.message.reply_text("Сначала загрузите сдачу.")
        return
    res = logic.play_optimal_trick()
    await update.message.reply_text(res)


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
        res = logic.play_optimal_tricks(n)
        await update.message.reply_text(res if res else "Взятки разыграны.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# === CALLBACK‑КНОПКИ ===========================================================

@require_auth
@ignore_telegram_edit_errors
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- Главное меню ---
    if data == "menu_analyze":
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "Выберите способ ввода расклада:",
            reply_markup=analyze_menu_markup(),
        )
        return

    # --- Подменю "разработка", возвращаем в analyze ---
    if data in {"menu_privacy", "menu_thanks", "generate_deal"}:
        await query.edit_message_text(
            "Функция находится в разработке 🚧",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_analyze")]]),
        )
        return

    # --- Ввод PBN ---
    if data == "input_pbn":
        context.user_data["state"] = STATE_AWAIT_PBN
        await query.edit_message_text(
            "Введите PBN‑строку расклада:",
            reply_markup=back_to_analyze_markup(),  # Кнопка назад в analyze
        )
        return

    # --- Фото ---
    if data == "input_photo":
        context.user_data["state"] = STATE_AWAIT_PHOTO
        await query.edit_message_text(
            "Пришлите фото расклада для распознавания:",
            reply_markup=back_to_analyze_markup(),  # Кнопка назад в analyze
        )
        return

    # --- Кнопка назад из analyze в main ---
    if data == "back_main":
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "Я нахожусь на стадии закрытого бета‑тестирования.\n"
            "Тыкай на кнопки, ищи баги и пиши автору!",
            reply_markup=main_menu_markup(),
        )
        return

    # --- Кнопка назад из подменю в analyze ---
    if data == "back_analyze":
        context.user_data.pop("state", None)
        await query.edit_message_text(
            "Выберите способ ввода расклада:",
            reply_markup=analyze_menu_markup(),
        )
        return

    await query.answer("Неизвестная кнопка", show_alert=True)


@require_auth
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
            # ! Здесь ИСПОЛЬЗУЙ query.message.reply_text, а не update.message.reply_text
            await query.message.reply_text(
                f"PBN:\n{_pre(pbn)}",
                parse_mode=ParseMode.MARKDOWN
            )
            await query.message.reply_text(
                _pre(detector.preview()),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=analyze_result_markup()
            )
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
@ignore_telegram_edit_errors
async def analysis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("contract_set"):
        await query.answer("Сначала задайте контракт командой /setcontract.", show_alert=True)
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    # Страницы
    if data.startswith("analysis_page_"):
        page = int(data.split("_")[-1])
        await query.edit_message_reply_markup(reply_markup=analysis_keyboard(page))
        return

    # Кнопки анализа (вызов python‑метода)
    if data.startswith("analysis_"):
        cmd = data[len("analysis_"):]
        logic: BridgeLogic = context.user_data.get("logic")
        if not logic:
            await query.edit_message_text(
                "Нет активной сдачи. Введите расклад заново.",
                reply_markup=None
            )
            return

        # Найти текущую страницу по команде (чтобы не прыгало на первую)
        try:
            idx = [c[1] for c in ANALYSIS_COMMANDS].index(cmd)
            page = idx // ANALYSIS_CMDS_PER_PAGE
        except Exception:
            page = 0

        try:
            method = getattr(logic, cmd)
            result = method()
        except Exception as e:
            result = f"Ошибка: {e}"

        await query.edit_message_text(
            _pre(result) if result else "Готово.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analysis_keyboard(page)
        )
        return


# === ТЕКСТОВЫЙ ВВОД ============================================================

@require_auth
async def handle_pbn_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text.strip()

    if state == STATE_AWAIT_PBN:
        try:
            detector = BridgeCardDetector.from_pbn(text)
            context.user_data["detector"] = detector
            context.user_data.pop("state", None)
            await update.message.reply_text(
                _pre(detector.preview()),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=analyze_result_markup()
            )
        except ValueError as ve:
            await update.message.reply_text(f"Некорректный PBN: {ve}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка анализа: {e}")
    else:
        # если мы НЕ в режиме ожидания PBN, явно отправляем сообщение непонимания
        await unknown_message(update, context)

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
        await msg.reply_text(
            _pre(result),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=analyze_result_markup()
        )
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
    app = Application.builder().token(TOKEN).post_init(post_init).build()

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

    logging.info("Бот запущен …")
    app.run_polling()


if __name__ == "__main__":
    main()
