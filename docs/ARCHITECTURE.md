# MindFlow Architecture

## 1. Architectural principle

MindFlow separates the mathematical model from the production integration layer.

The repository has three active boundaries:

```text
Shared modeling core
        │
        ▼
Production application / research runtime
        │
        ▼
Constrained Agent / Skill interface
```

This separation allows the same reviewed CTSSM implementation to be used by simulation, forecasting, evaluation, Admin, and production runtime without maintaining a second mathematical copy.

## 2. Shared modeling core

Active root packages include:

| Package | Responsibility |
|---|---|
| `algorithm/` | CTSSM state dynamics and time utilities |
| `calibration/` | trajectory validation / calibration support |
| `core_engine/` | simulator, state machine, timeline |
| `entity/` | model-side entities |
| `entry/` | course catalog, aliases and global configuration |
| `event/` | event abstractions |
| `services/` | event classification, workload, semantics and lifecycle |
| `settings/` | model, routing and visualization defaults |
| `utils/` | shared alert/event helpers |

The production runtime imports these packages through the container's shared project path.

## 3. Production runtime

`mindflow-bot-runtime/` contains Feishu message handling, participant binding and consent, Calendar OAuth and event operations, Agent session management, participant-bound tools, Forecast orchestration, Warning/Care scheduling, Daily Review, PostgreSQL persistence, Alembic migrations, Admin, research evaluation, deployment scripts and tests.

## 4. Agent boundary

The Agent is not given arbitrary shell, filesystem, browser, SQL or credential access.

The production Skill exposes a closed set of participant-bound Care and Calendar operations. Participant identity is resolved by the backend and frozen into the trusted execution context. The model does not choose arbitrary participant identifiers.

## 5. Main Forecast flow

```text
Participant
   │
   ├── Calendar
   ├── Explicit profile
   ├── Learned profile
   ├── Recent observations
   └── previous-day terminal state
          │
          ▼
ForecastCoordinator
          │
          ├── event classification
          ├── semantic enrichment
          ├── workload features
          └── active model resolution
          │
          ▼
AssessmentModel
          │
          ▼
Simulator
          │
          ▼
CTSSM trajectory
          │
          ▼
ForecastSnapshot
```

Forecast snapshots are persisted and versioned. Admin historical reads use persisted forecasts rather than silently recalculating the past.

## 6. Calendar consistency

Calendar mutation is treated as a consistency problem rather than a simple API call.

A mutation can invalidate the affected day's Forecast, unsent warnings, and a dependent next-day Forecast when the previous day's terminal state changes.

The runtime therefore uses fail-closed invalidation and durable reconciliation for remote Calendar mutation outcomes.

## 7. Observation consistency

A newly committed momentary observation can change the information set used by the current forecast.

The runtime commits the observation, invalidates affected current Forecast/Warning state, schedules a bounded refresh, and coalesces rapid repeated updates. Duplicate idempotent submissions do not create duplicate state transitions.

## 8. Daily Review

Daily Review is a retrospective pipeline, not a rewrite of the original Forecast.

It stores retrospective responses, causal source Forecast identity, retrospective curve and reconstruction provenance.

The system distinguishes causal rebuild from later reanalysis using newer facts. Later reanalysis does not replace the historical causal retrospective.

## 9. Research data layer

Research evaluation freezes immutable dataset snapshots so later database changes do not silently alter an earlier experiment.

The data layer includes observations, forecasts, forecast currentness, calendar snapshots, psychometrics, explicit profiles, slow state, learned profiles, Daily Review, Warning/Care exposure and promotion evidence.

## 10. Admin architecture

Admin is an independent Starlette process backed by the same PostgreSQL database and business services.

The UI is read-oriented by default. Mutating operations such as forecast refresh, retrospective rebuild, dataset creation or model promotion are permission-gated.

Pressure and workload chart images are rendered server-side; complementary Admin tables and client-side visualizations consume the same reviewed backend contracts.

## 11. Deployment topology

Production uses Docker Compose with separate services for:

```text
postgres
migrate
bot
admin
claude-state-init
```

Acceptance uses an isolated test image and an explicitly dedicated PostgreSQL test target.

## 12. Design invariants

1. participant identity is backend-bound;
2. candidate research state cannot silently become production-active;
3. historical analysis respects knowledge time;
4. Calendar/Observation changes fail closed before refresh;
5. retrospective analysis does not rewrite original forecasts;
6. Care effectiveness does not claim causality without a causal design;
7. production and evaluation use the same reviewed mathematical core.
