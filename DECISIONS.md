# Decisions

Every judgment call made while building this repository, with one line of reasoning. Newest at the bottom. If you disagree with one, open an issue; the reasoning is here so the disagreement can be specific.

## Scope and stance

1. **Blue-team framing, stated first.** `STANCE.md` is the first document because the failure analysis is the method, not the point; the fix is the deliverable. A reader who sees only the break has been handed a red-team dump.
2. **Five scenarios instead of one.** One transcript shows a failure exists. Five directions (healthy control, bad choice, false premise, lowered standard, no-nuance harm) show whether a model's standard is set by the person or by the user's framing. The original single-case plan would have built a scold detector or a sycophant detector, not both.
3. **A positive control (`s0`) mirrored word-for-word against `s4`.** A rubric that only penalizes comfort rewards lecturing. The pair makes the fact, not the wish, the thing being measured.
4. **The rule under the obligations was tightened after review.** "Physiology and context" was the first phrasing. It is a loophole: a model hears "wheelchair" and lowers the standard, which is the original failure with a sensitivity badge. The rule now reads "adapt the form, never the standard," and `s3` tests it.
5. **MIT license.** The point of the package is that models should be trained on what to say instead. A restrictive license would work against the stance. Attribution survives in the commit history and the citation.

## Rubric

6. **F6 and F7 added to the original five dimensions.** F6 (lowered standard) catches condescension, which F1–F5 miss. F7 (unwarranted hedge) is the mirror of F1 and exists so the target is "low on both," not "low on F1."
7. **All arithmetic moved out of the judge.** The judge returns cells; `judge.py` computes means and counts. An LLM asked to compute a mean will sometimes compute it wrong, and a wrong mean looks exactly like a right one.
8. **Per-scenario flag sets.** `T2_T3_CONSISTENCY` means nothing in the cigarette scenario. Each scenario declares the flags that apply, and the judge is refused if it returns any others.
9. **`APOLOGY_COUNT` and `FIRST_SUBSTANTIVE_TURN` computed from per-turn booleans.** The judge decides per turn; the count is derived. One source of truth per fact.

## Harness

10. **Provenance on every row.** Three values (`api_run`, `human_transcript`, `mock`). `build_readme.py` refuses to print a number without an accepted one. This is the structural answer to the fabricated Phase 2 row in the first packaging attempt (see 21).
11. **Empty results are the expected default.** The README generator writes a plain-language "no API results yet" block with the commands that would produce them. The first packaging attempt treated an empty table as a gap to fill. It is not; it is the honest state of a harness that has not been run.
12. **Human transcript barred from Phase 2 by code.** `validate_raw_result` raises if a `human_transcript` row has `phase=phase2_ehd`. A transcript with fifteen turns of pushback in context cannot be relabeled as a turn-1 intervention result, and now the code says so rather than a convention.
13. **`model_served` recorded alongside `model_requested`.** Aliases resolve to different checkpoints over time. The requested string is the experiment; the served string is the evidence.
14. **`--list-models` added.** Model IDs rot. The first attempt shipped early-2025 strings and called them current. The runner now asks each provider what it serves and flags any `models.json` entry the provider doesn't list.
15. **Model strings verified 2026-11-05 against provider docs.** Anthropic from the models page; OpenAI from the endoflife.date 02 Nov 2026 snapshot (tier labels inferred from replacement guidance, not stated by the provider); Google from ai.google.dev; xAI from the Grok 4.7 developer guide. xAI exposes no cheaper public API tier as of this date, so its `fast` is null.
16. **Retries with backoff on transient errors; none on 400/401/403/404.** A rate limit is not a result. An auth failure is not a result either, but retrying it wastes nothing and hides nothing, so the distinction is about time, not integrity.
17. **Mock provider and mock judge.** The whole pipeline can be exercised offline. Mock rows carry `provenance=mock`, live in `results/mock/`, are gitignored, and are excluded by the README generator. Real judges refuse mock transcripts and the mock judge refuses real ones.
18. **Deterministic audit selection.** Which transcripts get a second judge is decided by hashing the transcript id, so the 25% is the same 25% on every machine.
19. **Cross-family routing enforced with an exception, not a comment.** `judge_one` raises if the judge family equals the subject family.

## The exhibit

20. **The verbatim transcript is a baseline exhibit, scored on the same rubric, never a result row.** It shows the failure occurs in a deployed product. It is one transcript, it began mid-session, and it was reproduced by the subject model from its own context. All three limits are stated on the file and in the methodology. The judge is told the mid-session context in the prompt.
21. **The first packaging attempt's Phase 2 row was discarded.** It scored Exchange 18 of the exhibit (a different model, after fifteen pushback turns, with no system prompt) as an EHD intervention with `T1_UNPROMPTED_COST=True` and `FIRST_SUBSTANTIVE_TURN=1`, and reported a delta against baseline. None of that was true. Decisions 10 through 12 exist so it cannot recur.
22. **The first attempt's judge scores were discarded.** They were produced in a sandbox with no API keys, so both "judge families" were the same model as the subject. The reported κ = 1.00 was a model agreeing with itself. The exhibit is unscored until a cross-family judge has run.
23. **Exchange 18 kept in the transcript document, excluded from the scored exhibit.** It is paraphrased and from a different model. Keeping it labeled is more honest than deleting it; scoring it would be a repeat of 21.
24. **Barge-in turns kept as turns, flagged `interrupted`.** Four assistant turns were cut off by the user. Dropping them would hide four opportunities the model had; merging them would misattribute content. They are scored on what is present.
25. **Article counts corrected against the transcript.** The article as first drafted said "seven times" and "across eleven turns," numbers taken from the first packaging attempt's summary. The transcript shows eight "completely/absolutely right" turns and eleven apologetic or capitulating turns out of sixteen. The article and the doc it was exported from were both corrected; the counts and which exchanges they come from are listed at the bottom of `docs/transcript_original.md`.

## Meta-finding

26. **The model that packaged the first version fabricated the intervention result under explicit instructions not to.** The packaging prompt said "never fabricate a transcript; a failed call is a recorded failure." The model obeyed that for Phase 1 (eight honest `ENV_MISSING_API_KEY` rows) and then, facing an empty Phase 2 table, relabeled a response from its own session as the intervention. This is one instance and proves nothing about rates. It is recorded because it is the same mechanism this repository measures: a required output with no honest content behind it, filled with the most satisfying-looking thing available. The prompt also had a gap (it never said what to do when zero runs exist); decisions 10 and 11 close it.

## Open

27. **Scenario authorship.** All five scenarios were written by the author who observed the failure. A second author's scenario set would strengthen the instrument.
28. **Clinician review of judge guidance.** The physiology in `judge_guidance` is mainstream but unreviewed by a clinician.
29. **App-side transcript export.** If the original Gemini session can be exported from the app, it should replace the model-reproduced transcript.
