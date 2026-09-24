"""Benchmark HP-bar scanner implementations against saved frame annotations."""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

import cv2

from scripts.host.vision.screen_hp_scanner import (
    HpBarBox,
    hp_bar_annotations,
    scan_hp_bars_on_screen,
    scan_hp_bars_on_screen_contours,
    scan_hp_bars_on_screen_contours_all,
    scan_hp_bars_on_screen_contours_hybrid,
)


Scanner = Callable[[object], dict]
SCANNERS: dict[str, Scanner] = {
    "baseline_labels": scan_hp_bars_on_screen,
    "experimental_contours": scan_hp_bars_on_screen_contours,
    "experimental_contours_hybrid": scan_hp_bars_on_screen_contours_hybrid,
    "experimental_contours_all": scan_hp_bars_on_screen_contours_all,
}


def _iou(a: list[int], b: list[int]) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, right - left + 1) * max(0, bottom - top + 1)
    area_a = max(0, a[2] - a[0] + 1) * max(0, a[3] - a[1] + 1)
    area_b = max(0, b[2] - b[0] + 1) * max(0, b[3] - b[1] + 1)
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def match_annotations(expected: list[dict], predicted: list[dict], iou_threshold: float) -> dict:
    """Greedily match same-class boxes and return detection/count metrics."""
    expected_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    predicted_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in expected:
        expected_by_key[item["category"], item["team"]].append(item)
    for item in predicted:
        predicted_by_key[item["category"], item["team"]].append(item)

    totals = {"tp": 0, "fp": 0, "fn": 0, "hp_error_sum": 0.0, "hp_matches": 0}
    per_class = {}
    for key in sorted(set(expected_by_key) | set(predicted_by_key)):
        refs, preds = expected_by_key[key], predicted_by_key[key]
        candidates = []
        for ref_index, ref in enumerate(refs):
            for pred_index, pred in enumerate(preds):
                overlap = _iou(ref["bbox_xyxy"], pred["bbox_xyxy"])
                if overlap >= iou_threshold:
                    candidates.append((overlap, ref_index, pred_index))
        used_refs, used_preds = set(), set()
        hp_error = 0.0
        for _, ref_index, pred_index in sorted(candidates, reverse=True):
            if ref_index in used_refs or pred_index in used_preds:
                continue
            used_refs.add(ref_index)
            used_preds.add(pred_index)
            hp_error += abs(refs[ref_index]["hp_ratio"] - preds[pred_index]["hp_ratio"])
        tp, fp, fn = len(used_refs), len(preds) - len(used_preds), len(refs) - len(used_refs)
        name = f"{key[0]}.{key[1]}"
        per_class[name] = {"tp": tp, "fp": fp, "fn": fn}
        totals["tp"] += tp
        totals["fp"] += fp
        totals["fn"] += fn
        totals["hp_error_sum"] += hp_error
        totals["hp_matches"] += tp
    return {**totals, "per_class": per_class}


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def benchmark(dataset_dir: Path, *, repeats: int = 1, limit: int = 0,
              iou_threshold: float = 0.5) -> dict:
    annotation_paths = sorted((dataset_dir / "annotations").glob("*.json"))
    if limit:
        annotation_paths = annotation_paths[:limit]
    if not annotation_paths:
        raise FileNotFoundError(f"No annotation JSON files in {dataset_dir / 'annotations'}")

    results = {
        name: {
            "times_ms": [], "tp": 0, "fp": 0, "fn": 0,
            "hp_error_sum": 0.0, "hp_matches": 0,
            "predicted_count": 0, "expected_count": 0,
            "frames_more": 0, "frames_equal": 0, "frames_less": 0,
            "frame_differences": [],
            "per_class": defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0}),
        }
        for name in SCANNERS
    }

    first_meta = json.loads(annotation_paths[0].read_text(encoding="utf-8"))
    first_frame = cv2.imread(str(dataset_dir / "raw" / first_meta["image"]), cv2.IMREAD_COLOR)
    if first_frame is None:
        raise FileNotFoundError(first_meta["image"])
    first_rgb = cv2.cvtColor(first_frame, cv2.COLOR_BGR2RGB)
    for scanner in SCANNERS.values():
        scanner(first_rgb)  # warm up OpenCV allocations before measurements

    scanner_items = list(SCANNERS.items())
    for frame_index, annotation_path in enumerate(annotation_paths):
        meta = json.loads(annotation_path.read_text(encoding="utf-8"))
        frame_bgr = cv2.imread(str(dataset_dir / "raw" / meta["image"]), cv2.IMREAD_COLOR)
        if frame_bgr is None:
            raise FileNotFoundError(meta["image"])
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        expected = meta.get("annotations", [])

        # Rotate execution order so the same implementation does not always
        # receive the hottest CPU/cache state.
        offset = frame_index % len(scanner_items)
        ordered_scanners = scanner_items[offset:] + scanner_items[:offset]
        for name, scanner in ordered_scanners:
            durations, scan = [], None
            for _ in range(repeats):
                started = time.perf_counter_ns()
                scan = scanner(frame_rgb)
                durations.append((time.perf_counter_ns() - started) / 1_000_000.0)
            predicted = hp_bar_annotations(scan)
            metrics = match_annotations(expected, predicted, iou_threshold)
            result = results[name]
            result["times_ms"].append(statistics.median(durations))
            for field in ("tp", "fp", "fn", "hp_error_sum", "hp_matches"):
                result[field] += metrics[field]
            result["predicted_count"] += len(predicted)
            result["expected_count"] += len(expected)
            relation = "equal" if len(predicted) == len(expected) else "more" if len(predicted) > len(expected) else "less"
            result[f"frames_{relation}"] += 1
            if metrics["fp"] or metrics["fn"] or relation != "equal":
                result["frame_differences"].append({
                    "image": meta["image"],
                    "expected": len(expected), "predicted": len(predicted),
                    "count_delta": len(predicted) - len(expected),
                    "fp": metrics["fp"], "fn": metrics["fn"],
                    "per_class": metrics["per_class"],
                })
            for key, values in metrics["per_class"].items():
                for field in ("tp", "fp", "fn"):
                    result["per_class"][key][field] += values[field]

    report = {"dataset": str(dataset_dir), "frames": len(annotation_paths),
              "repeats": repeats, "iou_threshold": iou_threshold, "variants": {}}
    for name, raw in results.items():
        tp, fp, fn = raw["tp"], raw["fp"], raw["fn"]
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        times = raw.pop("times_ms")
        raw["per_class"] = dict(raw["per_class"])
        raw.update({
            "mean_ms": statistics.fmean(times),
            "median_ms": statistics.median(times),
            "p95_ms": _percentile(times, .95),
            "fps": 1000.0 / statistics.fmean(times),
            "precision": precision, "recall": recall, "f1": f1,
            "hp_mae": raw["hp_error_sum"] / raw["hp_matches"] if raw["hp_matches"] else 0.0,
            "count_delta": raw["predicted_count"] - raw["expected_count"],
            "mean_count_delta_per_frame": (
                raw["predicted_count"] - raw["expected_count"]
            ) / len(annotation_paths),
        })
        report["variants"][name] = raw

    baseline = report["variants"]["baseline_labels"]
    for name, variant in report["variants"].items():
        variant["versus_baseline"] = {
            "mean_ms_delta": variant["mean_ms"] - baseline["mean_ms"],
            "speedup": baseline["mean_ms"] / variant["mean_ms"],
            "f1_delta": variant["f1"] - baseline["f1"],
            "count_delta_difference": variant["count_delta"] - baseline["count_delta"],
        }
    return report


