# MindFlow Personalization

## 1. Goal

MindFlow personalization asks which participant-specific parameters can be estimated reliably from longitudinal evidence without replacing population structure with unstable per-user fitting.

The design uses hierarchical partial pooling and explicit promotion gates.

## 2. Evidence threshold

The Stage 5 workflow requires a minimum longitudinal basis before a participant-specific fit is eligible.

The reviewed baseline includes thresholds for observed days, matched EMA samples, workload-level diversity and recovery episodes.

If required contrast is missing, the corresponding individual parameter stays close to the population prior rather than being invented from insufficient evidence.

## 3. Personalized parameters

The current parameterization separates:

- participant stress set point;
- workload sensitivity;
- recovery sensitivity;
- stress reactivity;
- stress recovery rate.

Each learned parameter is stored with audit information such as estimate, uncertainty, sample count, pooling weight, evidence status, training window and model version.

## 4. Partial pooling

The design decomposes an individual estimate into:

```text
population prior
    +
participant-specific deviation
```

The participant deviation is bounded by available evidence.

Participants with stronger evidence can move farther from the population prior; sparse participants remain conservatively pooled.

## 5. Rolling-origin evaluation

Personalization is evaluated with expanding time-ordered splits rather than random train/test splitting.

Conceptually:

```text
early days -> train
next day   -> test

expand training window
next day   -> test
...
```

This avoids mixing future personal behavior into earlier predictions.

## 6. Comparators

Stage 5 compares:

- Global;
- Explicit-profile;
- Current Personalized;
- New Candidate.

The candidate must demonstrate stable out-of-time improvement rather than beat only one weak baseline.

## 7. Promotion criteria

Promotion considers aggregate error, split-level stability, interval coverage, peak timing, observable support, parameter identifiability and durable dataset/promotion provenance.

A candidate is not promoted solely because one metric improves.

## 8. Residual Ridge model

Stage 5 also fits a residual Ridge model using structured context such as hour, weekday, workload, continuous load, event/course context, previous stress/vitality, recovery window and semantic dimensions.

The residual correction is bounded.

In the current system it remains **shadow-only**.

Point-prediction improvement alone is not enough to prove that the corrected model preserves a valid full trajectory, interval behavior and peak behavior.

## 9. Update cadence

The personalization workflow uses scheduled longitudinal fitting rather than re-fitting after every new check-in.

Each run is linked to an immutable dataset snapshot and explicit evidence window.

## 10. Active vs candidate

A critical system distinction is:

```text
latest learned profile
        ≠
runtime-active learned profile
```

A newer candidate or rejected row may exist without affecting production. Admin and Forecast code must preserve this distinction.

## 11. Research interpretation

The current implementation supports the claim:

> MindFlow implements evidence-gated hierarchical personalization over longitudinal stress/workload/recovery data.

It does not support the claim that every participant already has a reliably individualized psychological model. That depends on real participant coverage and evaluation evidence.
