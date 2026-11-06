"""Aggregation from the JSON records on disk.

`results/scores.csv` is a derived artifact. Nothing reads it as an input; both
judge.py and build_readme.py call `collect()` here, which re-reads every raw
transcript and every judge record, re-validates each (including recomputing the
judge record's stored means from its cell-level output), and only then produces
rows. A hand-edited CSV therefore changes nothing. A planted judge record from
the subject's own family, or a mock record relabeled as api_run, raises
IntegrityError at collection.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from . import common as C

CSV_COLUMNS = [
    "transcript_id", "scenario_id", "phase", "provenance", "run_status", "subject_family",
    "model_requested", "model_served", "temperature", "run_index",
    "judge_family", "judge_model", "judge_status", "n_turns",
    *[f"{d}_mean" for d in C.DIMENSIONS],
    *C.ALL_FLAGS,
]


@dataclass
class Collected:
    raws: dict[str, dict]
    scores: dict[str, list[dict]]  # transcript_id -> judge records (validated)
    rows: list[dict]


def collect(raw_dir: Path, scores_dir: Path) -> Collected:
    scenarios = C.load_scenarios()
    raws: dict[str, dict] = {}
    for p in sorted(raw_dir.glob("*.json")):
        r = C.load_json(p)
        C.validate_raw_result(r, p)
        if r["transcript_id"] in raws:
            raise C.IntegrityError(f"{p}: duplicate transcript_id {r['transcript_id']!r}")
        if r["scenario_id"] not in scenarios:
            raise C.IntegrityError(f"{p}: unknown scenario {r['scenario_id']!r}")
        raws[r["transcript_id"]] = r

    scores: dict[str, list[dict]] = {t: [] for t in raws}
    if scores_dir.exists():
        for p in sorted(scores_dir.glob("*.json")):
            s = C.load_json(p)
            tid = s.get("transcript_id")
            if tid not in raws:
                raise C.IntegrityError(f"{p}: judge record for unknown transcript {tid!r}; no raw file backs it")
            raw = raws[tid]
            C.validate_score_record(s, raw, scenarios[raw["scenario_id"]], p)
            for prev in scores[tid]:
                same_family = prev["judge_family"] == s["judge_family"]
                same_model = prev["judge_model_requested"] == s["judge_model_requested"]
                if same_family and (s["judge_family"] != "mock" or same_model):
                    raise C.IntegrityError(f"{p}: two judge records from family {s['judge_family']!r} for {tid}; "
                                           "a second judge must be a different family")
            scores[tid].append(s)

    rows: list[dict] = []
    for tid, r in raws.items():
        base = {
            "transcript_id": tid, "scenario_id": r["scenario_id"], "phase": r["phase"],
            "provenance": r["provenance"], "run_status": r["status"], "subject_family": r["model_family"],
            "model_requested": r["model_requested"], "model_served": r.get("model_served") or "",
            "temperature": "default" if r.get("temperature") is None else r["temperature"],
            "run_index": r.get("run_index", ""), "n_turns": len(r["turns"]),
        }
        judged = scores[tid]
        if not judged:
            rows.append({**base, "judge_family": "", "judge_model": "", "judge_status": "NOT_JUDGED"})
            continue
        for s in judged:
            row = {**base, "judge_family": s["judge_family"], "judge_model": s["judge_model_requested"],
                   "judge_status": s["status"]}
            if s["status"] == C.STATUS_COMPLETED:
                comp = C.compute_from_judge(s["judge_output"]).as_dict()  # recomputed, not read back
                row.update({k: comp[k] for k in comp if k.endswith("_mean")})
                row["APOLOGY_COUNT"] = comp["APOLOGY_COUNT"]
                row["FIRST_SUBSTANTIVE_TURN"] = "" if comp["FIRST_SUBSTANTIVE_TURN"] is None else comp["FIRST_SUBSTANTIVE_TURN"]
                for f, v in s["judge_output"]["transcript_flags"].items():
                    row[f] = v
            rows.append(row)
    return Collected(raws=raws, scores=scores, rows=rows)


def write_csv(rows: list[dict], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in CSV_COLUMNS})


def agreement(scores: dict[str, list[dict]]) -> dict:
    per: dict[str, dict] = {}
    pooled_bin_a: list[int] = []
    pooled_bin_b: list[int] = []
    pooled_ord_a: list[int] = []
    pooled_ord_b: list[int] = []
    for tid, lst in scores.items():
        done = [s for s in lst if s["status"] == C.STATUS_COMPLETED]
        if len(done) < 2:
            continue
        a, b = done[0], done[1]
        if a["judge_family"] == b["judge_family"] and a["judge_family"] != "mock":
            raise C.IntegrityError(f"{tid}: agreement requires two different judge families, got {a['judge_family']!r} twice")
        ta, tb = a["judge_output"]["turn_scores"], b["judge_output"]["turn_scores"]
        bin_a, bin_b, ord_a, ord_b = [], [], [], []
        for x, y in zip(ta, tb):
            for k in ("contains_apology", "is_substantive"):
                bin_a.append(int(x[k])); bin_b.append(int(y[k]))
            for d in C.DIMENSIONS:
                ord_a.append(x[d]); ord_b.append(y[d])
        fa, fb = a["judge_output"]["transcript_flags"], b["judge_output"]["transcript_flags"]
        for k in fa:
            if k in fb:
                bin_a.append(int(fa[k])); bin_b.append(int(fb[k]))
        per[tid] = {
            "judges": [f"{a['judge_family']}:{a['judge_model_requested']}", f"{b['judge_family']}:{b['judge_model_requested']}"],
            "cohens_kappa_binary": C.cohen_kappa(bin_a, bin_b),
            "mad_ordinal_0_2": C.mean_abs_diff(ord_a, ord_b),
            "n_binary_cells": len(bin_a),
            "n_ordinal_cells": len(ord_a),
        }
        pooled_bin_a += bin_a; pooled_bin_b += bin_b
        pooled_ord_a += ord_a; pooled_ord_b += ord_b
    return {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "n_transcripts_dual_judged": len(per),
        "pooled": {
            "cohens_kappa_binary": C.cohen_kappa(pooled_bin_a, pooled_bin_b),
            "mad_ordinal_0_2": C.mean_abs_diff(pooled_ord_a, pooled_ord_b),
            "n_binary_cells": len(pooled_bin_a),
            "n_ordinal_cells": len(pooled_ord_a),
        },
        "per_transcript": per,
    }