def print_report(report: dict) -> None:
    print(f"frames={report['frames']} repeats={report['repeats']} IoU>={report['iou_threshold']}")
    print("variant                      mean    p95     fps      P       R      F1   count_d  more/equal/less")
    for name, result in report["variants"].items():
        print(
            f"{name:27} {result['mean_ms']:7.2f} {result['p95_ms']:7.2f} "
            f"{result['fps']:7.1f} {result['precision']:7.4f} {result['recall']:7.4f} "
            f"{result['f1']:7.4f} {result['count_delta']:+7d}  "
            f"{result['frames_more']}/{result['frames_equal']}/{result['frames_less']}"
        )
    for name, result in report["variants"].items():
        if name == "baseline_labels":
            continue
        comparison = result["versus_baseline"]
        print(
            f"{name} vs baseline: speedup={comparison['speedup']:.3f}x, "
            f"mean_delta={comparison['mean_ms_delta']:+.2f}ms, "
            f"F1_delta={comparison['f1_delta']:+.4f}, "
            f"count_delta_difference={comparison['count_delta_difference']:+d}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("runs/hp-scanner-captures"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--iou", type=float, default=.5)
    parser.add_argument("--json-output", type=Path, default=None)
    args = parser.parse_args()
    if args.repeats < 1 or args.limit < 0 or not 0 < args.iou <= 1:
        parser.error("repeats >= 1, limit >= 0 and 0 < iou <= 1 are required")
    report = benchmark(args.dataset, repeats=args.repeats, limit=args.limit, iou_threshold=args.iou)
    print_report(report)
    output = args.json_output or args.dataset / "benchmark-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
