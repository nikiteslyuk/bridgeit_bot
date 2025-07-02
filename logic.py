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
SUIT_ICONS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
SUIT2DENOM = {
    "S": "spades",
    "H": "hearts",
    "D": "diamonds",
    "C": "clubs",
}
ICON2LTR = {v: k for k, v in SUIT_ICONS.items()}
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
    s = str(card)
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

    # ───── вывод рук ─────
    def display(self) -> str:
        suits = ("S", "H", "D", "C")
        icon  = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}

        def suit_line(pl, s):
            ranks = sorted(
                (card_rank(c) for c in self.deal[pl] if card_suit(c) == s),
                key=RANKS.index
            )
            return icon[s] + ("".join(ranks) if ranks else "–")

        blk = {pl: {s: suit_line(pl, s) for s in suits}
               for pl in (Player.north, Player.east,
                           Player.south, Player.west)}

        north_w = max(len(blk[Player.north][s]) for s in suits)
        south_w = max(len(blk[Player.south][s]) for s in suits)
        west_w  = max(len(blk[Player.west ][s]) for s in suits)
        east_w  = max(len(blk[Player.east ][s]) for s in suits)

        gap = 10
        total_w = west_w + gap + east_w

        lines: list[str] = []

        indent_n = (total_w - north_w) // 2
        for s in suits:
            lines.append(" " * indent_n + blk[Player.north][s].ljust(north_w))

        lines.append("")

        for s in suits:
            w = blk[Player.west][s].ljust(west_w)
            e = blk[Player.east][s].ljust(east_w)
            lines.append(f"{w}{' ' * gap}{e}")

        lines.append("")

        indent_s = (total_w - south_w) // 2
        for s in suits:
            lines.append(" " * indent_s + blk[Player.south][s].ljust(south_w))

        trick = fmt_seq(self._current) if self._current else "—"
        lines.append(f"\nТекущая взятка: {trick}")

        text = "\n".join(lines)
        if text.startswith(" "):
            text = text[1:]
        return ZWS + text

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

    # ───── служебные проверки ─────
    @staticmethod
    def _has_suit(hand, suit: str) -> bool:
        return any(card_suit(c) == suit for c in hand)

    # ───── одиночный ход ─────
    # ───── может быть ValueError ─────
    def play_card(self, card_str: str) -> str:
        """
        Делает одиночный ход указанной картой и возвращает строку-сообщение.

        Возможные варианты возвращаемых строк:
          «N обязан ходить ♠.» — если игрок нарушает фоллоу-сьют, ход отменён;
          «Ход: N♣A» — нормальный успешный ход.

        Исключения:
          ValueError — если карты нет у игрока.
        """
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

        Возможные строки-результаты:
          «Сначала завершите текущую неполную взятку.» — если
            уже начата взятка, но ещё не положены все 4 карты;
          «Разыграна взятка: N♥8  E♥7  S♥2  W♥K» — нормальный
            успешный розыгрыш (формат карты зависит от входа).

        Ошибки формата/правил по-прежнему вызывают ValueError.
        """
        if self._current:
            return "Сначала завершите текущую неполную взятку."

        toks = trick.split()
        if len(toks) != 4:
            raise ValueError("Нужно 4 карты.")

        seq: List[Tuple[Player, Card]] = []
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

    # ───── откат последней взятки ─────
    def undo_last_trick(self) -> str:
        """
        Отменяет последнюю завершённую взятку (ручную или из автоплана)
        и возвращает текст-сообщение.
        """
        if self._current:
            return "Сначала завершите текущую неполную взятку."

        if self._trick_history:
            seq = self._trick_history.pop()
            self._trick_manual_flags.pop()
        elif self._auto_plan:
            seq = self._auto_plan.pop()
            self._auto_manual_flags.pop()
        else:
            return "Нет информации о предыдущих взятках."

        for pl, c in reversed(seq):
            self.deal[pl].add(c)
        self.deal.first = seq[0][0]

        return "Откатили последнюю взятку."


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
        Формирует полный текст истории розыгрыша, включая счёт,
        и возвращает его одной строкой.

        Точно воспроизводит прежний построчный вывод.
        """
        unknown = 13 - self._start_len

        tricks = self._trick_history + self._auto_plan
        flags  = self._trick_manual_flags + self._auto_manual_flags
        if self._current:
            tricks.append(self._current)
            flags.append(self._current_manual + [False] * (4 - len(self._current)))

        lines: list[str] = ["", "История розыгрыша:"]
        line_no = 1

        for _ in range(unknown):
            lines.append(f"{line_no:2}: —")
            line_no += 1

        for seq, fl in zip(tricks, flags):
            lines.append(f"{line_no:2}: {fmt_seq(seq)}")
            if any(fl):
                prefix = " " * 4
                arrow = "  ".join("^^^" if i < len(fl) and fl[i] else "   "
                                  for i in range(4)).rstrip()
                lines.append(prefix + arrow)
            line_no += 1

        finished = [t for t in tricks if len(t) == 4]
        trump = None if self.contract in (None, Denom.nt) else self.contract
        ns = sum(trick_winner(t, trump) in (Player.north, Player.south)
                 for t in finished)
        ew = len(finished) - ns

        lines.append("")
        lines.append(f"Текущее состояние: NS – {ns}, EW – {ew}")
        lines.append("")

        return "\n".join(lines)

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
        Доигрывает сдачу *молча* (без вывода) от текущего состояния
        до конца, заполняя _auto_plan и _auto_manual_flags.
        """
        if self.contract is None:
            return

        self._clear_auto()
        unknown = 13 - self._start_len
        self.deal.trump = self.contract
        part_len = len(self._current)

        if part_len:
            cur = self._current.copy()
            manual_flags = self._current_manual.copy()
            for pl in clockwise_from(self.deal.first)[part_len:]:
                c = self.optimal_move()
                self.deal.play(c)
                cur.append((pl, c))
                manual_flags.append(False)
            self._auto_plan.append(cur)
            self._auto_manual_flags.append(manual_flags)

            self.deal.first = trick_winner(
                cur, None if self.contract is Denom.nt else self.contract
            )
            self._current.clear()
            self._current_manual.clear()

        while len(self.deal[Player.north]):
            trick, flags = [], []
            for pl in clockwise_from(self.deal.first):
                c = self.optimal_move()
                self.deal.play(c)
                trick.append((pl, c))
                flags.append(False)
            self._auto_plan.append(trick)
            self._auto_manual_flags.append(flags)

            self.deal.first = trick_winner(
                trick, None if self.contract is Denom.nt else self.contract
            )

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
    g = BridgeLogic(pbn)
    g.set_contract('nt', 'n')
    g.play_optimal_to_end()
    g.undo_last_trick()
    g.undo_last_trick()
    g.undo_last_trick()
    g.undo_last_trick()

    g.play_optimal_card()
    g.play_optimal_card()
    g.play_optimal_card()
    print(g.display())
    # print(g.display())
