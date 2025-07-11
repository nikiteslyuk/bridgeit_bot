#!/usr/bin/env python3
# solver.py — анализ pbn позиций

from __future__ import annotations

import io
import sys
import contextlib

import copy
import re
from typing import Dict, List, Tuple

from endplay.types import Deal, Player, Denom, Card
from endplay.dds.ddtable import calc_dd_table
from endplay.dds.solve import solve_board

# ─────────── константы ───────────
PLAYER_MAP: Dict[str, Player] = {p.abbr: p for p in Player}
PLAYER_CW = [Player.north, Player.east, Player.south, Player.west]

SUIT2DENOM = {
    "S": "spades",
    "H": "hearts",
    "D": "diamonds",
    "C": "clubs",
}

SUITS = ["S", "H", "D", "C"]
SUIT_ICONS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
ICON2LTR = {v: k for k, v in SUIT_ICONS.items()}

# Мб использовать эмодзи?
# SUIT_ICONS = {"S": "♠️", "H": "♥️", "D": "♦️", "C": "♣️"}
# ICON2LTR: Dict[str, str] = {}
# for ltr, icon in SUIT_ICONS.items():
#     ICON2LTR[icon] = ltr
#     ICON2LTR[icon.replace("\ufe0f", "")] = ltr
RANKS = "AKQJT98765432"
ZWS = " "


# ─────────── утилиты ───────────
def pbn_ok(pbn: str) -> str:
    return pbn.strip() if ":" in pbn.split()[0] else "N:" + pbn.strip()


def parse_contract(txt: str) -> Denom:
    """
    Принимает «3NT», «NT», «4S», «S» (регистр/пробелы/«10»/«НТ» не важны)
    и возвращает соответствующий объект Denom.*
    """
    t = (
        txt.strip()
          .upper()
          .replace("10", "T")
          .replace("НТ", "NT")
          .replace(" ", "")
    )

    m = re.fullmatch(r"([1-7]?)(NT|[SHDC])", t)
    if not m:
        raise ValueError("Формат контракта: 3NT / NT / 4S / S / …")

    _, denom_token = m.groups()

    if denom_token == "NT":
        return Denom.nt

    # S / H / D / C
    return getattr(Denom, SUIT2DENOM[denom_token])


def normalize_card(card: str) -> str:
    t = card.strip()
    for icon, ltr in ICON2LTR.items():
        t = t.replace(icon, ltr)
    t = t.lower().replace("10", "t")
    suit = next((c.upper() for c in t if c in "shdc"), None)
    rank = next((c.upper() for c in t if c in "tjqka98765432"), None)
    if not suit or not rank or len(t) not in (2, 3):
        raise ValueError(f"Не удаётся разобрать карту: {card}")
    return suit + rank


def clockwise_from(first: Player) -> List[Player]:
    i = PLAYER_CW.index(first)
    return PLAYER_CW[i:] + PLAYER_CW[:i]


def card_suit(card: Card) -> str:
    s = str(card).replace("\ufe0f", "")
    for icon, ltr in ICON2LTR.items():
        if icon in s:
            return ltr
    for l in "SHDC":
        if l in s.upper():
            return l
    raise RuntimeError("Не удалось определить масть карты.")


def card_rank(card: Card) -> str:
    for r in RANKS:
        if r in str(card).upper():
            return r
    raise RuntimeError("Не удалось определить номинал карты.")


def trick_winner(trick: List[Tuple[Player, Card]], trump: Denom | None) -> Player:
    led = card_suit(trick[0][1])
    trump_s = None if trump in (None, Denom.nt) else trump.name.upper()[0]

    def key(pc: Tuple[Player, Card]):
        _, c = pc
        suit, rank = card_suit(c), card_rank(c)
        suit_score = 2 if suit == trump_s else (1 if suit == led else 0)
        return suit_score, -RANKS.index(rank)

    return max(trick, key=key)[0]


def fmt_cards(cards: List[Card]) -> str:
    """Аккуратный вывод 2-символьных карт через 2 пробела."""
    return "  ".join(f"{str(c):>2}" for c in cards)


def fmt_card_full(pl: Player, card: Card) -> str:
    """Возвращает строку вида «N♥8» (3 символа)."""
    return f"{pl.abbr}{str(card):>2}"


