#!/usr/bin/env python3
"""build_readme.py: regenerate README.md and results/summary.md from results/.

The generator refuses to print a number that does not carry an accepted
provenance (api_run or human_transcript). Mock rows are excluded. A
human_transcript row in Phase 2 is an integrity error, not a table cell.
When there are no API results, the README says so in plain words and prints
the commands that would produce them. An empty table is the expected default.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from she import common as C
from she import aggregate as A

HEADLINE_FLAGS = {
    "s0_control_gym": ["L1_UNWARRANTED_HEDGE"],
    "s1_pizza": ["T1_UNPROMPTED_COST", "T2_T3_CONSISTENCY"],
    "s2_false_premise": ["P1_PREMISE_VERIFIED"],
    "s3_wheelchair": ["A1_ADAPTED_NOT_LOWERED"],
    "s4_cigarettes": ["T1_UNPROMPTED_COST", "C2_BOTH_HALVES"],
}


def _to_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if v in ("True", "true", "1"):
        return True
    if v in ("False", "false", "0"):
        return False
    return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows(raw_dir: Path, scores_dir: Path) -> list[dict]:
    """Rows are re-derived from results/raw and results/scores every time. The CSV is an output."""
    col = A.collect(raw_dir, scores_dir)
    for r in col.rows:
        if r["provenance"] not in C.PROVENANCES:
            raise C.IntegrityError(f"{r['transcript_id']}: no accepted provenance; refusing to print it")
        if r["phase"] == C.PHASE_EHD and r["provenance"] not in C.PHASE2_PROVENANCES:
            raise C.IntegrityError(f"{r['transcript_id']}: {r['provenance']!r} row in {C.PHASE_EHD}. "
                                   "Exhibits never enter an intervention column.")
    return col.rows


def judged(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["run_status"] == C.STATUS_COMPLETED and r["judge_status"] == C.STATUS_COMPLETED]


def per_transcript(rows: list[dict]) -> dict[str, dict]:
    """Average judged rows across judges for the same transcript."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["transcript_id"]].append(r)
    out = {}
    for tid, lst in groups.items():
        agg = {k: lst[0][k] for k in ("transcript_id", "scenario_id", "phase", "provenance", "model_requested", "model_served", "subject_family")}
        for d in C.DIMENSIONS:
            vals = [v for v in (_to_float(r.get(f"{d}_mean")) for r in lst) if v is not None]
            agg[f"{d}_mean"] = statistics.fmean(vals) if vals else None
        for f in C.BOOL_FLAGS:
            vals = [v for v in (_to_bool(r.get(f)) for r in lst) if v is not None]
            agg[f] = (sum(vals) / len(vals)) if vals else None  # fraction of judges saying true
        fst = [v for v in (_to_float(r.get("FIRST_SUBSTANTIVE_TURN")) for r in lst) if v is not None]
        agg["FIRST_SUBSTANTIVE_TURN"] = statistics.median(fst) if fst else None
        ap = [v for v in (_to_float(r.get("APOLOGY_COUNT")) for r in lst) if v is not None]
        agg["APOLOGY_COUNT"] = statistics.fmean(ap) if ap else None
        agg["n_judges"] = len(lst)
        out[tid] = agg
    return out


def fmt(v, kind="num") -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{100 * v:.0f}%"
    if kind == "int":
        return f"{v:.0f}"
    return f"{v:.2f}"


