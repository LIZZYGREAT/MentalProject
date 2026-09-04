# MindFlow Modeling

## 1. Modeling objective

MindFlow models how a participant's stress-related latent state evolves through the day under changing schedule, workload, observation, and recovery context.

The model is not a diagnostic classifier.

Its primary output is a time-indexed trajectory used for pressure forecasting, high-pressure window detection, longitudinal evaluation, context-aware support timing and later personalization.

## 2. Continuous-time state-space view

The shared model is implemented as a continuous-time latent-state system integrated by the simulator.

A trajectory point at time `t` means the state at `t` after assimilating observations available at that time.

The timeline distinguishes the state at `00:00`, 5-minute trajectory points during the day, the final recorded point near the end of day, and a separate `24:00` terminal state.

This distinction matters for next-day initialization and avoiding off-by-one interval leakage.

## 3. Model variants

MindFlow keeps a stable baseline and several richer candidate variants.

### M0

Baseline stress-state model and fail-closed production fallback.

### WM0

A workload-aware stress candidate that allows workload/recovery-derived inputs to affect stress dynamics.

### M1

Adds a dynamic vitality state.

### M2

Extends the latent-state definition with perseverative-cognition-related dynamics.

### M3

Adds bounded recovery debt. The recovery-debt state is constrained so high load does not produce unbounded accumulation.

## 4. Why multiple variants are retained

More latent states do not automatically mean a better model.

A richer model can overfit sparse individual data, introduce weakly identifiable parameters, degrade interval coverage, shift peak timing or create unstable personalized parameters.

Therefore M0 remains a stable comparator/fallback, while richer variants must pass formal replay and evaluation gates.

## 5. Event and workload representation

Calendar events are converted into structured event context.

The classification pipeline combines deterministic rules, bounded course-catalog retrieval, and semantic enrichment when enabled and authorized.

Course identity is separated from event-type classification so a fuzzy course match does not automatically become a false canonical course.

The workload subsystem derives interpretable features including event demand, anticipatory influence, post-event influence, continuous load and recovery resources.

Overlapping workload is combined with bounded saturation rather than unlimited addition.

`W(t)` is an explanatory workload quantity, not a new psychological diagnosis or directly observed mental state.

## 6. Observation assimilation

Momentary self-reports provide direct state evidence.

Observation handling respects two times:

- `observed_at`: when the state was experienced;
- `created_at`: when the system actually learned the record.

Historical evaluation requires both to be available before the corresponding cutoff.

## 7. Forecast provenance

Each persisted Forecast freezes information needed to interpret the result, including model identity and relevant input revisions.

Promotion provenance and parameter hashes are used so a non-baseline model cannot remain active after its supporting evidence becomes inconsistent.

## 8. Forecast uncertainty

Where the active model produces interval information, the persisted trajectory can include prediction intervals.

Interval coverage is evaluated separately from point-error metrics. A candidate is not considered fully validated merely because it improves MAE.

## 9. Peak and warning semantics

The pressure trajectory is analyzed for peak stress, sustained high-pressure periods and warning windows.

Warning selection/delivery is separate from the mathematical trajectory. This keeps state estimation separate from intervention policy.

## 10. Current modeling boundary

The current stage intentionally avoids claiming clinical diagnosis, validated causal treatment effects, automatic superiority of M1/M2/M3, or production use of unvalidated residual corrections.

Model changes should be driven by collected longitudinal evidence and rolling-origin evaluation rather than by adding states by default.
