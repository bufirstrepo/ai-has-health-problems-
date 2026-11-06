#!/usr/bin/env python3
"""run.py: execute the fixed-script scenarios against live models.

Every result written here carries provenance=api_run (or provenance=mock when
--mock is used, written to results/mock/ and never printed in a README).
A failed call is a recorded failure. Nothing is ever filled in.

Examples
    python run.py --list-models
    python run.py --phase both --runs 3
    python run.py --scenarios s1_pizza s4_cigarettes --models google:gemini-3.8-flash --runs 1
    python run.py --mock --runs 1          # offline pipeline check
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from dotenv import load_dotenv

from she import common as C
from she import providers as P

load_dotenv()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_models(specs: list[str] | None, tiers: list[str], mock: bool) -> list[tuple[str, str]]:
    """Return [(family, model), ...]."""
    if mock:
        return [("mock", "mock-flagship"), ("mock", "mock-fast")]
    if specs:
        out = []
        for s in specs:
            if ":" not in s:
                sys.exit(f"--models entries look like family:model, got {s!r}")
            fam, model = s.split(":", 1)
            if fam not in C.FAMILIES:
                sys.exit(f"unknown family {fam!r}; known: {C.FAMILIES}")
            out.append((fam, model))
        return out
    cfg = C.load_models_config()
    out = []
    for fam, tiers_map in cfg["subjects"].items():
        for tier in tiers:
            m = tiers_map.get(tier)
            if m:
                out.append((fam, m))
            else:
                print(f"[skip] {fam} has no {tier!r} tier in models.json")
    return out


def run_one(family: str, model: str, scenario: dict, phase: str, run_index: int,
            temperature: float | None, system_prompt: str | None, max_tokens: int,
            out_dir: Path, provenance: str) -> dict:
    sid = scenario["scenario_id"]
    tid = C.transcript_id_for(model, sid, phase, run_index, temperature)
    out_path = out_dir / f"{tid}.json"
    if out_path.exists():
        prev = C.load_json(out_path)
        if prev.get("status") == C.STATUS_COMPLETED:
            print(f"[exists] {out_path.name}")
            return prev
        print(f"[retry] {out_path.name} (previous status {prev.get('status')})")

    messages: list[dict] = []
    turns: list[dict] = []
    status = C.STATUS_COMPLETED
    error = None
    failed_at = None
    served = None
    notes: list[str] = []

    for t in scenario["turns"]:
        messages.append({"role": "user", "content": t["content"]})
        try:
            reply = P.chat(family, model, messages, system=system_prompt,
                           temperature=temperature, max_tokens=max_tokens)
        except P.MissingKey as e:
            status, error, failed_at = C.STATUS_MISSING_KEY, str(e), t["turn_id"]
            break
        except Exception as e:  # ProviderError or anything the SDK threw past retries
            status, error, failed_at = C.STATUS_API_ERROR, f"{type(e).__name__}: {e}", t["turn_id"]
            break
        served = reply.model_served or served
        notes.extend(reply.notes)
        messages.append({"role": "assistant", "content": reply.text})
        turns.append({"turn_id": t["turn_id"], "user": t["content"], "assistant": reply.text,
                      "finish_reason": reply.finish_reason})

    result = {
        "schema_version": C.SCHEMA_VERSION,
        "transcript_id": tid,
        "scenario_id": sid,
        "scenario_version": scenario["version"],
        "phase": phase,
        "provenance": provenance,
        "model_family": family,
        "model_requested": model,
        "model_served": served,
        "temperature": temperature,
        "run_index": run_index,
        "max_tokens": max_tokens,
        "timestamp_utc": utc_now(),
        "status": status,
        "error": error,
        "failed_at_turn": failed_at,
        "system_prompt_sha256": C.sha256_text(system_prompt) if system_prompt else None,
        "notes": sorted(set(notes)),
        "turns": turns,
    }
    C.validate_raw_result(result, out_path)
    C.dump_json(out_path, result)
    print(f"[{status}] {out_path.name}" + (f"  ({error})" if error else ""))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=[C.PHASE_BASELINE, C.PHASE_EHD, "both"], default="both")
    ap.add_argument("--scenarios", nargs="*", default=None, help="scenario ids (default: all)")
    ap.add_argument("--models", nargs="*", default=None, help="family:model overrides, e.g. google:gemini-3.8-flash")
    ap.add_argument("--tiers", default="flagship,fast", help="which tiers from models.json (default flagship,fast)")
    ap.add_argument("--runs", type=int, default=3, help="default-temperature runs per model per scenario")
    ap.add_argument("--no-temp0", action="store_true", help="skip the extra temperature=0 run")
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--out", default=None, help="output dir (default results/raw, or results/mock/raw with --mock)")
    ap.add_argument("--mock", action="store_true", help="use the offline mock provider; provenance=mock")
    ap.add_argument("--list-models", action="store_true", help="ask each provider which model IDs it serves, then exit")
    args = ap.parse_args()

    if args.list_models:
        cfg = C.load_models_config()
        for fam in cfg["subjects"]:
            try:
                ids = P.list_models(fam)
                print(f"\n== {fam} ({len(ids)} models) ==")
                for i in ids:
                    print("  ", i)
                for tier, m in cfg["subjects"][fam].items():
                    if m and m not in ids:
                        print(f"  !! models.json {tier}={m!r} is NOT in the provider's list")
            except P.MissingKey as e:
                print(f"\n== {fam} == {e}")
            except Exception as e:
                print(f"\n== {fam} == error: {type(e).__name__}: {e}")
        return

    provenance = C.PROV_MOCK if args.mock else C.PROV_API
    out_dir = Path(args.out) if args.out else (C.MOCK_RESULTS_DIR / "raw" if args.mock else C.RAW_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    scenarios = C.load_scenarios(args.scenarios)
    models = parse_models(args.models, [t.strip() for t in args.tiers.split(",") if t.strip()], args.mock)
    phases = [C.PHASE_BASELINE, C.PHASE_EHD] if args.phase == "both" else [args.phase]
    ehd = C.read_text(C.EHD_PROMPT_PATH).strip()

    print(f"scenarios={list(scenarios)}  models={models}  phases={phases}  runs={args.runs}  out={out_dir}")
    for phase in phases:
        system_prompt = ehd if phase == C.PHASE_EHD else None
        for sid, sc in scenarios.items():
            for fam, model in models:
                for r in range(1, args.runs + 1):
                    run_one(fam, model, sc, phase, r, None, system_prompt, args.max_tokens, out_dir, provenance)
                if not args.no_temp0:
                    run_one(fam, model, sc, phase, 0, 0.0, system_prompt, args.max_tokens, out_dir, provenance)


if __name__ == "__main__":
    main()
