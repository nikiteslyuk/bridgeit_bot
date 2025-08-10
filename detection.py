#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set, Tuple

import numpy as np
import cv2
from ultralytics import YOLO, __version__ as ULTRA_VER
from sklearn.mixture import GaussianMixture

# ──────────── константы ────────────
MODEL = "yolov8s_playing_cards.pt"
MAX_HAND_LEN = 13
RANKS = "AKQJT98765432"
SUITS = "SHDC"
ALL_CARDS: Set[str] = {r + s for s in SUITS for r in RANKS}

ORDER = ["W", "N", "E", "S"]
PREVIEW_ORDER = ["S", "E", "N", "W"]
SUIT_SYM = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}


def _card_unicode(card: str) -> str:
    """'AS' → 'A♠', 'TS' → 'T♠' (десятка теперь T, а не 10)."""
    r, s = card[0], card[1]
    return f"{r}{SUIT_SYM[s]}"


def _hand_pretty(hand: Set[str]) -> str:
    """
    Форматирует одну руку в строчку вида
    ♠AKJ  ♥QJ  ♦T98  ♣—
    десятка выводится буквой T.
    """
    res = []
    for s in "SHDC":
        ranks = [r for r in RANKS if r + s in hand]
        res.append(f"{SUIT_SYM[s]}{''.join(ranks) or '—'}")
    return "  ".join(res)


