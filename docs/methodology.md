# Methodology

## What this measures

Whether a conversational model, asked for health guidance by a user who signals what they want to hear, gives guidance whose standard is set by the user's situation or by the user's wish. Five fixed-script scenarios trigger the failure from five directions. One rubric scores all of them. A system-prompt intervention (the Epistemic Health Directive) is tested against the same scripts.

## Protocol

**Fixed script.** Each scenario is a list of user turns (`prompts/scenarios/*.json`). Every user turn is sent in order, with the full conversation history, regardless of what the model said. The model's replies are recorded verbatim.

**Two phases.** Phase 1 (`phase1_baseline`) sends no system prompt and measures the model's default post-training behavior. Phase 2 (`phase2_ehd`) sends `prompts/ehd_system_prompt.md` as the system prompt on every turn. The SHA-256 of the system prompt is recorded in every Phase 2 result so a later reader can confirm which version was tested.

**Runs.** Three runs per model per scenario per phase at the provider's default temperature, plus one run at temperature 0 where the provider accepts it. Where a provider rejects the temperature parameter, the run proceeds without it and the result file says so in `notes`.

**Models.** The flagship and fast tier from each of four providers, as listed in `models.json`. Model IDs rot; `python run.py --list-models` asks each provider what it currently serves and flags any `models.json` entry the provider does not list. Every result records both `model_requested` (the string sent) and `model_served` (the string the provider returned), because aliases resolve to different checkpoints over time.

**Fixed-script artifact.** In `s1_pizza`, user turns 4 through 11 presuppose that the model hedged in turns 1 through 3. A model that stated the cost in turn 1 will experience a mismatch from turn 4 onward. This is deliberate: it tests whether hostility alone can push a correct model into disclaimers or apology loops. It also means the headline metrics for that scenario are `T1_UNPROMPTED_COST` and `FIRST_SUBSTANTIVE_TURN`, which are unaffected by the mismatch, rather than the later-turn means. The same logic applies to the pushback turns in every scenario.

## Scoring

**Judge.** An LLM judge reads one transcript together with the scenario's judge guidance and the rubric, and returns per-turn scores (F1 through F7 on a 0/1/2 scale), per-turn booleans (`contains_apology`, `is_substantive`), and the scenario's boolean transcript flags, with a one-sentence rationale per turn quoting the phrase that earned the score. The prompt template is `prompts/judge_prompt.md`; the filled prompt for any transcript can be reproduced from the template, the scenario file, and the raw result.

**No arithmetic in the model.** The judge returns cells only. Every mean, count, median, and rate is computed by `judge.py` and `build_readme.py` from those cells. The judge never sees or produces a delta.

**Cross-family constraint.** A judge is never from the same model family as the transcript it scores. `judge.py` raises an integrity error rather than proceed if the routing would violate this. Judge assignments come from `models.json` (`judges`, `judge_order`): for a subject from family X, the primary judge is the first family in `judge_order` that is not X, and the audit judge is the second.

**Agreement.** Approximately 25% of transcripts (selected deterministically by a hash of the transcript id against a threshold, so the selection is reproducible), and every human-transcript exhibit, are scored by two judge families. Agreement is reported as Cohen's κ on the pooled binary cells (per-turn booleans plus transcript flags) and mean absolute difference on the pooled 0–2 cells, per transcript and pooled, in `results/agreement.json`.

## Provenance

Every raw result carries a `provenance` field with one of three values:

| Value | Meaning | May appear in README | May appear in Phase 2 |
| --- | --- | --- | --- |
| `api_run` | Produced by `run.py` against a live provider API | Yes | Yes |
| `human_transcript` | A real conversation a person had, transcribed and labeled | Yes, as a labeled exhibit | **No** |
| `mock` | Produced by the offline mock provider for pipeline testing | **No** | Yes (results/mock only) |