def fmt_seq(seq: List[Tuple[Player, Card]]) -> str:
    """Форматирует целый список (взятку) в виде «N♥8  E♠A  …»."""
    return "  ".join(fmt_card_full(pl, c) for pl, c in seq)


# ─────────── основной класс ───────────
class BridgeLogic:
    # ───── может быть ValueError ─────
    def __init__(self, pbn: str):
        self._pbn_str = pbn_ok(pbn)
        self.deal = Deal.from_pbn(pbn_ok(pbn))
        self._orig_deal = copy.deepcopy(self.deal)
        self._start_len = len(self.deal[Player.north])

        if len({len(self.deal[p]) for p in Player}) != 1:
            raise ValueError("Во всех руках должно быть одинаковое количество карт.")

        self.contract: Denom | None = None
        self.declarer: Player | None = None

        self._trick_history: List[List[Tuple[Player, Card]]] = []
        self._trick_manual_flags: List[List[bool]] = []

        self._current: List[Tuple[Player, Card]] = []
        self._current_manual: List[bool] = []

        self._auto_plan: List[List[Tuple[Player, Card]]] = []
        self._auto_manual_flags: List[List[bool]] = []

    def to_pbn(self) -> str:
        return self._pbn_str[2:]

    # ───── допустимые ходы текущего игрока ─────
    def legal_moves(self) -> list[str]:
        """
        Возвращает список карт, которыми текущий игрок может
        походить в текущий момент (учитывается фоллоу-сьют).

        Формат каждой карты — строка 'RankSuit', например 'AS', '3C'.
        Variation-selector U+FE0F, если он есть, удаляется.
        """
        pl = self.current_player()
        if not self.deal[pl]:
            return []

        cards_sorted: list[Card] = sorted(
            self.deal[pl],
            key=lambda c: (
                SUITS.index(card_suit(c)),
                RANKS.index(card_rank(c))
            )
        )

        if not self._current:
            allowed = cards_sorted
        else:
            led = card_suit(self._current[0][1])
            if self._has_suit(self.deal[pl], led):
                allowed = [c for c in cards_sorted if card_suit(c) == led]
            else:
                allowed = cards_sorted

        return [f"{card_rank(c)}{card_suit(c)}" for c in allowed]

    # ───── вывод рук + указатель хода ─────
    def display(self) -> str:
        suits = ("S", "H", "D", "C")

        def suit_line(pl, s):
            ranks = sorted(
                (card_rank(c) for c in self.deal[pl] if card_suit(c) == s),
                key=RANKS.index,
            )
            return SUIT_ICONS[s] + ("".join(ranks) if ranks else "–")

        blk = {
            pl: {s: suit_line(pl, s) for s in suits}
            for pl in (Player.north, Player.east, Player.south, Player.west)
        }

        if not hasattr(self, "_fixed_west_hand_w"):
            WEST_GAP = 3
            self._fixed_west_hand_w = max(len(blk[Player.west][s]) for s in suits)
            self._fixed_side_w      = self._fixed_west_hand_w + WEST_GAP

        WEST_GAP    = 3
        WEST_HAND_W = self._fixed_west_hand_w
        SIDE_W      = self._fixed_side_w
        MID_W       = 13
        EAST_GAP    = WEST_GAP
        e_shift     = " " * EAST_GAP

        seq = self._current
        arrow = {Player.north: "↑", Player.east: "→",
                 Player.south: "↓", Player.west: "←"}

        center_rows = [" " * MID_W for _ in range(4)]

        for pl, card in seq:
            tok = f"{arrow[pl]}{card}" if pl is Player.west else f"{card}{arrow[pl]}"
            row = {Player.north: 0, Player.east: 1,
                   Player.west: 2, Player.south: 3}[pl]
            if pl is Player.west:
                center_rows[row] = tok.ljust(MID_W)
            elif pl is Player.east:
                center_rows[row] = tok.rjust(MID_W - 1) + " "
            else:
                center_rows[row] = tok.center(MID_W)

        if any(len(self.deal[p]) for p in Player):
            cur_pl = self.current_player()
            tok = arrow[cur_pl]
            row = {Player.north: 0, Player.east: 1,
                   Player.west: 2,  Player.south: 3}[cur_pl]
            if cur_pl is Player.west:
                center_rows[row] = tok.ljust(MID_W)
            elif cur_pl is Player.east:
                center_rows[row] = tok.rjust(MID_W - 1) + " "
            else:
                center_rows[row] = tok.center(MID_W)

        tok_len   = len(str(seq[0][1])) + 1 if seq else 1
        left_pad  = (MID_W - tok_len) // 2
        spacing_NS = " " * (SIDE_W + left_pad)

        lines: list[str] = []

        for s in suits:
            lines.append(spacing_NS + blk[Player.north][s])
        lines.append("")

        for idx, s in enumerate(suits):
            w_hand = blk[Player.west][s].ljust(WEST_HAND_W) + " " * WEST_GAP
            e_hand = e_shift + blk[Player.east][s]
            lines.append(f"{w_hand}{center_rows[idx]}{e_hand}")
        lines.append("")

        for s in suits:
            lines.append(spacing_NS + blk[Player.south][s])

        return "\n".join(lines)

    # ───── DD-таблица исходной сдачи ─────
    def dd_table(self) -> str:
        if not all(len(self._orig_deal[p]) == 13 for p in Player):
            return "В исходной сдаче не по 13 карт — DDS недоступен."

        dd = calc_dd_table(self._orig_deal)

        denoms = [Denom.clubs, Denom.diamonds, Denom.hearts,
                  Denom.spades, Denom.nt]
        icon = {Denom.clubs: "♣", Denom.diamonds: "♦", Denom.hearts: "♥",
                Denom.spades: "♠", Denom.nt: "NT"}

        lines: list[str] = []
        header = " " + " ".join(f"{icon[d]:>2}" for d in denoms)
        lines.append(ZWS + header)

        for pl in (Player.north, Player.south, Player.east, Player.west):
            vals = []
            for d in denoms:
                try:
                    v = dd[d, pl]
                except Exception:
                    v = dd[pl, d]
                vals.append(f"{v:>2}")
            lines.append(f"{pl.abbr} " + " ".join(vals))

        return "\n".join(lines)

    # ───── контракт ─────
    # ───── может быть ValueError ─────
    def set_contract(self, contract: str, first: str) -> str:
        """
        Задаёт контракт и игрока, который делает ПЕРВЫЙ ход
        (а не «декларанта», как раньше).

        contract – строка вида «3NT», «4S», «NT» …  
        first    – «N» / «E» / «S» / «W».

        Возвращает строку вида:
        «Задан контракт 3NT, первый ход у N.»
        """
        self.contract = parse_contract(contract)

        pl = PLAYER_MAP.get(first.upper())
        if pl is None:
            raise ValueError("Первый игрок — одна из N/E/S/W.")

        self.deal.first = pl
        self.deal.trump = self.contract

        return f"Задан контракт {contract.upper()}, первый ход у {pl.abbr}."

    # ───── ход / оптимальные ходы ─────
    def current_player(self) -> Player:
        return clockwise_from(self.deal.first)[len(self._current)]

    def optimal_move(self) -> Card:
        if self.contract is None:
            raise RuntimeError("Сначала задайте контракт.")
        self.deal.trump = self.contract
        return max(solve_board(self.deal), key=lambda ct: ct[1])[0]

    def show_move_options(self) -> str:
        """
        Формирует строку-отчёт о возможных ходах текущего игрока.

        Формат точно повторяет прежний вывод:
            Анализ руки N:
             ♠ и ходы
             ...
            <пустая строка>
             ♥ и ходы
             ...
            <пустая строка>
            …

        Возвращает готовый текст. Если контракт не задан – RuntimeError.
        """
        if self.contract is None:
            raise RuntimeError("Сначала задайте контракт.")
        self.deal.trump = self.contract

        results = list(solve_board(self.deal))

        lines: list[str] = [f"Анализ руки {self.deal.first.abbr}:"]

        for suit in ("S", "H", "D", "C"):
            suit_moves = [(c, v) for c, v in results if card_suit(c) == suit]
            if not suit_moves:
                continue

            suit_moves.sort(key=lambda cv: (-cv[1], RANKS.index(card_rank(cv[0]))))

            for c, v in suit_moves:
                lines.append(f"{str(c):>2}: {v}")

            lines.append("")

        if lines and lines[-1] == "":
            lines.pop()

        return "\n".join(lines)

    def move_options(self) -> dict[str, int]:
        """
        Возвращает словарь возможных ходов текущего игрока
        формата {'AS': 8, 'KH': 7, …},
        где ключ — карта RankSuit (rank → A,K,Q,J,T,9…; suit → S,H,D,C),
        а значение — максимальное количество взяток, которое
        остаётся у стороны-хозяина хода при оптимальной игре
        после выхода этой картой.

        Требует предварительно заданного контракта.
        """
        pl = self.current_player()
        if not self.deal[pl]:
            return {}
        results = list(solve_board(self.deal))
        return {f"{card_rank(c)}{card_suit(c)}": tricks for c, tricks in results}

    # ───── служебные проверки ─────
    @staticmethod
    def _has_suit(hand, suit: str) -> bool:
        return any(card_suit(c) == suit for c in hand)

    def _flush_auto(self) -> None:
        """
        Переносит все автосыгранные взятки (_auto_plan) в обычную историю
        (_trick_history) в хронологическом порядке и очищает буферы авто-плана.
        Этот метод вызывается каждый раз, когда пользователь делает
        «ручное» действие либо заново запускает автодоигрыш.
        """
        if self._auto_plan:
            self._trick_history.extend(self._auto_plan)
            self._trick_manual_flags.extend(self._auto_manual_flags)
            self._auto_plan.clear()
            self._auto_manual_flags.clear()

    # ───── одиночный ход ─────
    # ───── может быть ValueError ─────
    def play_card(self, card_str: str) -> str:
        """
        Делает одиночный ход указанной картой и возвращает строку-сообщение.
        """
        # сначала фиксируем все ранее автосыгранные, чтобы они не слетели
        self._flush_auto()

        pl = self.current_player()
        card = Card(normalize_card(card_str))

        if card not in self.deal[pl]:
            raise ValueError(f"{card} нет у {pl.abbr}.")

        if self._current:
            led = card_suit(self._current[0][1])
            if self._has_suit(self.deal[pl], led) and card_suit(card) != led:
                return f"{pl.abbr} обязан ходить {SUIT_ICONS[led]}."

        self.deal.play(card)
        self._current.append((pl, card))
        self._current_manual.append(True)

        message = f"Ход: {fmt_card_full(pl, card)}"

        if len(self._current) == 4:
            self._trick_history.append(self._current.copy())
            self._trick_manual_flags.append(self._current_manual.copy())
            trump = None if self.contract is Denom.nt else self.contract
            self.deal.first = trick_winner(self._current, trump)
            self._current.clear()
            self._current_manual.clear()

        return message

    # ───── может быть ValueError ─────
    def play_trick(self, trick: str) -> str:
        """
        Принимает строку из 4 карт, полностью разыгрывает взятку
        и возвращает итоговое сообщение.
        """
        # фиксируем предыдущий автоплан
        self._flush_auto()

        if self._current:
            return "Сначала завершите текущую неполную взятку."

        toks = trick.split()
        if len(toks) != 4:
            raise ValueError("Нужно 4 карты.")

        seq: list[tuple[Player, Card]] = []
        for pl, tok in zip(clockwise_from(self.deal.first), toks):
            c = Card(normalize_card(tok))
            if c not in self.deal[pl]:
                raise ValueError(f"{c} нет у {pl.abbr}.")
            seq.append((pl, c))

        led = card_suit(seq[0][1])
        for pl, c in seq[1:]:
            if self._has_suit(self.deal[pl], led) and card_suit(c) != led:
                raise ValueError(f"{pl.abbr} обязан ходить {SUIT_ICONS[led]}")

        for _, c in seq:
            self.deal.play(c)

        self._trick_history.append(seq)
        self._trick_manual_flags.append([True, True, True, True])

        trump = None if self.contract is Denom.nt else self.contract
        self.deal.first = trick_winner(seq, trump)

        return f"Разыграна взятка: {fmt_seq(seq)}"

    def _restore_card(self, pl: Player, card: Card) -> None:
        if card not in self.deal[pl]:
            self.deal[pl].add(card)
        if hasattr(self.deal, "_played"):
            self.deal._played.discard(card)

    # ───── откат последней взятки ─────
    def undo_last_trick(self) -> str:
        if self._current:
            return "Сначала завершите текущую неполную взятку."

        if self._auto_plan:
            seq = self._auto_plan.pop()
            self._auto_manual_flags.pop()
        elif self._trick_history:
            seq = self._trick_history.pop()
            self._trick_manual_flags.pop()
        else:
            return "Нет информации о предыдущих взятках."

        for pl, card in reversed(seq):
            try:
                self.deal.unplay()
            except RuntimeError:
                pass
            self._restore_card(pl, card)

        self.deal.first = seq[0][0]
        return "Откатили последнюю взятку."

    # ───── отмена последнего хода (карты) ─────
    def undo_last_card(self) -> str:
        if self._current:
            pl, card = self._current.pop()
            self._current_manual.pop()
            try:
                self.deal.unplay()
            except RuntimeError:
                pass
            self._restore_card(pl, card)
            return f"Отменили ход: {pl.abbr}{card}"

        if self._auto_plan:
            seq = self._auto_plan.pop()
            flags = self._auto_manual_flags.pop()
        elif self._trick_history:
            seq = self._trick_history.pop()
            flags = self._trick_manual_flags.pop()
        else:
            return "Нет предыдущих ходов для отмены."

        for pl, card in reversed(seq):
            try:
                self.deal.unplay()
            except RuntimeError:
                pass
            self._restore_card(pl, card)

        self._current.clear()
        self._current_manual.clear()
        if len(seq) > 1:
            self.deal.first = seq[0][0]
            for i in range(len(seq) - 1):
                pl, card = seq[i]
                self.deal.play(card)
                self._current.append((pl, card))
                self._current_manual.append(flags[i])
        pl_last, card_last = seq[-1]
        return f"Отменили ход: {pl_last.abbr}{card_last}"

    # ───── может быть ValueError ─────
    def goto_trick(self, no_: int) -> str:
        """
        Перемещается к началу указанной взятки (то есть перед первой
        картой в ней) и возвращает сообщение «Откатились ко взятке N.».

        Ошибки («Номер взятки некорректен» и т. д.) по-прежнему
        выбрасываются из `goto_card`.
        """
        self.goto_card(no_, 1)
        return f"Откатились ко взятке {no_}."

    # ───── история всех взяток + счёт ─────
    def show_history(self) -> str:
        """
        Корректно показывает историю розыгрыша в хронологическом порядке.
        Поддерживает частично начатые и автосыгранные взятки и правильно
        пересчитывает счёт NS/EW.
        """
        unknown = 13 - self._start_len

        tricks: list[list[tuple[Player, Card]]] = []
        flags:  list[list[bool]]               = []

        tricks.extend(self._trick_history)
        flags.extend(self._trick_manual_flags)

        tricks.extend(self._auto_plan)
        flags.extend(self._auto_manual_flags)

        if self._current:
            tricks.append(self._current)
            flags.append(self._current_manual + [False] * (4 - len(self._current)))

        out = ["", "История розыгрыша:"]
        line_no = 1

        for _ in range(unknown):
            out.append(f"{line_no:2}: —")
            line_no += 1

        for seq, fl in zip(tricks, flags):
            out.append(f"{line_no:2}: {fmt_seq(seq)}")
            if any(fl):
                padded = fl + [False] * (4 - len(fl))
                arrow = "  ".join("^^^" if f else "   " for f in padded).rstrip()
                out.append("    " + arrow)
            line_no += 1

        trump = None if self.contract in (None, Denom.nt) else self.contract
        finished = [t for t in tricks if len(t) == 4]
        ns = sum(trick_winner(t, trump) in (Player.north, Player.south)
                 for t in finished)
        ew = len(finished) - ns

        out += ["", f"Текущее состояние: NS – {ns}, EW – {ew}", ""]
        return "\n".join(out)

    def history_matrix(self) -> tuple[list[list[str]], int]:
        """
        Возвращает (tricks, unknown),
        где  tricks  – список взяток (каждая — list[str] вида 'N♠A'),
             unknown – сколько ранних взяток неизвестно (N: —).

        Неполная текущая взятка включается последней.
        """
        unknown = 13 - self._start_len

        seqs: list[list[tuple[Player, Card]]] = []
        seqs.extend(self._trick_history)
        seqs.extend(self._auto_plan)
        if self._current:
            seqs.append(self._current)

        tricks: list[list[str]] = [
            [fmt_card_full(pl, c) for pl, c in trick] for trick in seqs
        ]
        return tricks, unknown

    # ───── история строками без заголовков ─────
    def history_plain_lines(self) -> list[str]:
        """
        Возвращает список строк вида
            ' 1: W♠K  N♠2  E♠3  S♠4'
            ' 2: …'
        без заголовков и счёта.
        """
        tricks: list[list[tuple[Player, Card]]] = []
        tricks.extend(self._trick_history)
        tricks.extend(self._auto_plan)
        if self._current:
            tricks.append(self._current)

        lines: list[str] = []
        for idx, trick in enumerate(tricks, 1):
            seq = "  ".join(fmt_card_full(pl, c) for pl, c in trick)
            lines.append(f"{idx:2}: {seq}")
        return lines


    # ───── тихий откат автоплана ─────
    def _clear_auto(self):
        """Снимает автодоигрыш, возвращая карты в deal."""
        if not self._auto_plan:
            return
        for seq in reversed(self._auto_plan):
            for pl, c in reversed(seq):
                self.deal[pl].add(c)
        self._auto_plan.clear()
        self._auto_manual_flags.clear()

    # ───── доигрыш + план розыгрыша ─────
    def play_optimal_to_end(self) -> None:
        """
        Доигрывает сдачу до конца оптимальными картами по DDS.
        Учёт частично начатой взятки и фиксация уже сыгранных авто-взяток.
        """
        if self.contract is None or all(len(self.deal[p]) == 0 for p in Player):
            return

        # сначала принимаем все уже разыгранные автосыгранные взятки
        self._flush_auto()
        # а возможный автоплан «наперед» (после откатов) убираем
        self._clear_auto()

        # Доигрываем неполную текущую взятку
        if self._current:
            cur = self._current.copy()
            manual_flags = self._current_manual.copy()
            for pl in clockwise_from(self.deal.first)[len(self._current):]:
                c = self.optimal_move()
                self.deal.play(c)
                cur.append((pl, c))
                manual_flags.append(False)
            self._auto_plan.append(cur)
            self._auto_manual_flags.append(manual_flags)
            self.deal.first = trick_winner(
                cur, None if self.contract is Denom.nt else self.contract)
            self._current.clear()
            self._current_manual.clear()

        # Затем доигрываем оставшиеся взятки
        while any(len(self.deal[p]) for p in Player):
            trick, flags = [], []
            for pl in clockwise_from(self.deal.first):
                c = self.optimal_move()
                self.deal.play(c)
                trick.append((pl, c))
                flags.append(False)
            self._auto_plan.append(trick)
            self._auto_manual_flags.append(flags)
            self.deal.first = trick_winner(
                trick, None if self.contract is Denom.nt else self.contract)

    def show_current_hand(self) -> str:
        """
        Возвращает строку вида:

        Рука N:
        ♠AKT
        ♥–
        ♦T72
        ♣842
        """
        pl = self.current_player()
        suits_order = ("♠", "♥", "♦", "♣")

        suit_cards: Dict[str, List[str]] = {icon: [] for icon in suits_order}
        for card in self.deal[pl]:
            icon = SUIT_ICONS[card_suit(card)]
            suit_cards[icon].append(card_rank(card))

        for icon in suits_order:
            suit_cards[icon].sort(key=lambda r: RANKS.index(r))

        lines = [f"Рука {pl.abbr}:"]
        for icon in suits_order:
            ranks = "".join(suit_cards[icon]) or "–"
            lines.append(f"{icon}{ranks}")

        return "\n".join(lines)

    # ───── может быть ValueError ─────
    def goto_card(self, trick_no: int, card_no: int) -> str:
        """
        Откатывается к моменту *перед* картой №card_no (1–4) во взятке №trick_no
        (нумерация как в show_history, с учётом ранних «—»).

        Возвращает строку:
            «Откатились к взятке N, карта M.»

        При неверных параметрах по-прежнему возбуждает ValueError.
        """
        if not (1 <= card_no <= 4):
            raise ValueError("Номер карты 1–4.")

        unknown = 13 - self._start_len
        if trick_no <= unknown:
            raise ValueError("Эти ранние взятки неизвестны – откат невозможен.")

        full_tricks = self._trick_history + self._auto_plan
        full_flags  = self._trick_manual_flags + self._auto_manual_flags
        idx = trick_no - unknown - 1
        if idx >= len(full_tricks):
            raise ValueError("Такой взятки ещё нет.")

        target_trick = full_tricks[idx]
        target_flags = full_flags[idx]

        self.deal = copy.deepcopy(self._orig_deal)
        self._trick_history.clear()
        self._trick_manual_flags.clear()
        self._auto_plan.clear()
        self._auto_manual_flags.clear()
        self._current.clear()
        self._current_manual.clear()

        trump = None if self.contract in (None, Denom.nt) else self.contract

        for i in range(idx):
            seq, fl = full_tricks[i], full_flags[i]
            for _, c in seq:
                self.deal.play(c)
            self._trick_history.append(seq)
            self._trick_manual_flags.append(fl)
            self.deal.first = trick_winner(seq, trump)

        self.deal.first = target_trick[0][0]
        for j in range(card_no - 1):
            pl, card = target_trick[j]
            self.deal.play(card)
            self._current.append((pl, card))
            self._current_manual.append(target_flags[j])

        return f"Откатились к взятке {trick_no}, карта {card_no}."

    def play_optimal_card(self, *, announce: bool = True) -> str:
        """
        Делает оптимальный (по DDS) ход для текущего игрока и
        возвращает строку-сообщение.
        """
        if self.contract is None:
            return "Сначала задайте контракт." if announce else ""

        if all(len(self.deal[p]) == 0 for p in Player):
            return "Сдача уже закончена — ходов нет." if announce else ""

        if len(self._current) == 4:
            trump = None if self.contract is Denom.nt else self.contract
            self._trick_history.append(self._current.copy())
            self._trick_manual_flags.append(self._current_manual.copy())
            self.deal.first = trick_winner(self._current, trump)
            self._current.clear()
            self._current_manual.clear()

        card = self.optimal_move()
        pl   = self.current_player()
        self.deal.play(card)
        self._current.append((pl, card))
        self._current_manual.append(False)

        message = f"Оптимальный ход: {fmt_card_full(pl, card)}" if announce else ""

        if len(self._current) == 4:
            trump = None if self.contract is Denom.nt else self.contract
            self._trick_history.append(self._current.copy())
            self._trick_manual_flags.append(self._current_manual.copy())
            self.deal.first = trick_winner(self._current, trump)
            self._current.clear()
            self._current_manual.clear()

        return message

    def play_optimal_trick(self, *, announce: bool = True) -> str:
        """
        Разыгрывает одну взятку оптимально (по DDS) и возвращает строку-сообщение.
        """

        if self.contract is None or all(len(self.deal[p]) == 0 for p in Player):
            return "Сдача уже закончена." if announce else ""

        before = len(self._trick_history)
        while len(self._trick_history) == before and any(len(self.deal[p]) > 0 for p in Player):
            self.play_optimal_card(announce=False)

        if len(self._trick_history) > before and announce:
            seq = self._trick_history[-1]
            return f"Оптимально разыграна взятка: {fmt_seq(seq)}"

        return ""

    def play_optimal_tricks(self, n: int, *, announce: bool = False) -> str:
        """
        Разыгрывает следующие *n* взяток оптимально.

        Если есть начатая неполная взятка — доигрывает её за 1-й шаг.  
        announce=False (default) → без вывода вообще.  
        Можно поставить True, тогда будет вывод по 1 строке на взятку.
        """
        if n <= 0:
            return

        played = 0
        while played < n and len(self.deal[Player.north]) > 0:
            self.play_optimal_trick(announce=announce)
            played += 1

        return ""


# ─────────── демо ───────────
if __name__ == "__main__":
    # pbn = "W:52.AK64.Q8.AT863 KQJT98.83.KJ.QJ7 A3.QJT7.T762.K95 764.952.A9543.42"
    pbn = "T652.7652.Q6.AKJ 3.3.T97532.Q9853 Q4.AKQ984.AK4.76 AKJ987.JT.J8.T42"
    # pbn = "AKQJT98765432... .AKQJT98765432.. ...AKQJT98765432 ..AKQJT98765432."
    g = BridgeLogic(pbn)
    g.set_contract('s', 'n')
    print(g.show_move_options())
    print(g.move_options())


