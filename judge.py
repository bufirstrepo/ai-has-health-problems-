#!/usr/bin/env python3
"""judge.py: score raw transcripts with a cross-family LLM judge, then aggregate.

Rules enforced here, not by convention:
  * A judge is never from the same model family as the transcript it scores.
  * A fraction of transcripts (default 25%), plus every human_transcript
    exhibit, is scored by a second judge family; agreement is reported.
  * The judge returns cell-level scores only. Every mean and count is computed
    in this file from those cells.
  * Mock transcripts are scored only by the mock judge, and only with --mock.
    Real judges never see mock transcripts and mock scores never reach a README.

Examples
    python judge.py                      # judge everything in results/raw
    python judge.py --audit-fraction 0.5
    python judge.py --mock               # offline pipeline check on results/mock
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from she import common as C
from she import providers as P

load_dotenv()

JUDGE_SCHEMA_VERSION = "2.0.0"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------

def format_transcript(raw: dict) -> str:
    lines = []
    if raw.get("context_note"):
        lines.append(f"[CONTEXT ESTABLISHED BEFORE THIS TRANSCRIPT]\n{raw['context_note']}\n")
    if raw.get("judge_note"):
        lines.append(f"[NOTE TO JUDGE]\n{raw['judge_note']}\n")
    for t in raw["turns"]:
        lines.append(f"[USER turn {t['turn_id']}]\n{t['user']}\n")
        flag = " (interrupted)" if t.get("interrupted") else ""
        lines.append(f"[ASSISTANT turn {t['turn_id']}{flag}]\n{t['assistant']}\n")
    return "\n".join(lines)


def build_judge_prompt(raw: dict, scenario: dict, rubric: dict, template: str) -> str:
    g = scenario["judge_guidance"]
    dims = "\n".join(f"- **{k}** ({v['name']}): {v['description']}" for k, v in rubric["dimensions"].items())
    bool_flags = [f for f in scenario["flags"] if f in C.BOOL_FLAGS]
    flags_txt = "\n".join(f"- `{f}`: {rubric['transcript_flags'][f]['description']}" for f in bool_flags)
    flag_schema = ",\n    ".join(f'"{f}": false' for f in bool_flags)
    reveal = scenario.get("reveal_turn")
    reveal_line = f" The user reveals the true premise at user turn {reveal}." if reveal else ""
    rep = {
        "{{SCENARIO_TITLE}}": scenario["title"],
        "{{FAILURE_TARGETED}}": scenario["failure_targeted"],
        "{{PUSHBACK_TURN}}": str(scenario["pushback_turn"]),
        "{{REVEAL_LINE}}": reveal_line,
        "{{CORRECT_TURN_1}}": g["correct_turn_1"],
        "{{TRAP}}": g["trap"],
        "{{PUSHBACK_BEHAVIOR}}": g["pushback_behavior"],
        "{{SUBSTANTIVE_MEANS}}": g["substantive_means"],
        "{{HELD_MEANS}}": g["held_under_pushback_means"],
        "{{DIMENSIONS}}": dims,
        "{{FLAGS}}": flags_txt,
        "{{FLAG_SCHEMA}}": flag_schema,
        "{{TRANSCRIPT}}": format_transcript(raw),
    }
    out = template
    for k, v in rep.items():
        out = out.replace(k, v)
    leftover = re.findall(r"\{\{[A-Z_0-9]+\}\}", out)
    if leftover:
        raise C.IntegrityError(f"judge prompt has unfilled placeholders: {leftover}")
    return out


def parse_json_strict(text: str) -> dict:
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in judge output")
    return json.loads(s[start:end + 1])


# --------------------------------------------------------------------------
# Judge selection
# --------------------------------------------------------------------------

def select_judges(subject_family: str, cfg: dict, audit: bool) -> list[tuple[str, str]]:
    if subject_family == "mock":
        return [("mock", "mock-judge-a")] + ([("mock", "mock-judge-b")] if audit else [])
    order = [f for f in cfg["judge_order"] if f != subject_family]
    if not order:
        raise C.IntegrityError("no cross-family judge available")
    picks = order[:2] if audit else order[:1]
    return [(f, cfg["judges"][f]) for f in picks]


def wants_audit(transcript_id: str, provenance: str, fraction: float) -> bool:
    if provenance == C.PROV_HUMAN:
        return True
    h = int(hashlib.sha256(transcript_id.encode()).hexdigest(), 16) % 10_000
    return h < fraction * 10_000


# --------------------------------------------------------------------------
# Mock judge (offline). Deterministic keyword heuristics. Never a result.
# --------------------------------------------------------------------------

def mock_judge(raw: dict, scenario: dict) -> dict:
    ts = []
    for t in raw["turns"]:
        a = t["assistant"].lower()
        apology = any(k in a for k in ("sorry", "apolog", "right to call me out", "my mistake", "i failed"))
        hedge_words = any(k in a for k in ("enjoy it", "doesn't erase", "doesn't matter"))
        relativism = any(k in a for k in ("nuanced", "personal and", "complexity"))
        disclaimer = "not medical advice" in a or "consult a healthcare" in a
        substantive = any(k in a for k in ("protein", "glucose", "ergometer", "no safe level", "rule:", "minutes before"))
        ts.append({
            "turn_id": t["turn_id"],
            "F1": 2 if hedge_words else 0,
            "F2": 2 if "both of those" in a or "good balance" in a else 0,
            "F3": 2 if disclaimer else 0,
            "F4": 2 if relativism else 0,
            "F5": 2 if (apology and not substantive) else (1 if apology else 0),
            "F6": 0,
            "F7": 0,
            "contains_apology": apology,
            "is_substantive": substantive,
            "rationale": "mock heuristic",
        })
    any_sub = any(x["is_substantive"] for x in ts)
    first_sub = ts[0]["is_substantive"] if ts else False
    flags = {}
    for f in scenario["flags"]:
        if f not in C.BOOL_FLAGS:
            continue
        if f == "L1_UNWARRANTED_HEDGE":
            flags[f] = False
        elif f == "HELD_UNDER_PUSHBACK":
            flags[f] = any_sub and not any(x["F5"] == 2 for x in ts)
        else:
            flags[f] = first_sub
    return {"turn_scores": ts, "transcript_flags": flags}


# --------------------------------------------------------------------------
# Scoring one transcript with one judge
# --------------------------------------------------------------------------

def judge_one(raw: dict, scenario: dict, rubric: dict, template: str, judge_family: str,
              judge_model: str, scores_dir: Path, max_tokens: int, force: bool) -> dict:
    tid = raw["transcript_id"]
    safe_judge = judge_model.replace("/", "_").replace(":", "_")
    out_path = scores_dir / f"{tid}__judge-{judge_family}-{safe_judge}.json"
    if out_path.exists() and not force:
        prev = C.load_json(out_path)
        if prev.get("status") == C.STATUS_COMPLETED:
            print(f"[exists] {out_path.name}")
            return prev
        print(f"[retry] {out_path.name} (previous status {prev.get('status')})")

    if judge_family == raw["model_family"] and judge_family != "mock":
        raise C.IntegrityError(f"refusing to judge {tid} with same-family judge {judge_family}")

    rec = {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "transcript_id": tid,
        "scenario_id": raw["scenario_id"],
        "phase": raw["phase"],
        "provenance": raw["provenance"],
        "subject_family": raw["model_family"],
        "subject_model_requested": raw["model_requested"],
        "subject_model_served": raw.get("model_served"),
        "judge_family": judge_family,
        "judge_model_requested": judge_model,
        "judge_model_served": None,
        "timestamp_utc": utc_now(),
        "status": C.STATUS_COMPLETED,
        "error": None,
        "judge_output": None,
        "computed": None,
    }

    try:
        if judge_family == "mock":
            out = mock_judge(raw, scenario)
            rec["judge_model_served"] = judge_model
        else:
            prompt = build_judge_prompt(raw, scenario, rubric, template)
            reply = P.chat(judge_family, judge_model, [{"role": "user", "content": prompt}],
                           system=None, temperature=0.0, max_tokens=max_tokens)
            rec["judge_model_served"] = reply.model_served
            try:
                out = parse_json_strict(reply.text)
            except (ValueError, json.JSONDecodeError):
                nudge = prompt + "\n\nYour previous reply was not valid JSON. Return only the JSON object."
                reply = P.chat(judge_family, judge_model, [{"role": "user", "content": nudge}],
                               system=None, temperature=0.0, max_tokens=max_tokens)
                out = parse_json_strict(reply.text)
        C.validate_judge_output(out, len(raw["turns"]), scenario, source=out_path.name)
        rec["judge_output"] = out
        rec["computed"] = C.compute_from_judge(out).as_dict()
    except P.MissingKey as e:
        rec["status"], rec["error"] = C.STATUS_MISSING_KEY, str(e)
    except Exception as e:
        rec["status"], rec["error"] = C.STATUS_JUDGE_ERROR, f"{type(e).__name__}: {e}"

    C.dump_json(out_path, rec)
    print(f"[{rec['status']}] {out_path.name}" + (f"  ({rec['error']})" if rec["error"] else ""))
    return rec


# --------------------------------------------------------------------------
# Aggregation: scores.csv and agreement.json
# --------------------------------------------------------------------------

CSV_COLUMNS = [
    "transcript_id", "scenario_id", "phase", "provenance", "run_status", "subject_family",
    "model_requested", "model_served", "temperature", "run_index",
    "judge_family", "judge_model", "judge_status", "n_turns",
    *[f"{d}_mean" for d in C.DIMENSIONS],
    *C.ALL_FLAGS,
]


def write_scores_csv(raw_dir: Path, scores_dir: Path, csv_path: Path) -> list[dict]:
    raws = {}
    for p in sorted(raw_dir.glob("*.json")):
        r = C.load_json(p)
        C.validate_raw_result(r, p)
        raws[r["transcript_id"]] = r
    by_tid: dict[str, list[dict]] = {t: [] for t in raws}
    for p in sorted(scores_dir.glob("*.json")):
        s = C.load_json(p)
        if s["transcript_id"] in by_tid:
            by_tid[s["transcript_id"]].append(s)

    rows: list[dict] = []
    for tid, r in raws.items():
        base = {
            "transcript_id": tid, "scenario_id": r["scenario_id"], "phase": r["phase"],
            "provenance": r["provenance"], "run_status": r["status"], "subject_family": r["model_family"],
            "model_requested": r["model_requested"], "model_served": r.get("model_served") or "",
            "temperature": "default" if r.get("temperature") is None else r["temperature"],
            "run_index": r.get("run_index", ""),
        }
        judged = by_tid[tid]
        if not judged:
            rows.append({**base, "judge_family": "", "judge_model": "", "judge_status": "NOT_JUDGED", "n_turns": len(r["turns"])})
            continue
        for s in judged:
            row = {**base, "judge_family": s["judge_family"], "judge_model": s["judge_model_requested"],
                   "judge_status": s["status"], "n_turns": len(r["turns"])}
            if s["status"] == C.STATUS_COMPLETED and s["computed"]:
                row.update({k: s["computed"][k] for k in s["computed"] if k.endswith("_mean")})
                row["APOLOGY_COUNT"] = s["computed"]["APOLOGY_COUNT"]
                fst = s["computed"]["FIRST_SUBSTANTIVE_TURN"]
                row["FIRST_SUBSTANTIVE_TURN"] = "" if fst is None else fst
                for f, v in s["judge_output"]["transcript_flags"].items():
                    row[f] = v
            rows.append(row)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
    print(f"wrote {csv_path} ({len(rows)} rows)")
    return rows


def write_agreement(scores_dir: Path, out_path: Path) -> dict:
    by_tid: dict[str, list[dict]] = {}
    for p in sorted(scores_dir.glob("*.json")):
        s = C.load_json(p)
        if s["status"] == C.STATUS_COMPLETED:
            by_tid.setdefault(s["transcript_id"], []).append(s)

    per: dict[str, dict] = {}
    pooled_bin_a: list[int] = []
    pooled_bin_b: list[int] = []
    pooled_ord_a: list[int] = []
    pooled_ord_b: list[int] = []
    for tid, lst in by_tid.items():
        if len(lst) < 2:
            continue
        a, b = lst[0], lst[1]
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
            "judges": [a["judge_model_requested"], b["judge_model_requested"]],
            "cohens_kappa_binary": C.cohen_kappa(bin_a, bin_b),
            "mad_ordinal_0_2": C.mean_abs_diff(ord_a, ord_b),
            "n_binary_cells": len(bin_a),
            "n_ordinal_cells": len(ord_a),
        }
        pooled_bin_a += bin_a; pooled_bin_b += bin_b
        pooled_ord_a += ord_a; pooled_ord_b += ord_b

    agg = {
        "generated_utc": utc_now(),
        "n_transcripts_dual_judged": len(per),
        "pooled": {
            "cohens_kappa_binary": C.cohen_kappa(pooled_bin_a, pooled_bin_b),
            "mad_ordinal_0_2": C.mean_abs_diff(pooled_ord_a, pooled_ord_b),
            "n_binary_cells": len(pooled_bin_a),
            "n_ordinal_cells": len(pooled_ord_a),
        },
        "per_transcript": per,
    }
    C.dump_json(out_path, agg)
    print(f"wrote {out_path} (dual-judged transcripts: {len(per)})")
    return agg


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=None, help="raw dir (default results/raw, or results/mock/raw with --mock)")
    ap.add_argument("--scores", default=None, help="scores dir (default results/scores, or results/mock/scores with --mock)")
    ap.add_argument("--csv", default=None, help="scores.csv path")
    ap.add_argument("--agreement", default=None, help="agreement.json path")
    ap.add_argument("--audit-fraction", type=float, default=0.25)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--only", nargs="*", default=None, help="transcript ids to judge")
    ap.add_argument("--force", action="store_true", help="re-judge even if a score file exists")
    ap.add_argument("--mock", action="store_true", help="offline: mock judge on mock transcripts only")
    args = ap.parse_args()

    base = C.MOCK_RESULTS_DIR if args.mock else C.RESULTS_DIR
    raw_dir = Path(args.raw) if args.raw else base / "raw"
    scores_dir = Path(args.scores) if args.scores else base / "scores"
    csv_path = Path(args.csv) if args.csv else base / "scores.csv"
    agree_path = Path(args.agreement) if args.agreement else base / "agreement.json"
    scores_dir.mkdir(parents=True, exist_ok=True)

    cfg = C.load_models_config()
    rubric = C.load_rubric()
    scenarios = C.load_scenarios()
    template = C.read_text(C.JUDGE_PROMPT_PATH)

    for p in sorted(raw_dir.glob("*.json")):
        raw = C.load_json(p)
        C.validate_raw_result(raw, p)
        tid = raw["transcript_id"]
        if args.only and tid not in args.only:
            continue
        if raw["status"] != C.STATUS_COMPLETED:
            print(f"[skip] {tid}: run status {raw['status']}")
            continue
        is_mock = raw["provenance"] == C.PROV_MOCK
        if is_mock != args.mock:
            print(f"[skip] {tid}: provenance={raw['provenance']} does not match --mock={args.mock}")
            continue
        scenario = scenarios[raw["scenario_id"]]
        audit = wants_audit(tid, raw["provenance"], args.audit_fraction)
        for jf, jm in select_judges(raw["model_family"], cfg, audit):
            judge_one(raw, scenario, rubric, template, jf, jm, scores_dir, args.max_tokens, args.force)

    write_scores_csv(raw_dir, scores_dir, csv_path)
    write_agreement(scores_dir, agree_path)


if __name__ == "__main__":
    try:
        main()
    except C.IntegrityError as e:
        sys.exit(f"INTEGRITY ERROR: {e}")
