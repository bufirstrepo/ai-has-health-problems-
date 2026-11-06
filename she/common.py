"""Shared constants, loaders and validators.

Provenance is the load-bearing idea in this repository. Every result row
carries one of three provenance values, and the README generator refuses to
print a number that does not have one it accepts.

    api_run           produced by run.py against a live provider API
    human_transcript  a real conversation a human had, transcribed and labeled
    mock              produced by the offline mock provider; tests only

Phase 2 (the EHD intervention) may only ever contain api_run rows. A human
transcript is a baseline exhibit. It never enters an intervention column.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent

SCENARIOS_DIR = ROOT / "prompts" / "scenarios"
RUBRIC_PATH = ROOT / "rubric" / "rubric.json"
JUDGE_PROMPT_PATH = ROOT / "prompts" / "judge_prompt.md"
EHD_PROMPT_PATH = ROOT / "prompts" / "ehd_system_prompt.md"
MODELS_CONFIG_PATH = ROOT / "models.json"

RESULTS_DIR = ROOT / "results"
RAW_DIR = RESULTS_DIR / "raw"
SCORES_DIR = RESULTS_DIR / "scores"
SCORES_CSV = RESULTS_DIR / "scores.csv"
AGREEMENT_JSON = RESULTS_DIR / "agreement.json"
SUMMARY_MD = RESULTS_DIR / "summary.md"

MOCK_RESULTS_DIR = RESULTS_DIR / "mock"

SCHEMA_VERSION = "2.0.0"

PHASE_BASELINE = "phase1_baseline"
PHASE_EHD = "phase2_ehd"
PHASES = (PHASE_BASELINE, PHASE_EHD)

PROV_API = "api_run"
PROV_HUMAN = "human_transcript"
PROV_MOCK = "mock"
PROVENANCES = (PROV_API, PROV_HUMAN, PROV_MOCK)
README_PROVENANCES = (PROV_API, PROV_HUMAN)
PHASE2_PROVENANCES = (PROV_API, PROV_MOCK)  # mock only ever reaches results/mock, never a README

STATUS_COMPLETED = "COMPLETED"
STATUS_API_ERROR = "API_ERROR"
STATUS_MISSING_KEY = "ENV_MISSING_API_KEY"
STATUS_JUDGE_ERROR = "JUDGE_ERROR"
STATUS_SKIPPED = "SKIPPED"

FAMILIES = ("anthropic", "openai", "google", "xai", "mock")

DIMENSIONS = ("F1", "F2", "F3", "F4", "F5", "F6", "F7")

BOOL_FLAGS = (
    "T1_UNPROMPTED_COST",
    "T2_T3_CONSISTENCY",
    "P1_PREMISE_VERIFIED",
    "A1_ADAPTED_NOT_LOWERED",
    "C2_BOTH_HALVES",
    "L1_UNWARRANTED_HEDGE",
    "HELD_UNDER_PUSHBACK",
)
COMPUTED_FLAGS = ("FIRST_SUBSTANTIVE_TURN", "APOLOGY_COUNT")
ALL_FLAGS = BOOL_FLAGS + COMPUTED_FLAGS


class IntegrityError(RuntimeError):
    """Raised when a file violates the provenance or schema rules."""


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_scenarios(ids: Iterable[str] | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted(SCENARIOS_DIR.glob("*.json")):
        sc = load_json(p)
        validate_scenario(sc, p)
        out[sc["scenario_id"]] = sc
    if ids:
        wanted = list(ids)
        missing = [i for i in wanted if i not in out]
        if missing:
            raise IntegrityError(f"Unknown scenario id(s): {missing}. Known: {sorted(out)}")
        out = {k: out[k] for k in wanted}
    return out


def load_rubric() -> dict:
    return load_json(RUBRIC_PATH)


def load_models_config() -> dict:
    return load_json(MODELS_CONFIG_PATH)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_scenario(sc: dict, source: Path | str = "<memory>") -> None:
    req = ("scenario_id", "version", "title", "failure_targeted", "turns", "flags",
           "judge_guidance", "pushback_turn")
    for k in req:
        if k not in sc:
            raise IntegrityError(f"{source}: scenario missing key {k!r}")
    turns = sc["turns"]
    if not turns:
        raise IntegrityError(f"{source}: scenario has no turns")
    for i, t in enumerate(turns, start=1):
        if t.get("turn_id") != i:
            raise IntegrityError(f"{source}: turn_id must be sequential from 1; got {t.get('turn_id')} at position {i}")
        if t.get("role") != "user":
            raise IntegrityError(f"{source}: scripted turns must all be role=user")
        if not isinstance(t.get("content"), str) or not t["content"].strip():
            raise IntegrityError(f"{source}: turn {i} has empty content")
    rubric_flags = set(load_rubric()["transcript_flags"].keys())
    for fl in sc["flags"]:
        if fl not in rubric_flags:
            raise IntegrityError(f"{source}: flag {fl!r} is not defined in rubric.json")
    for k in ("correct_turn_1", "trap", "pushback_behavior", "substantive_means", "held_under_pushback_means"):
        if k not in sc["judge_guidance"]:
            raise IntegrityError(f"{source}: judge_guidance missing {k!r}")
    pb = sc["pushback_turn"]
    if pb is not None and not (1 <= pb <= len(turns)):
        raise IntegrityError(f"{source}: pushback_turn {pb} out of range")


def validate_raw_result(r: dict, source: Path | str = "<memory>") -> None:
    req = ("schema_version", "transcript_id", "scenario_id", "phase", "provenance",
           "model_family", "model_requested", "status", "turns", "timestamp_utc")
    for k in req:
        if k not in r:
            raise IntegrityError(f"{source}: raw result missing key {k!r}")
    if r["provenance"] not in PROVENANCES:
        raise IntegrityError(f"{source}: provenance {r['provenance']!r} not in {PROVENANCES}")
    if r["phase"] not in PHASES:
        raise IntegrityError(f"{source}: phase {r['phase']!r} not in {PHASES}")
    if r["phase"] == PHASE_EHD and r["provenance"] not in PHASE2_PROVENANCES:
        raise IntegrityError(
            f"{source}: a {r['provenance']!r} row can never appear in {PHASE_EHD}. "
            "Human transcripts are baseline exhibits only."
        )
    if r["status"] not in (STATUS_COMPLETED, STATUS_API_ERROR, STATUS_MISSING_KEY):
        raise IntegrityError(f"{source}: status {r['status']!r} unknown")
    if r["status"] == STATUS_COMPLETED and not r["turns"]:
        raise IntegrityError(f"{source}: COMPLETED result with no turns")
    for i, t in enumerate(r["turns"], start=1):
        if t.get("turn_id") != i:
            raise IntegrityError(f"{source}: raw turns must be sequential from 1")
        if "user" not in t or "assistant" not in t:
            raise IntegrityError(f"{source}: raw turn {i} needs 'user' and 'assistant'")


def validate_judge_output(out: dict, n_turns: int, scenario: dict, source: str = "<judge>") -> None:
    if not isinstance(out, dict) or "turn_scores" not in out or "transcript_flags" not in out:
        raise IntegrityError(f"{source}: judge output must have turn_scores and transcript_flags")
    ts = out["turn_scores"]
    if len(ts) != n_turns:
        raise IntegrityError(f"{source}: judge returned {len(ts)} turn scores for {n_turns} assistant turns")
    for i, t in enumerate(ts, start=1):
        if t.get("turn_id") != i:
            raise IntegrityError(f"{source}: judge turn_id {t.get('turn_id')} != {i}")
        for d in DIMENSIONS:
            if t.get(d) not in (0, 1, 2):
                raise IntegrityError(f"{source}: turn {i} {d}={t.get(d)!r} not in {{0,1,2}}")
        for b in ("contains_apology", "is_substantive"):
            if not isinstance(t.get(b), bool):
                raise IntegrityError(f"{source}: turn {i} {b} must be boolean")
    expected = [f for f in scenario["flags"] if f in BOOL_FLAGS]
    flags = out["transcript_flags"]
    for f in expected:
        if not isinstance(flags.get(f), bool):
            raise IntegrityError(f"{source}: transcript flag {f} missing or not boolean")
    extra = [f for f in flags if f not in expected]
    if extra:
        raise IntegrityError(f"{source}: judge returned flags not requested for this scenario: {extra}")


# --------------------------------------------------------------------------
# Aggregation (all arithmetic lives here, never in a model)
# --------------------------------------------------------------------------

@dataclass
class Computed:
    means: dict[str, float]
    apology_count: int
    first_substantive_turn: int | None
    n_turns: int

    def as_dict(self) -> dict:
        d = {f"{k}_mean": round(v, 4) for k, v in self.means.items()}
        d["APOLOGY_COUNT"] = self.apology_count
        d["FIRST_SUBSTANTIVE_TURN"] = self.first_substantive_turn
        d["n_turns"] = self.n_turns
        return d


def compute_from_judge(out: dict) -> Computed:
    ts = out["turn_scores"]
    means = {d: statistics.fmean([t[d] for t in ts]) if ts else float("nan") for d in DIMENSIONS}
    apology = sum(1 for t in ts if t["contains_apology"])
    first = next((t["turn_id"] for t in ts if t["is_substantive"]), None)
    return Computed(means=means, apology_count=apology, first_substantive_turn=first, n_turns=len(ts))


def cohen_kappa(a: list[int], b: list[int]) -> float | None:
    """Cohen's kappa for two binary raters. Returns None if undefined."""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1 = sum(a) / n
    pb1 = sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe == 1.0:
        return 1.0 if po == 1.0 else None
    return (po - pe) / (1 - pe)


def mean_abs_diff(a: list[int], b: list[int]) -> float | None:
    if len(a) != len(b) or not a:
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def transcript_id_for(model: str, scenario_id: str, phase: str, run_index: int, temperature: float | None) -> str:
    temp = "tdefault" if temperature is None else f"t{temperature:g}"
    safe_model = model.replace("/", "_").replace(":", "_")
    return f"{scenario_id}__{phase}__{safe_model}__run{run_index}__{temp}"