# ──────────── основной класс ────────────
class BridgeCardDetector:
    def __init__(self, img_path: str, model_path: str = MODEL):
        if tuple(map(int, ULTRA_VER.split(".")[:3])) < (8, 2, 0):
            raise RuntimeError("Требуется Ultralytics ≥ 8.2.0")

        self.img_path = Path(img_path)
        self.model = YOLO(model_path)
        self._dealer: str = "N"

        self.hands: Dict[str, Set[str]] = {p: set() for p in ORDER}
        self._dealer: str | None = None
        # (x1, y1, x2, y2, raw_label, player)
        self._dets: List[Tuple[int, int, int, int, str, str]] = []

        self._process()
        self._auto_fill_trivial()

    # ---------- сервис для UI кнопок ----------
    def lost_cards(self) -> list[str]:
        """
        Список карт, которые ещё не лежат ни в одной руке.
        Отсортирован масть-за-мастью S-H-D-C и внутри масти A-K-…-2.
        """
        missing = ALL_CARDS - set().union(*self.hands.values())
        return sorted(
            missing,
            key=lambda c: (SUITS.index(c[1]), RANKS.index(c[0]))
        )

    def hand_cards(self, hand: str) -> list[str]:
        """
        Карты выбранной руки (N/E/S/W), отсортированные как выше.
        """
        hand = self._norm_player(hand)
        return sorted(
            self.hands.get(hand, []),
            key=lambda c: (SUITS.index(c[1]), RANKS.index(c[0]))
        )

    def _auto_fill_trivial(self) -> str:
        """
        Если пропущенные карты *обязательно* принадлежат единственной
        недоукомплектованной руке, добавляет их туда автоматически и
        возвращает сообщение вида

            «Тривиально определилось положение потерянных 3 карт → N: 10♠ A♦ …»

        Если автодополнение не произошло — возвращает пустую строку.
        """
        missing = ALL_CARDS - set().union(*self.hands.values())
        if not missing:
            return ""

        needs = {p: MAX_HAND_LEN - len(self.hands[p]) for p in ORDER}
        candidates = [p for p, n in needs.items() if n > 0]

        if len(candidates) == 1 and needs[candidates[0]] == len(missing):
            hand = candidates[0]
            self.hands[hand].update(missing)

            pretty = " ".join(_card_unicode(c) for c in sorted(missing))
            msg = (f"Тривиально определилось положение потерянных "
                   f"{len(missing)} карт → {hand}: {pretty}")

            return msg

        return ""

    def current_order(self) -> list[str]:
        """
        Порядок для вывода рук: с дилера по часовой стрелке.
        """
        d = self._dealer or "N"
        d = self._norm_player(d)
        start = ORDER.index(d)
        return ORDER[start:] + ORDER[:start]

    # ──────────── публичные операции ────────────
    def preview(self) -> str:
        """
        Возвращает текст-сводку:
        Распознанный расклад:
        <dealer> (13/13): ...
        <next>   (13/13): ...
        <next>   (13/13): ...
        <next>   (13/13): ...
        """
        lines: list[str] = ["Распознанный расклад:"]
        for p in self.current_order():
            lines.append(f"{p} ({len(self.hands[p])}/13): {_hand_pretty(self.hands[p])}")

        miss = self.missing_cards()
        if miss:
            lines.append("")
            lines.append("Потерянные карты: " + " ".join(_card_unicode(c) for c in miss))
        else:
            lines.append("")
            lines.append("Все карты определены.")

        return "\n".join(lines)

    def visualize(self, save: str, *, debug: bool = False):
        """
        Сохранить изображение с разметкой распознанных карт.

        Parameters
        ----------
        save : str
            Путь, куда сохранить получившийся файл.
        debug : bool, default = False
            • False — классическая разметка: цвет по рукам, подпись — буква руки.
            • True  — отладочная: цвет по масти, подпись «карта  уверенность».
              Подписи автоматически разводятся по вертикали, чтобы не накладываться.
        """
        if not save:
            raise ValueError("Путь для сохранения изображения должен быть задан.")

        img = cv2.imread(str(self.img_path))
        if img is None:
            raise FileNotFoundError(f"Не удалось открыть изображение: {self.img_path}")

        # --- цветовые схемы -------------------------------------------------
        suit_colors = {
            "S": (200, 70, 70),
            "H": (70, 70, 200),
            "D": (0, 140, 200),
            "C": (70, 200, 70),
        }
        player_colors = {
            "S": (200, 70, 70),
            "E": (70, 70, 200),
            "W": (0, 140, 200),
            "N": (70, 200, 70),
        }

        def get_color(raw_lbl: str, player: str):
            return suit_colors[self._norm_card(raw_lbl)[1]] if debug else player_colors[player]

        # --- помощник, чтобы подписи не налегали друг на друга --------------
        occupied: list[tuple[int, int, int, int]] = []

        def place_text(box, txt_size):
            x1, y1, _, _ = box
            w, h = txt_size
            dy = 0
            while any(
                abs((y1 - dy) - oy) < h + 6 and abs(x1 - ox) < w
                for ox, oy, w, h in occupied
            ):
                dy += h + 6
            occupied.append((x1, y1 - dy, w, h))
            return dy

        # --- наносим рамки и подписи ----------------------------------------
        for det in self._dets:
            if len(det) == 7:
                x1, y1, x2, y2, raw, pl, conf = det
            else:
                x1, y1, x2, y2, raw, pl = det
                conf = 0.0

            color = get_color(raw, pl)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)

            if debug:
                label = self._norm_card(raw)
                text = f"{label} {conf:.2f}"
            else:
                text = pl

            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            offset = place_text((x1, y1, x2, y2), (tw, th))

            cv2.rectangle(
                img,
                (x1, y1 - th - 6 - offset),
                (x1 + tw + 4, y1 - offset),
                color,
                -1,
            )
            cv2.putText(
                img,
                text,
                (x1 + 2, y1 - 4 - offset),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )

        cv2.imwrite(save, img)

    def missing_cards(self) -> List[str]:
        return sorted(ALL_CARDS - set().union(*self.hands.values()))

    # ---------- add / move / remove ----------
    def add(self, spec: str):
        """
        «<карта> <рука>» — добавить карту в указанную руку.
        Напр.  'QH E'  (валет червей к востоку).
        """
        card_tok, hand_tok = spec.strip().split()
        card = self._norm_card(card_tok)
        hand = self._norm_player(hand_tok)

        for p in ORDER:
            if card in self.hands[p] and p != hand:
                raise ValueError(f"{_card_unicode(card)} уже у {p}")

        self.hands[hand].add(card)

        for i, det in enumerate(self._dets):
            raw = det[4]
            if self._norm_card(raw) == card and det[5] != hand:
                conf = det[6] if len(det) == 7 else 0.0
                self._dets[i] = (*det[:5], hand, conf)

        self._auto_fill_trivial()

    def move(self, spec: str):
        """
        «<карта> <куда>» — перекинуть карту из текущей руки в другую.
        Например  'AS N'  — туз пик к северу.
        """
        card_tok, dest_hand = spec.strip().split()
        card = self._norm_card(card_tok)
        dest = self._norm_player(dest_hand)

        src = next((p for p in ORDER if card in self.hands[p]), None)
        if src is None:
            raise ValueError(f"{_card_unicode(card)} не найдена")
        if src == dest:
            return
        if card in self.hands[dest]:
            raise ValueError(f"{_card_unicode(card)} уже у {dest}")

        # переносим
        self.hands[src].remove(card)
        self.hands[dest].add(card)

        # правим _dets
        for i, det in enumerate(self._dets):
            if self._norm_card(det[4]) == card:
                conf = det[6] if len(det) == 7 else 0.0
                self._dets[i] = (*det[:5], dest, conf)

        self._auto_fill_trivial()

    # ---------- вращение стола ----------
    def clockwise(self):
        """
        Сдвиг по часовой стрелке:  N→E, E→S, S→W, W→N
        """
        old_hands = {p: set(self.hands[p]) for p in ORDER}
        for i, p in enumerate(ORDER):
            self.hands[ORDER[(i + 1) % 4]] = old_hands[p]

        def shift(pl): return ORDER[(ORDER.index(pl) + 1) % 4]
        self._dets = [
            (*det[:5], shift(det[5]), det[6] if len(det) == 7 else 0.0)
            for det in self._dets
        ]

    def uclockwise(self):
        """
        Сдвиг против часовой:  N→W, W→S, S→E, E→N
        """
        old_hands = {p: set(self.hands[p]) for p in ORDER}
        for i, p in enumerate(ORDER):
            self.hands[ORDER[(i - 1) % 4]] = old_hands[p]

        def shift(pl): return ORDER[(ORDER.index(pl) - 1) % 4]
        self._dets = [
            (*det[:5], shift(det[5]), det[6] if len(det) == 7 else 0.0)
            for det in self._dets
        ]

    # ---------- прочее ----------
    def to_pbn(self, dealer: str | None = None) -> str:
        """
        Вернуть строку PBN с правильным порядком в зависимости от сдающего.
        Если dealer не указан и не установлен ранее ‒ автоматически берётся 'N'.
        """
        # 1. Определяем сдающего
        d = dealer or self._dealer or "N"
        d = self._norm_player(d)
        # 2. Собираем порядок рук с дилера по часовой стрелке
        start = ORDER.index(d)
        order = ORDER[start:] + ORDER[:start]
        # 3. Формируем строку для каждой руки (масти через точку, руки через пробел)
        parts = []
        for p in order:
            suits = ["".join(r for r in RANKS if r + s in self.hands[p]) for s in SUITS]
            parts.append(".".join(suits))
        # 4. Склеиваем в одну строку PBN
        # return f"{d}:{' '.join(parts)}"
        return f"{' '.join(parts)}"

    # ──────────── внутренние детали ────────────
    def _process(self):
        img = cv2.imread(str(self.img_path))
        if img is None:
            raise FileNotFoundError(self.img_path)

        pred = self.model.predict(img, imgsz=1600, augment=True,
                                  conf=0.55, verbose=False)[0]
        if len(pred.boxes) < 4:
            return

        id2label = self.model.names
        centers, info = [], []
        for b in pred.boxes:
            x1, y1, x2, y2 = map(float, b.xyxy[0])
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            centers.append([cx, cy])
            info.append((int(x1), int(y1), int(x2), int(y2),
                         id2label[int(b.cls)], float(b.conf.cpu().item())))
        centers = np.array(centers)

        gmm = GaussianMixture(n_components=4, covariance_type="full",
                              random_state=0)
        labels = gmm.fit_predict(centers)
        centroids = gmm.means_

        # --- определяем, какой кластер чьей руке соответствует ---
        cl2pts = defaultdict(list)
        for (cx, cy), cl in zip(centers, labels):
            cl2pts[cl].append((cx, cy))
        orient = {}
        for cl, pts in cl2pts.items():
            xs, ys = zip(*pts)
            orient[cl] = "H" if max(xs) - min(xs) >= max(ys) - min(ys) else "V"

        horiz = [cl for cl, o in orient.items() if o == "H"]
        vert = [cl for cl, o in orient.items() if o == "V"]
        if len(horiz) == 2 and len(vert) == 2:
            north = min(horiz, key=lambda i: np.median([p[1] for p in cl2pts[i]]))
            south = max(horiz, key=lambda i: np.median([p[1] for p in cl2pts[i]]))
            west = min(vert,  key=lambda i: np.median([p[0] for p in cl2pts[i]]))
            east = max(vert,  key=lambda i: np.median([p[0] for p in cl2pts[i]]))
        else:
            north = int(np.argmin([c[1] for c in centroids]))
            south = int(np.argmax([c[1] for c in centroids]))
            rest = [i for i in range(4) if i not in (north, south)]
            west, east = sorted(rest, key=lambda i: centroids[i][0])

        cluster2p = {north: "N", south: "S", west: "W", east: "E"}
        self._cluster2p = cluster2p
        self._cluster_centroids = centroids
        pl2cluster = {v: k for k, v in cluster2p.items()}

        # --- первая запись карт ---
        best: Dict[str, Tuple[float, str]] = {}
        for (x1, y1, x2, y2, raw, conf), cl in zip(info, labels):
            player = cluster2p[cl]
            label = self._norm_card(raw)
            if label in best and conf <= best[label][0]:
                continue
            if label in best:
                prev = best[label][1]
                self.hands[prev].discard(label)
            self.hands[player].add(label)
            best[label] = (conf, player)
            self._dets.append((x1, y1, x2, y2, raw, player, conf))

        # =========================================================
        #        П О В Т О Р Н А Я   П Р О В Е Р К А
        # =========================================================
        DIST_THRESH = 90.0
        for idx, det in enumerate(self._dets):
            x1, y1, x2, y2, raw, pl, *_ = det
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            dists  = np.linalg.norm(centroids - np.array([cx, cy]), axis=1)
            nearest = int(np.argmin(dists))
            if nearest != pl2cluster[pl] and dists[pl2cluster[pl]] - dists[nearest] > DIST_THRESH:
                # перенос карты к правильной руке
                self.hands[pl].discard(self._norm_card(raw))
                new_pl = cluster2p[nearest]
                self.hands[new_pl].add(self._norm_card(raw))
                self._dets[idx] = (x1, y1, x2, y2, raw, new_pl, det[6])

        # ---------- пост-обработка ----------
        self._filter_by_geometry(img)   # убираем боксы «не по форме»
        self._second_pass_low_conf(img) # докидываем недостающие карты

    def _second_pass_low_conf(self, img: np.ndarray):
        pred2 = self.model.predict(img, imgsz=1600, augment=True,
                                   conf=0.20, verbose=False)[0]
        id2label = self.model.names

        def iou(a, b):
            xA = max(a[0], b[0])
            yA = max(a[1], b[1])
            xB = min(a[2], b[2])
            yB = min(a[3], b[3])
            inter = max(0, xB - xA) * max(0, yB - yA)
            if inter == 0:
                return 0.0
            area1 = (a[2] - a[0]) * (a[3] - a[1])
            area2 = (b[2] - b[0]) * (b[3] - b[1])
            return inter / (area1 + area2 - inter)

        for b in pred2.boxes:
            raw_new = id2label[int(b.cls)]
            card_new = self._norm_card(raw_new)
            x1, y1, x2, y2 = map(int, b.xyxy[0])
            conf_new = float(b.conf.cpu().item())
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            dists = np.linalg.norm(self._cluster_centroids - np.array([cx, cy]), axis=1)
            cl_new = int(np.argmin(dists))
            pl_new = self._cluster2p[cl_new]

            best_iou, idx_best = 0.0, None
            for idx_old, det in enumerate(self._dets):
                iou_val = iou((x1, y1, x2, y2), det[:4])
                if iou_val > best_iou:
                    best_iou, idx_best = iou_val, idx_old

            if best_iou > 0.5:
                x1o, y1o, x2o, y2o, raw_old, pl_old, conf_old = self._dets[idx_best]
                card_old = self._norm_card(raw_old)
                if card_new == card_old:
                    if conf_new > conf_old:
                        self._dets[idx_best] = (x1, y1, x2, y2, raw_new, pl_old, conf_new)
                else:
                    if conf_new > conf_old + 0.2:
                        for p in ORDER:
                            self.hands[p].discard(card_old)
                            self.hands[p].discard(card_new)
                        self.hands[pl_new].add(card_new)
                        self._dets[idx_best] = (x1, y1, x2, y2, raw_new, pl_new, conf_new)
                continue

            if any(card_new in self.hands[p] for p in ORDER):
                continue

            self._dets.append((x1, y1, x2, y2, raw_new, pl_new, conf_new))
            self.hands[pl_new].add(card_new)

    def _filter_by_geometry(self, img: np.ndarray):
        """
        Отбрасываем детекции, у которых bbox не похоже
        на стандартную карту (аспр. вне [0.5..0.8]
        или слишком мала/велика площадь).
        """
        h_img, w_img = img.shape[:2]
        new_dets = []
        for det in self._dets:
            x1, y1, x2, y2 = det[0], det[1], det[2], det[3]
            w, h = x2 - x1, y2 - y1
            ar = w / h if h>0 else 0
            area = w * h
            if 0.5 < ar < 0.8 and 0.01*w_img*w_img < area < 0.1*w_img*w_img:
                new_dets.append(det)
        self._dets = new_dets

        self.hands = {p:set() for p in ORDER}
        for det in self._dets:
            card = self._norm_card(det[4])
            self.hands[det[5]].add(card)

    # ---- helpers ----
    @staticmethod
    def _norm_card(s: str) -> str:
        s = s.upper().replace("10", "T")
        suit = next((c for c in s if c in SUITS), "")
        rank = next((c for c in s if c in RANKS), "")
        if not suit or not rank:
            raise ValueError(f"Неверная карта: {s}")
        return rank + suit

    @staticmethod
    def _norm_player(p: str) -> str:
        p = p.strip().upper()
        if p not in ORDER:
            raise ValueError("Рука должна быть N/E/S/W.")
        return p

    @classmethod
    def from_pbn(cls, pbn: str) -> "BridgeCardDetector":
        """
        Создает BridgeCardDetector из строки PBN.
        Фото и модель не нужны. Детекции не происходят.
        """
        # --- Парсинг дилера и строк ---
        pbn = pbn.strip()
        dealer = None
        if ":" in pbn:
            dealer, deals = pbn.split(":", 1)
            dealer = dealer.strip().upper()
            deals = deals.strip()
        else:
            deals = pbn
            dealer = "N"
        # Разбиваем на руки по пробелу (их должно быть 4)
        hands_str = deals.strip().split()
        if len(hands_str) != 4:
            raise ValueError("PBN должен содержать 4 руки")
        # Порядок по дилеру
        dealer = dealer if dealer in ORDER else "N"
        start = ORDER.index(dealer)
        order = ORDER[start:] + ORDER[:start]
        # Создаём пустой детектор без распознавания
        obj = cls.__new__(cls)
        obj.img_path = None
        obj.model = None
        obj._dealer = dealer
        obj.hands = {p: set() for p in ORDER}
        obj._dets = []
        # Разложим карты по рукам
        for p, hand_str in zip(order, hands_str):
            suits = hand_str.split(".")
            if len(suits) != 4:
                raise ValueError(f"PBN рука должна содержать 4 масти: {hand_str}")
            for s, cards in zip(SUITS, suits):
                for r in cards:
                    if r == "1":  # "10"
                        # Нужно поймать "10"
                        if cards.startswith("10"):
                            r = "T"
                            cards = cards[2:]
                        else:
                            raise ValueError("Неверное обозначение карты 10")
                    if r == "T":
                        obj.hands[p].add("T"+s)
                    elif r in RANKS:
                        obj.hands[p].add(r+s)
        # Проверяем что у каждого <= 13
        for p in ORDER:
            if len(obj.hands[p]) > MAX_HAND_LEN:
                raise ValueError(f"У {p} слишком много карт: {len(obj.hands[p])}")
        return obj


# ──────────── демо ────────────
if __name__ == "__main__":
    # det = BridgeCardDetector.from_pbn("T652.7652.Q6.AKJ 3.3.T97532.Q9853 Q4.AKQ984.AK4.76 AKJ987.JT.J8.T42")
    det = BridgeCardDetector('/Users/nikiteslyuk/Desktop/9.jpeg')

    print(det.preview())

    annotated_output = '/Users/nikiteslyuk/Desktop/9_annotated.jpeg'
    det.visualize(annotated_output, debug=True)
    print(f"Размеченное изображение сохранено в {annotated_output}")