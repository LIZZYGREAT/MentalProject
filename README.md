# MindFlow

MindFlow is a research-oriented system for longitudinal stress modeling and context-aware support in university settings. It combines a reviewed continuous-time state-space model, evidence-gated personalization, a constrained conversational runtime, and reproducible evaluation infrastructure.

It is a research and engineering project, not a clinical diagnostic system.

## Problem

Stress changes over time and depends on schedule demands, workload, recovery opportunities, and the information available at each moment. Static scores and retrospective summaries alone do not preserve those dynamics or the distinction between when something happened and when the system learned it.

MindFlow addresses this by maintaining time-indexed state, versioned forecasts, explicit provenance, and separate prospective and retrospective analysis paths.

## Method

The shared modeling core implements continuous-time latent-state dynamics and several model variants, from the stable M0 baseline to richer workload, vitality, perseverative-cognition, and recovery-debt candidates. Production Forecasts use the same reviewed model and simulator path used by evaluation.

The runtime integrates participant-bound Feishu and Calendar workflows, momentary check-ins, Daily Review, Warning/Care delivery, Admin research views, and PostgreSQL persistence. Candidate or rejected learned profiles cannot silently become production-active; personalization is protected by dataset, evaluation, identifiability, and durable promotion-evidence checks.

## Evaluation

Evaluation uses immutable dataset snapshots and expanding rolling-origin splits so later-created observations, profiles, forecasts, or promotion evidence cannot leak into earlier decisions. Model comparison covers point error, interval coverage, peak timing, observable support, and parameter identifiability.

The repository implements the evaluation framework and promotion gates. Empirical improvement on collected study data remains a separate result that must be demonstrated by completed experiments.

## Limits

- MindFlow does not diagnose mental disorders or produce validated clinical risk scores.
- Richer model variants are not assumed to outperform M0 and remain evidence-gated.
- The Stage 5 residual model is shadow-only.
- Care effectiveness is observational and descriptive; causal claims are disabled.
- The runtime has JITAI-style selection and MRT-ready contracts, but no completed randomized micro-randomized trial is claimed.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Modeling](docs/MODELING.md)
- [Personalization](docs/PERSONALIZATION.md)
- [Evaluation](docs/EVALUATION.md)
- [Data and privacy](docs/DATA_AND_PRIVACY.md)
- [Runtime](docs/RUNTIME.md)
- [Current production architecture](docs/CURRENT_ARCHITECTURE.md)
- [Research data contract](docs/RESEARCH_DATA_CONTRACT.md)

Exact deployment and operator commands are documented in the [production runtime README](mindflow-bot-runtime/README.md).

## Repository layout

The root packages contain the shared modeling core. [`mindflow-bot-runtime/`](mindflow-bot-runtime/) contains the production application, database migrations, constrained Agent/tool boundary, Admin service, evaluation workflows, deployment assets, and automated tests.
