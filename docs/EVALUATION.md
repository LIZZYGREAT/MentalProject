# MindFlow Evaluation

## 1. Evaluation objective

Evaluation in MindFlow is not only a final accuracy report. It is also a production safety boundary.

A richer or personalized model is allowed to affect runtime behavior only when its evidence is compatible with the reviewed evaluation contract.

## 2. Knowledge-causal evaluation

Historical model evaluation must respect what was actually known at the time.

For a split origin, later-created records are excluded even when their `observed_at` points to an earlier period.

Examples include backfilled EMA, later profile updates, later psychometric records, promotion evidence created after the split, and later Forecast versions.

This prevents look-ahead bias.

## 3. Immutable dataset snapshots

Research runs use immutable dataset snapshots.

A snapshot freezes the identities and provenance of relevant records so later database changes do not silently alter an earlier experiment.

Snapshot contents have evolved across schema versions as more evidence became necessary for valid evaluation.

## 4. Rolling-origin design

For longitudinal participant modeling, the core design is expanding rolling-origin evaluation.

Example:

```text
days 1–14 -> train
day 15    -> test

days 1–15 -> train
day 16     -> test
...
```

Each split uses only information available before the test origin.

## 5. Core metrics

Current model comparison includes metrics such as:

- MAE;
- RMSE;
- prediction-interval coverage;
- peak timing error.

Point accuracy and trajectory behavior are deliberately separated.

## 6. Identifiability and observable support

A parameter should not be promoted merely because an optimizer returns a number.

MindFlow therefore tracks whether the training data contains enough relevant variation to identify important workload/recovery relationships.

The evaluation layer can distinguish supported, weak and not identified states. A not-identified parameter is a blocking condition for promotion.

## 7. Baseline replay

The stable M0 baseline is replayed through the same reviewed AssessmentModel → Simulator → state-dynamics path used by richer candidates.

Historical production predictions can be retained as a descriptive comparator but do not replace formal replay in the promotion gate.

## 8. Stage 5 evaluation

Personalization compares Global, Explicit-profile, Current Personalized and New Candidate.

The candidate must show stable out-of-time performance and must not materially degrade important trajectory properties.

Promotion evidence is persisted and bound to the exact dataset/model/parameter identity.

## 9. Care effectiveness

Care outcome analysis currently reports descriptive observational evidence.

It can summarize helpful feedback, 30-minute follow-up changes, 60-minute follow-up changes, forecast residuals, receptivity and contextual groups.

Uncertainty estimates are reported for descriptive summaries.

However:

```text
causal_claim_allowed = false
mrt_runtime_enabled = false
```

Therefore these analyses cannot be interpreted as causal treatment effects.

## 10. JITAI / MRT boundary

The runtime contains JITAI-style decision logic and MRT-ready data contracts.

That is different from having completed a randomized micro-randomized trial.

A future causal study would require an explicit randomization policy, trial protocol, eligibility/availability definition, logged randomization probability, causal estimator and appropriate sample size/statistical analysis.

## 11. Admin research views

Admin exposes cohort metrics, participant evaluation, model comparison, data quality, parameter learning, promotion history, workload diagnostics and Care outcomes.

These views are intended to expose the evidence behind a conclusion rather than show a single unexplained score.

## 12. Reporting principle

Public reporting should distinguish:

```text
implemented evaluation framework
```

from:

```text
empirically demonstrated improvement on collected study data
```

The first is a system capability. The second requires completed real-data experiments.
