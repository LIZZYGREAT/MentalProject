# MindFlow Data and Privacy Boundaries

## 1. Scope

MindFlow processes longitudinal research data that may be sensitive.

The system separates participant identity, research state, model parameters, Agent context, credentials and operational logs.

## 2. Participant identity

The internal join key is `participant_id`.

External platform identities are resolved by the backend. The model and participant-bound tools do not accept arbitrary participant identifiers as normal model-controlled parameters.

This prevents the Agent from selecting another participant's data context.

## 3. Data layers

### Momentary state

Examples include stress, energy/vitality, current activity, workload and perceived control.

### Retrospective state

Daily Review provides retrospective anchors and qualitative context. It does not overwrite the original Forecast.

### Explicit profile

Information explicitly provided by a participant. Model-inferred values should not be written back into this layer as if the participant stated them.

### Psychometrics

Standardized assessment history is append-only and versioned.

### Slow state

Derived recent longitudinal aggregates such as workload/recovery context.

### Learned model parameters

Auditable parameter snapshots with evidence windows, uncertainty and validation status.

## 4. State time and knowledge time

MindFlow tracks both when the state/event happened and when the system learned the record.

This distinction is necessary for causal historical analysis. A record created later cannot be injected into an earlier evaluation simply because its event timestamp points to the past.

## 5. Forecast vs retrospective analysis

Original Forecast snapshots are retained.

Daily Review creates a separate retrospective estimate.

Admin distinguishes causal reconstruction from later reanalysis using more recent facts. Later reanalysis must not silently rewrite the historical model output.

## 6. Credentials

Credentials belong to the backend configuration boundary.

Examples include Feishu application secrets, DeepSeek API keys, PostgreSQL passwords, encryption keys and OAuth tokens.

These values must not be placed in Git-tracked `.env`, prompts, tool schemas, public logs or research exports.

`.env.example` should contain placeholders, not live credential values.

## 7. Agent permissions

The constrained Agent environment denies general shell execution, arbitrary file read/write, arbitrary web access and unrestricted subagents.

The Agent receives only the approved Skill/tool surface.

## 8. Calendar OAuth

Calendar authorization is participant-scoped.

External application identities such as Feishu `open_id` are app-scoped and are not used as a universal cross-application research identity.

## 9. Research exports

Research datasets should be de-identified and reproducible.

Recommended export rules:

- use participant codes or derived research IDs;
- exclude raw OAuth tokens and application secrets;
- exclude direct identity mappings;
- preserve record/version/provenance IDs needed for audit;
- preserve state/knowledge timestamps;
- preserve model and dataset schema versions.

## 10. Clinical boundary

MindFlow is not a clinical diagnostic system.

Admin risk labels are internal research attention signals. They should not be represented as psychiatric diagnoses, validated clinical risk scores or evidence of a mental disorder.

## 11. Causal boundary

Observed changes after Care are not automatically caused by Care.

The current system explicitly marks Care effectiveness analysis as observational. Any future causal claim requires an appropriate causal design and analysis.

## 12. Repository hygiene

Before public release:

1. keep `.env` ignored;
2. keep runtime database files ignored;
3. scan Git history for secrets;
4. rotate any credential that was publicly committed and remains valid;
5. do not paste scanner-discovered secret values into public issues or logs;
6. verify public example profiles contain no direct personal identifiers.