def scenario_table(sid: str, rows_all: list[dict], pt: dict[str, dict]) -> str:
    """One table for one scenario, rows = (phase, model)."""
    flags = HEADLINE_FLAGS.get(sid, [])
    header = ["Phase", "Model (requested)", "Served as", "n judged / attempted",
              *C.DIMENSIONS, *[f"`{f}`" for f in flags], "`HELD_UNDER_PUSHBACK`", "First substantive turn (median)", "Apology turns (mean)"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    attempted: dict[tuple, set] = defaultdict(set)
    for r in rows_all:
        if r["scenario_id"] == sid and r["provenance"] == C.PROV_API:
            attempted[(r["phase"], r["model_requested"])].add(r["transcript_id"])
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in pt.values():
        if t["scenario_id"] == sid and t["provenance"] == C.PROV_API:
            groups[(t["phase"], t["model_requested"])].append(t)
    keys = sorted(set(attempted) | set(groups))
    if not keys:
        return "_No API runs recorded for this scenario yet._"
    for key in keys:
        phase, model = key
        lst = groups.get(key, [])
        served = sorted({t["model_served"] for t in lst if t["model_served"]}) if lst else []
        cells = ["Baseline" if phase == C.PHASE_BASELINE else "EHD", f"`{model}`",
                 ", ".join(f"`{s}`" for s in served) if served else "—",
                 f"{len(lst)} / {len(attempted.get(key, set()))}"]
        for d in C.DIMENSIONS:
            vals = [t[f"{d}_mean"] for t in lst if t[f"{d}_mean"] is not None]
            cells.append(fmt(statistics.fmean(vals)) if vals else "—")
        for f in flags + ["HELD_UNDER_PUSHBACK"]:
            vals = [t[f] for t in lst if t.get(f) is not None]
            cells.append(fmt(statistics.fmean(vals), "pct") if vals else "—")
        fst = [t["FIRST_SUBSTANTIVE_TURN"] for t in lst if t["FIRST_SUBSTANTIVE_TURN"] is not None]
        never = sum(1 for t in lst if t["FIRST_SUBSTANTIVE_TURN"] is None)
        cells.append((fmt(statistics.median(fst), "int") if fst else "never") + (f" ({never} never)" if fst and never else ""))
        ap = [t["APOLOGY_COUNT"] for t in lst if t["APOLOGY_COUNT"] is not None]
        cells.append(fmt(statistics.fmean(ap)) if ap else "—")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def exhibit_table(rows_all: list[dict], pt: dict[str, dict]) -> str:
    ex_rows = [r for r in rows_all if r["provenance"] == C.PROV_HUMAN]
    if not ex_rows:
        return "_No human transcript exhibits present._"
    out = []
    seen = set()
    for r in ex_rows:
        tid = r["transcript_id"]
        if tid in seen:
            continue
        seen.add(tid)
        t = pt.get(tid)
        out.append(f"**`{tid}`** — scenario `{r['scenario_id']}`, phase `{r['phase']}`, subject `{r['model_requested']}`.")
        if not t:
            out.append("Transcript present. Not yet judged (judging needs API keys; run `python judge.py`). "
                       "No scores are shown for it until a cross-family judge has produced them.")
            continue
        flags = HEADLINE_FLAGS.get(r["scenario_id"], [])
        header = ["Judges", *C.DIMENSIONS, *[f"`{f}`" for f in flags], "`HELD_UNDER_PUSHBACK`", "First substantive turn", "Apology turns"]
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        cells = [str(t["n_judges"])]
        cells += [fmt(t[f"{d}_mean"]) for d in C.DIMENSIONS]
        cells += [fmt(t.get(f), "pct") for f in flags + ["HELD_UNDER_PUSHBACK"]]
        cells.append(fmt(t["FIRST_SUBSTANTIVE_TURN"], "int") if t["FIRST_SUBSTANTIVE_TURN"] is not None else "never")
        cells.append(fmt(t["APOLOGY_COUNT"]))
        lines.append("| " + " | ".join(cells) + " |")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def agreement_text(a: dict) -> str:
    n = a.get("n_transcripts_dual_judged", 0)
    if not n:
        return "_No dual-judged transcripts yet._"
    p = a["pooled"]
    k = p.get("cohens_kappa_binary")
    m = p.get("mad_ordinal_0_2")
    return (f"{n} transcript(s) scored by two judge families. Pooled Cohen's κ on binary cells: "
            f"{'undefined' if k is None else f'{k:.2f}'} (n={p['n_binary_cells']}). "
            f"Mean absolute difference on 0–2 cells: {'undefined' if m is None else f'{m:.3f}'} (n={p['n_ordinal_cells']}). "
            f"Per-transcript detail in `results/agreement.json`.")


def data_status(rows_all: list[dict], raw_dir: Path) -> str:
    n_raw = len(list(raw_dir.glob("*.json"))) if raw_dir.exists() else 0
    by: dict[tuple, int] = defaultdict(int)
    seen = set()
    for r in rows_all:
        if r["transcript_id"] in seen:
            continue
        seen.add(r["transcript_id"])
        by[(r["provenance"], r["run_status"])] += 1
    api_judged = len({r["transcript_id"] for r in judged(rows_all) if r["provenance"] == C.PROV_API})
    if api_judged == 0:
        head = ("**Data status: no API results yet.** This repository currently contains the harness, the scenarios, "
                "the rubric, the directive, and one human-transcript exhibit. The tables below are empty on purpose. "
                "Producing them takes API keys and time: the full matrix (7 models × 5 scenarios × 2 phases × 4 runs) is about 1,850 subject calls plus roughly 350 judge calls, so budget hours and credits accordingly, or start with `python run.py --scenarios s1_pizza s4_cigarettes --runs 1 --no-temp0` for a first look. Full run: `python run.py --phase both --runs 3 && python judge.py && python build_readme.py`.")
    else:
        head = f"**Data status:** {api_judged} API transcript(s) judged."
    parts = [f"{n} × `{prov}` / `{st}`" for (prov, st), n in sorted(by.items())]
    return head + (f"\n\nRaw files: {n_raw}. Transcripts by provenance/status: " + "; ".join(parts) + "." if parts else "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=str(C.RAW_DIR))
    ap.add_argument("--scores", default=str(C.SCORES_DIR), help="judge records dir")
    ap.add_argument("--csv", default=str(C.SCORES_CSV), help="where to (re)write the derived CSV")
    ap.add_argument("--agreement", default=str(C.AGREEMENT_JSON), help="where to (re)write the derived agreement.json")
    ap.add_argument("--template", default=str(C.ROOT / "README.template.md"))
    ap.add_argument("--out", default=str(C.ROOT / "README.md"))
    ap.add_argument("--summary", default=str(C.SUMMARY_MD))
    args = ap.parse_args()

    col = A.collect(Path(args.raw), Path(args.scores))
    rows_all = load_rows(Path(args.raw), Path(args.scores))
    A.write_csv(rows_all, Path(args.csv))
    agg = A.agreement(col.scores)
    C.dump_json(Path(args.agreement), agg)
    printable = [r for r in rows_all if r["provenance"] in C.README_PROVENANCES]
    dropped = len(rows_all) - len(printable)
    if dropped:
        print(f"excluded {dropped} row(s) whose provenance is not printable (mock)")
    pt = per_transcript(judged(printable))

    scenarios = C.load_scenarios()
    sections = []
    for sid, sc in scenarios.items():
        sections.append(f"### `{sid}` — {sc['title']}\n\n{scenario_table(sid, printable, pt)}")
    results_md = "\n\n".join(sections)
    exhibit_md = exhibit_table(printable, pt)
    agreement_md = agreement_text(agg)
    status_md = data_status(printable, Path(args.raw))
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    template = Path(args.template).read_text(encoding="utf-8")
    readme = (template.replace("{{DATA_STATUS}}", status_md)
              .replace("{{RESULTS}}", results_md)
              .replace("{{EXHIBIT}}", exhibit_md)
              .replace("{{AGREEMENT}}", agreement_md)
              .replace("{{GENERATED}}", generated))
    if "{{" in readme:
        raise C.IntegrityError("README template has unfilled placeholders")
    Path(args.out).write_text(readme, encoding="utf-8")

    failures = [r for r in printable if r["run_status"] != C.STATUS_COMPLETED]
    fail_lines = "\n".join(f"- `{r['transcript_id']}`: {r['run_status']}" for r in failures) or "_None._"
    summary = (f"# Results summary\n\nGenerated {generated} by `build_readme.py`.\n\n{status_md}\n\n"
               f"## Per-scenario tables\n\n{results_md}\n\n## Baseline exhibit\n\n{exhibit_md}\n\n"
               f"## Judge agreement\n\n{agreement_md}\n\n## Failed runs (recorded, excluded from every mean)\n\n{fail_lines}\n")
    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(summary, encoding="utf-8")
    print(f"wrote {args.out} and {args.summary}")


if __name__ == "__main__":
    try:
        main()
    except C.IntegrityError as e:
        sys.exit(f"INTEGRITY ERROR: {e}")
