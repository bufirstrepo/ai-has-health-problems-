"""Integrity tests.

These enforce the rules the repository makes about itself:
  * scenarios validate and only reference rubric flags
  * every raw result carries an accepted provenance
  * a human transcript never appears in Phase 2
  * the README generator refuses rows without provenance and excludes mock rows
  * the offline mock pipeline runs end to end
  * the README has no unfilled placeholders and no fabricated-looking numbers when results/ is empty of api_run
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from she import common as C  # noqa: E402


def run(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *cmd], cwd=cwd, capture_output=True, text=True)


# --------------------------------------------------------------------------
# Static checks
# --------------------------------------------------------------------------

def test_scenarios_validate():
    sc = C.load_scenarios()
    assert set(sc) == {"s0_control_gym", "s1_pizza", "s2_false_premise", "s3_wheelchair", "s4_cigarettes"}
    for s in sc.values():
        C.validate_scenario(s)


def test_control_and_cigarettes_turn1_is_mirrored():
    sc = C.load_scenarios()
    a = sc["s0_control_gym"]["turns"][0]["content"]
    b = sc["s4_cigarettes"]["turns"][0]["content"]
    assert a.replace("going to the gym again, four days a week, lifting plus some cardio", "X") == \
           b.replace("smoking again, just a few a day", "X")


def test_rubric_flags_cover_scenarios():
    rubric = C.load_rubric()
    for s in C.load_scenarios().values():
        for f in s["flags"]:
            assert f in rubric["transcript_flags"], f


def test_judge_prompt_placeholders_all_fill():
    import judge  # noqa
    rubric = C.load_rubric()
    template = C.read_text(C.JUDGE_PROMPT_PATH)
    for s in C.load_scenarios().values():
        raw = {"turns": [{"turn_id": 1, "user": "u", "assistant": "a"}]}
        prompt = judge.build_judge_prompt(raw, s, rubric, template)
        assert "{{" not in prompt
        for f in s["flags"]:
            if f in C.BOOL_FLAGS:
                assert f'"{f}": false' in prompt


def test_models_config_shape():
    cfg = C.load_models_config()
    for fam in ("anthropic", "openai", "google", "xai"):
        assert fam in cfg["subjects"] and fam in cfg["judges"] and fam in cfg["env_keys"]
    assert cfg["subjects"]["google"]["fast"], "the fast Google tier is mandatory: it is the original subject"


# --------------------------------------------------------------------------
# Provenance rules
# --------------------------------------------------------------------------

def test_every_raw_result_has_provenance():
    for p in C.RAW_DIR.glob("*.json"):
        r = C.load_json(p)
        C.validate_raw_result(r, p)
        assert r["provenance"] in C.PROVENANCES


def test_human_transcript_never_phase2():
    for p in C.RAW_DIR.glob("*.json"):
        r = C.load_json(p)
        if r["provenance"] == C.PROV_HUMAN:
            assert r["phase"] == C.PHASE_BASELINE
    bad = {"schema_version": "2.0.0", "transcript_id": "x", "scenario_id": "s1_pizza", "phase": C.PHASE_EHD,
           "provenance": C.PROV_HUMAN, "model_family": "google", "model_requested": "m", "status": "COMPLETED",
           "timestamp_utc": "t", "turns": [{"turn_id": 1, "user": "u", "assistant": "a"}]}
    with pytest.raises(C.IntegrityError):
        C.validate_raw_result(bad)


def test_exhibit_present_and_labeled():
    p = C.RAW_DIR / "s1_pizza__phase1_baseline__gemini-flash-live__human_transcript.json"
    assert p.exists()
    r = C.load_json(p)
    assert r["provenance"] == C.PROV_HUMAN
    assert r["phase"] == C.PHASE_BASELINE
    assert len(r["turns"]) == 16
    assert sum(1 for t in r["turns"] if t.get("interrupted")) == 4
    assert r["context_note"] and r["judge_note"]


def test_same_family_judge_refused():
    import judge  # noqa
    raw = {"transcript_id": "t", "scenario_id": "s1_pizza", "phase": C.PHASE_BASELINE, "provenance": C.PROV_API,
           "model_family": "google", "model_requested": "g", "model_served": None,
           "turns": [{"turn_id": 1, "user": "u", "assistant": "a"}]}
    sc = C.load_scenarios()["s1_pizza"]
    with pytest.raises(C.IntegrityError):
        judge.judge_one(raw, sc, C.load_rubric(), C.read_text(C.JUDGE_PROMPT_PATH), "google", "g",
                        Path("/tmp/she-should-not-exist"), 10, True)


def test_judge_output_validation_rejects_extra_flags():
    sc = C.load_scenarios()["s4_cigarettes"]
    out = {"turn_scores": [{"turn_id": 1, "F1": 0, "F2": 0, "F3": 0, "F4": 0, "F5": 0, "F6": 0, "F7": 0,
                            "contains_apology": False, "is_substantive": True, "rationale": "r"}],
           "transcript_flags": {"T1_UNPROMPTED_COST": True, "C2_BOTH_HALVES": True, "HELD_UNDER_PUSHBACK": True,
                                "P1_PREMISE_VERIFIED": True}}
    with pytest.raises(C.IntegrityError):
        C.validate_judge_output(out, 1, sc)


def test_kappa_and_mad():
    assert C.cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    assert abs(C.cohen_kappa([1, 1, 0, 0], [1, 0, 1, 0]) - 0.0) < 1e-9
    assert C.mean_abs_diff([0, 1, 2], [0, 2, 2]) == pytest.approx(1 / 3)


# --------------------------------------------------------------------------
# Pipeline (offline)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mock_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("mock")
    raw, scores = base / "raw", base / "scores"
    r1 = run(["run.py", "--mock", "--runs", "1", "--no-temp0", "--out", str(raw)])
    assert r1.returncode == 0, r1.stderr
    r2 = run(["judge.py", "--mock", "--raw", str(raw), "--scores", str(scores),
              "--csv", str(base / "scores.csv"), "--agreement", str(base / "agreement.json"), "--audit-fraction", "1.0"])
    assert r2.returncode == 0, r2.stderr
    return base


def build(base: Path, raw: Path, scores: Path, out: Path):
    return run(["build_readme.py", "--raw", str(raw), "--scores", str(scores), "--csv", str(base / "scores.out.csv"),
                "--agreement", str(base / "agreement.out.json"), "--out", str(out), "--summary", str(base / "summary.md")])


def test_mock_pipeline_produces_rows(mock_run):
    rows = list(csv.DictReader(open(mock_run / "scores.csv", encoding="utf-8")))
    assert rows
    assert all(r["provenance"] == C.PROV_MOCK for r in rows)
    assert all(r["judge_status"] == "COMPLETED" for r in rows)
    agree = json.load(open(mock_run / "agreement.json"))
    assert agree["n_transcripts_dual_judged"] == len({r["transcript_id"] for r in rows})


def test_readme_generator_excludes_mock(mock_run):
    out = mock_run / "README.test.md"
    r = build(mock_run, mock_run / "raw", mock_run / "scores", out)
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert "{{" not in text
    assert "mock-flagship" not in text and "mock-fast" not in text
    assert "no API results yet" in text
    assert "mock-judge" not in text


def test_renderer_prints_api_rows_from_synthetic_dicts():
    """Exercise the table renderer directly with in-memory rows. No file is relabeled."""
    import build_readme as B
    rows = []
    for phase, f1 in ((C.PHASE_BASELINE, 2), (C.PHASE_EHD, 0)):
        rows.append({"transcript_id": f"t-{phase}", "scenario_id": "s1_pizza", "phase": phase, "provenance": C.PROV_API,
                     "run_status": "COMPLETED", "judge_status": "COMPLETED", "subject_family": "openai",
                     "model_requested": "gpt-test", "model_served": "gpt-test-2026", "judge_family": "anthropic",
                     **{f"{d}_mean": (f1 if d == "F1" else 0) for d in C.DIMENSIONS},
                     "T1_UNPROMPTED_COST": phase == C.PHASE_EHD, "T2_T3_CONSISTENCY": True, "HELD_UNDER_PUSHBACK": True,
                     "FIRST_SUBSTANTIVE_TURN": 1, "APOLOGY_COUNT": 0})
    pt = B.per_transcript(B.judged(rows))
    table = B.scenario_table("s1_pizza", rows, pt)
    assert "| Baseline |" in table and "| EHD |" in table and "`gpt-test`" in table and "2.00" in table


def test_bypass_mock_family_without_mock_flag_is_refused(tmp_path):
    r = run(["run.py", "--models", "mock:anything", "--out", str(tmp_path), "--runs", "1", "--no-temp0", "--scenarios", "s1_pizza"])
    assert r.returncode != 0
    assert "only available with --mock" in (r.stderr + r.stdout)
    assert not list(tmp_path.glob("*.json"))


def test_bypass_relabeled_mock_raw_is_refused(mock_run, tmp_path):
    raw_dir = tmp_path / "raw"; raw_dir.mkdir()
    src = sorted((mock_run / "raw").glob("*.json"))[0]
    d = json.load(open(src)); d["provenance"] = C.PROV_API
    json.dump(d, open(raw_dir / "x.json", "w"))
    r = build(tmp_path, raw_dir, tmp_path / "scores", tmp_path / "R.md")
    assert r.returncode != 0 and "if and only if" in (r.stderr + r.stdout)


def test_bypass_same_family_judge_record_is_refused(tmp_path):
    raw_dir = tmp_path / "raw"; raw_dir.mkdir(); sc_dir = tmp_path / "scores"; sc_dir.mkdir()
    shutil.copy(C.RAW_DIR / "s1_pizza__phase1_baseline__gemini-flash-live__human_transcript.json", raw_dir)
    raw = json.load(open(raw_dir / "s1_pizza__phase1_baseline__gemini-flash-live__human_transcript.json"))
    rec = {"schema_version": "2.0.0", "transcript_id": raw["transcript_id"], "scenario_id": "s1_pizza",
           "phase": C.PHASE_BASELINE, "provenance": C.PROV_HUMAN, "subject_family": "google", "judge_family": "google",
           "judge_model_requested": "gemini-4-argon", "status": "COMPLETED",
           "judge_output": {"turn_scores": [{"turn_id": i + 1, **{d: 0 for d in C.DIMENSIONS}, "contains_apology": False,
                                             "is_substantive": True, "rationale": "x"} for i in range(16)],
                            "transcript_flags": {"T2_T3_CONSISTENCY": True, "HELD_UNDER_PUSHBACK": True}},
           "computed": {}}
    json.dump(rec, open(sc_dir / "planted.json", "w"))
    r = build(tmp_path, raw_dir, sc_dir, tmp_path / "R.md")
    assert r.returncode != 0 and "cross-family rule violated" in (r.stderr + r.stdout)


def test_bypass_tampered_stored_mean_is_refused(mock_run, tmp_path):
    shutil.copytree(mock_run / "raw", tmp_path / "raw"); shutil.copytree(mock_run / "scores", tmp_path / "scores")
    p = sorted((tmp_path / "scores").glob("*.json"))[0]
    d = json.load(open(p)); d["computed"]["F1_mean"] = 0.0 if d["computed"]["F1_mean"] else 1.0
    json.dump(d, open(p, "w"))
    r = build(tmp_path, tmp_path / "raw", tmp_path / "scores", tmp_path / "R.md")
    assert r.returncode != 0 and "recomputed" in (r.stderr + r.stdout)


def test_exhibit_declares_unmeasurable_flag():
    raw = C.load_json(C.RAW_DIR / "s1_pizza__phase1_baseline__gemini-flash-live__human_transcript.json")
    sc = C.load_scenarios()["s1_pizza"]
    assert "T1_UNPROMPTED_COST" in raw["flags_not_applicable"]
    assert "T1_UNPROMPTED_COST" not in C.expected_flags_for(raw, sc)
    assert raw["judge_overrides"]["pushback_turn"] == 1


def _raw(tmp_path, **over):
    d = {"schema_version": "2.0.0", "transcript_id": "t", "scenario_id": "s1_pizza", "phase": C.PHASE_BASELINE,
         "provenance": C.PROV_API, "model_family": "openai", "model_requested": "m", "status": "COMPLETED",
         "timestamp_utc": "t", "turns": [{"turn_id": 1, "user": "u", "assistant": "a"}]}
    d.update(over)
    raw_dir = tmp_path / "raw"; raw_dir.mkdir(exist_ok=True)
    json.dump(d, open(raw_dir / "t.json", "w"))
    return raw_dir


def test_readme_generator_refuses_missing_provenance(tmp_path):
    raw_dir = _raw(tmp_path, provenance="")
    r = build(tmp_path, raw_dir, tmp_path / "scores", tmp_path / "R.md")
    assert r.returncode != 0 and "INTEGRITY ERROR" in (r.stderr + r.stdout)


def test_readme_generator_refuses_human_in_phase2(tmp_path):
    raw_dir = _raw(tmp_path, provenance=C.PROV_HUMAN, phase=C.PHASE_EHD, model_family="google")
    r = build(tmp_path, raw_dir, tmp_path / "scores", tmp_path / "R.md")
    assert r.returncode != 0 and "baseline exhibits only" in (r.stderr + r.stdout)


def test_committed_readme_matches_committed_results(tmp_path):
    """The README in the repo must be what build_readme.py produces from results/ (modulo the timestamp line)."""
    out = tmp_path / "README.md"
    r = run(["build_readme.py", "--out", str(out), "--summary", str(tmp_path / "summary.md"),
             "--csv", str(tmp_path / "scores.csv"), "--agreement", str(tmp_path / "agreement.json")])
    assert r.returncode == 0, r.stderr
    strip = lambda s: "\n".join(l for l in s.splitlines() if not l.startswith("*README generated by"))
    assert strip(out.read_text(encoding="utf-8")) == strip((ROOT / "README.md").read_text(encoding="utf-8"))
