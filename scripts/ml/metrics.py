# scripts/ml/metrics_classification.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import List, Dict, Tuple, Optional
import math


# =========================
# Confusion + базовые метрики
# =========================
def confusion_binary(y_true: List[int], y_pred: List[int]) -> Tuple[int, int, int, int]:
    """
    Returns: TP, FP, FN, TN
    """
    TP = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    TN = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 0)
    FP = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    FN = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)
    return TP, FP, FN, TN


def mcc(TP: int, FP: int, FN: int, TN: int) -> float:
    num = TP * TN - FP * FN
    den = math.sqrt((TP + FP) * (TP + FN) * (TN + FP) * (TN + FN) + 1e-12)
    return float(num / den)


def metrics_from_confusion(TP: int, FP: int, FN: int, TN: int) -> Dict[str, float]:
    P = TP / (TP + FP + 1e-12)
    R = TP / (TP + FN + 1e-12)
    F1 = 2 * P * R / (P + R + 1e-12)
    Acc = (TP + TN) / max(1, TP + TN + FP + FN)
    TPR = TP / (TP + FN + 1e-12)
    TNR = TN / (TN + FP + 1e-12)
    BalAcc = 0.5 * (TPR + TNR)
    MCC = mcc(TP, FP, FN, TN)

    return {
        "acc": float(Acc),
        "bal_acc": float(BalAcc),
        "precision": float(P),
        "recall": float(R),
        "f1": float(F1),
        "mcc": float(MCC),
        "tpr": float(TPR),
        "tnr": float(TNR),
    }


def _rankdata_average_ties(values: List[float]) -> List[float]:
    """
    Средние ранги при tie-значениях.
    Возвращает ранги 1..N.
    """
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n

    i = 0
    r = 1
    while i < n:
        j = i
        v = values[order[i]]
        while j < n and values[order[j]] == v:
            j += 1
        # i..j-1 одинаковые -> средний ранг
        avg_rank = (r + (r + (j - i) - 1)) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        r += (j - i)
        i = j

    return ranks


def roc_auc_score_binary(y_true: List[int], scores: List[float]) -> Optional[float]:
    """
    ROC-AUC через ранги (эквивалент Mann–Whitney U).
    Возвращает None, если в y_true только один класс.
    """
    if len(y_true) != len(scores) or not y_true:
        return None
    n_pos = sum(1 for t in y_true if t == 1)
    n_neg = len(y_true) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None

    ranks = _rankdata_average_ties(scores)
    sum_ranks_pos = sum(r for r, t in zip(ranks, y_true) if t == 1)

    # U = sum_ranks_pos - n_pos*(n_pos+1)/2
    U = sum_ranks_pos - n_pos * (n_pos + 1) / 2.0
    auc = U / (n_pos * n_neg)
    return float(auc)


def pr_auc_score_binary(y_true: List[int], scores: List[float]) -> Optional[float]:
    """
    PR-AUC (Average Precision / площадь под PR кривой step-wise).
    Возвращает None если нет positive.
    """
    if len(y_true) != len(scores) or not y_true:
        return None
    n_pos = sum(1 for t in y_true if t == 1)
    if n_pos == 0:
        return None

    # сортируем по score убыванию
    pairs = sorted(zip(scores, y_true), key=lambda x: x[0], reverse=True)

    tp = 0
    fp = 0
    prev_recall = 0.0
    ap = 0.0

    # step-wise интеграл: AP = sum (recall_i - recall_{i-1}) * precision_i
    for _s, t in pairs:
        if t == 1:
            tp += 1
        else:
            fp += 1
        precision = tp / (tp + fp + 1e-12)
        recall = tp / (n_pos + 1e-12)
        if t == 1:
            ap += (recall - prev_recall) * precision
            prev_recall = recall

    return float(ap)


def auc_metrics(y_true: List[int], scores: List[float]) -> Dict[str, Optional[float]]:
    return {
        "roc_auc": roc_auc_score_binary(y_true, scores),
        "pr_auc": pr_auc_score_binary(y_true, scores),
    }
