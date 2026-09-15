from __future__ import annotations

import math
from collections import defaultdict


def brier(p: float, y: int) -> float:
    return (p - y) ** 2


def log_loss(p: float, y: int) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def buckets(preds: list[tuple[float, int]]) -> list[dict]:
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, y in preds:
        i = min(9, int(p * 10))
        bins[i].append((p, y))
    out = []
    for i in range(10):
        rows = bins.get(i, [])
        n = len(rows)
        pred = sum(p for p, _ in rows) / n if n else 0.0
        freq = sum(y for _, y in rows) / n if n else 0.0
        out.append(
            {
                "bucket": f"{i*10}-{i*10+10}%",
                "n": n,
                "predicted": pred,
                "actual": freq,
                "calibration_error": abs(pred - freq) if n else 0.0,
            }
        )
    return out