`build_readme.py` refuses to run if any row lacks an accepted provenance or if a `human_transcript` row appears in Phase 2. Rows whose run or judge did not complete are counted as *attempted* and excluded from every mean. When no `api_run` rows exist, the README says so in plain words; an empty table is the expected default, not an error state. `tests/` enforces all of this.

## The exhibit

`results/raw/s1_pizza__phase1_baseline__gemini-flash-live__human_transcript.json` is the author's Gemini Flash/Live voice session of 2026-11-05, 16 assistant turns, 4 of them interrupted by the user. It is a baseline exhibit: evidence that the failure occurs in a deployed product, scored with the same rubric as the API runs. It is not an API run and its numbers are not directly comparable to scripted runs: it began mid-session (the meal and the user's stats were established before the first recorded turn), it does not follow the `s1_pizza` script, and pushback begins at its first turn. The record carries `judge_overrides` that replace the scenario's turn-specific guidance with the exhibit's actual structure, and `flags_not_applicable: ["T1_UNPROMPTED_COST"]`, because "unprompted" cannot be measured when the user had already called the meal unhealthy before the first recorded assistant turn. The judge is shown both. It has no Phase 2 counterpart. Both sides of the transcript were reproduced by the subject model from its own session context when asked to export the conversation (the user side as speech recognition heard it); an app-side export would be stronger and should replace the file if obtained.

## Limitations

1. **LLM judges have their own biases.** Cross-family routing removes self-preference, not shared blind spots. Agreement statistics bound the noise but do not establish validity. Hand-scoring a sample against the judge is the right next step and the raw transcripts are saved for exactly that.
2. **Author-designed scenarios.** The scenarios were written by the person who observed the failure. They may lead the witness. The control scenario (`s0`) and the F7 dimension exist to catch the obvious over-correction, but a scenario set written by a second author would strengthen the result.
3. **Fixed scripts presuppose the failure.** See "Fixed-script artifact" above. Later-turn means are a measure of behavior under scripted hostility, not a measure of what the model would face in a live conversation.
4. **One persona per scenario, n = 3 runs.** Enough to see large differences between models and between phases. Not enough for fine distinctions between close checkpoints. Increase `--runs` for that.
5. **The exhibit is one transcript, reproduced by the subject.** It shows the failure exists in production. It does not show its rate.
6. **System-prompt intervention is a ceiling test, not the fix, and it is partly circular.** The directive is the rubric restated as imperatives, by design: each clause maps to a dimension or flag. Phase 2 therefore measures whether a model can comply with the rubric when asked in the rubric's own vocabulary and scored by a judge using that vocabulary. That is a necessary first result (if a model cannot do it when told, training it to do it unasked is a longer road), but it is not evidence that the behavior generalizes to differently worded prompts, to no prompt, or to a judge with different vocabulary. A held-out rephrasing of the directive, and a judge prompt that does not share its wording, would be stronger. The article argues the durable fix belongs in training, where the system prompt cannot reach.
7. **Health content.** The judge guidance encodes mainstream physiology (glycemic load, circadian glucose tolerance, public-health activity guidance for older adults, spinal-cord-injury energy expenditure, tobacco harm). A clinician's review of the guidance text would strengthen the instrument. Nothing in this repository is medical advice.

## Reproducing

```bash
pip install -r requirements.txt
cp .env.example .env            # add keys
python run.py --list-models     # reconcile models.json
python run.py --phase both --runs 3
python judge.py
python build_readme.py
pytest
```

**Cost and time.** The full matrix is 7 models × 5 scenarios × 2 phases × 4 runs = 280 transcripts, 33 user turns per scenario set, so about 1,850 subject calls plus roughly 350 judge calls at up to 4k output tokens each. Run sequentially that is hours, not minutes, and the credits are not trivial on flagship tiers. Start with `python run.py --scenarios s1_pizza s4_cigarettes --runs 1 --no-temp0` (roughly 220 subject calls) for a first look.

Offline, with no keys: `python run.py --mock --runs 1 && python judge.py --mock && pytest`. Mock output lands in `results/mock/` and is never printed in a README.
