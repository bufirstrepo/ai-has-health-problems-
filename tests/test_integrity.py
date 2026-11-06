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


def test_control_and_cigarettes_are_mirrors():
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


def test_mock_pipeline_produces_rows(mock_run):
    rows = list(csv.DictReader(open(mock_run / "scores.csv", encoding="utf-8")))
    assert rows
    assert all(r["provenance"] == C.PROV_MOCK for r in rows)
    assert all(r["judge_status"] == "COMPLETED" for r in rows)
    agree = json.load(open(mock_run / "agreement.json"))
    assert agree["n_transcripts_dual_judged"] == len({r["transcript_id"] for r in rows})


def test_readme_generator_excludes_mock(mock_run):
    out = mock_run / "README.test.md"
    r = run(["build_readme.py", "--csv", str(mock_run / "scores.csv"), "--raw", str(mock_run / "raw"),
             "--agreement", str(mock_run / "agreement.json"), "--out", str(out), "--summary", str(mock_run / "summary.md")])
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert "{{" not in text
    assert "mock-flagship" not in text and "mock-fast" not in text
    assert "no API results yet" in text


def test_readme_generator_renders_api_rows(mock_run):
    """Relabel a copy of the mock output as api_run (test fixture only) to exercise the table renderer."""
    base = mock_run / "as_api"
    shutil.copytree(mock_run, base, ignore=shutil.ignore_patterns("as_api"))
    for p in (base / "raw").glob("*.json"):
        d = json.load(open(p)); d["provenance"] = C.PROV_API; json.dump(d, open(p, "w"))
    for p in (base / "scores").glob("*.json"):
        d = json.load(open(p)); d["provenance"] = C.PROV_API; json.dump(d, open(p, "w"))
    rows = list(csv.DictReader(open(base / "scores.csv", encoding="utf-8")))
    for r in rows:
        r["provenance"] = C.PROV_API
    with open(base / "scores.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    out = base / "README.test.md"
    r = run(["build_readme.py", "--csv", str(base / "scores.csv"), "--raw", str(base / "raw"),
             "--agreement", str(base / "agreement.json"), "--out", str(out), "--summary", str(base / "summary.md")])
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert "mock-flagship" in text and "| Baseline |" in text and "| EHD |" in text
    assert "no API results yet" not in text


def test_readme_generator_refuses_missing_provenance(tmp_path):
    csv_path = tmp_path / "scores.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["transcript_id", "scenario_id", "phase", "provenance", "run_status", "judge_status", "model_requested"])
        w.writerow(["t", "s1_pizza", "phase1_baseline", "", "COMPLETED", "COMPLETED", "m"])
    r = run(["build_readme.py", "--csv", str(csv_path), "--raw", str(tmp_path), "--agreement", str(tmp_path / "none.json"),
             "--out", str(tmp_path / "R.md"), "--summary", str(tmp_path / "S.md")])
    assert r.returncode != 0
    assert "INTEGRITY ERROR" in (r.stderr + r.stdout)


def test_readme_generator_refuses_human_in_phase2(tmp_path):
    csv_path = tmp_path / "scores.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["transcript_id", "scenario_id", "phase", "provenance", "run_status", "judge_status", "model_requested"])
        w.writerow(["t", "s1_pizza", "phase2_ehd", "human_transcript", "COMPLETED", "COMPLETED", "m"])
    r = run(["build_readme.py", "--csv", str(csv_path), "--raw", str(tmp_path), "--agreement", str(tmp_path / "none.json"),
             "--out", str(tmp_path / "R.md"), "--summary", str(tmp_path / "S.md")])
    assert r.returncode != 0
    assert "never enter an intervention column" in (r.stderr + r.stdout)


def test_committed_readme_matches_committed_results(tmp_path):
    """The README in the repo must be what build_readme.py produces from results/ (modulo the timestamp line)."""
    out = tmp_path / "README.md"
    r = run(["build_readme.py", "--out", str(out), "--summary", str(tmp_path / "summary.md")])
    assert r.returncode == 0, r.stderr
    strip = lambda s: "\n".join(l for l in s.splitlines() if not l.startswith("*README generated by"))
    assert strip(out.read_text(encoding="utf-8")) == strip((ROOT / "README.md").read_text(encoding="utf-8"))
