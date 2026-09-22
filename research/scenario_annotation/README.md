# Scenario Annotation (Stage 1)

This package is the offline research workflow defined by
`docs/modeling/12_Stage1_Scenario_Annotation_Implementation.md`.

It is intentionally isolated from the production runtime, CTSSM, databases, and
forecast path. Its job is to validate representation semantics before Event V2,
appraisal, bot-representation, synthetic, or latent-dynamics work begins.

## Artifact boundary

- `scenarios/` contains only annotator-visible facts available by each scenario's
  `known_at_cutoff`.
- `hidden/` contains design metadata and is never copied into assignments.
- `annotations/` contains independent coder output.
- `adjudication/` is populated only after independent annotation and review.
- `manifests/` is populated only after all freeze gates pass.

No generator output is treated as Gold. No annotation produced by this package
contains latent stress, EMA, support effect, or forecast values.

## Environment

Run commands from the repository root with the project interpreter:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli --help
```

The isolated package dependencies used by CI are pinned in `requirements.txt`
and `requirements-dev.txt`; they do not install or start the production runtime,
PostgreSQL, or the bot service.

Validate a scenario file:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli validate-scenarios research/scenario_annotation/scenarios/calibration.jsonl
```

Run the package tests:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m pytest research/scenario_annotation/tests -q -p no:cacheprovider
```

Verify that committed generated artifacts match a clean regeneration:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli check-deterministic-artifacts
```

Later sections document assignment, analysis, reporting, and freeze commands once
their corresponding workflow parts are available.

Build the four calibration assignments (AI-A, AI-B, AI-C, Human):

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli build-assignments --round calibration --seed 12001
```

Each annotator receives a separately randomized Module A/B/C file and a manifest
binding the manual and scenario versions. Natural/structured counterparts are
counterbalanced so no annotator sees both in the same round.

Export one-scenario provider-neutral packets for an AI annotator:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli export-ai-packets --round calibration --annotator AI-A
```

The command creates one packet per Scenario × Module. A packet includes only the
relevant manual section, that annotator's purpose-scoped scenario view, and the
model-controlled output contract. Submit packets independently to different model
families; do not expose another annotator's output, Human output, hidden metadata,
analysis, or Gold.

Import a returned JSON object through the runner instead of editing system fields:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli import-ai-output --round calibration --annotator AI-A --module B --scenario-id CAL_019 --input returned.json --provider provider-a --model model-a --request-id request-001
```

The runner owns IDs, versions, annotator/round metadata, timestamps, and provenance.
It records provider/model/settings/request/attempt and the raw-output digest, then
validates the enriched draft against the exact assignment scenario.

Start the Human calibration UI from the repository root:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.ui --round calibration --annotator Human
```

The server listens only on `127.0.0.1`. Annotation mode has fixed backend paths
for the Human assignment, Coding Manual, schemas, and Human drafts; it has no
reader for hidden design metadata, AI output, Gold, or analysis reports. Drafts
are written atomically as one JSON file per Scenario × Module so work can be
resumed without rewriting a shared JSONL file.

When every scenario in a module is marked complete, export and revalidate the
formal annotation documents:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli export-annotations --round calibration --annotator Human
```

The export refuses missing or incomplete drafts and validates target/evidence
references against the exact Human assignment before replacing an output file.

After all independent annotation documents have passed schema validation, run the
field-level analysis pipeline:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli analyze --round calibration
```

The pipeline writes `field_metrics.csv`, confusion matrices, critical violation
records and rates, orthogonality checks, a disagreement/adjudication queue, and
construct-level Markdown reports. It refuses to run on an empty annotation drop
and never edits the coding manual automatically.

Only after all four independent assignment sets have been exported and the full
calibration analysis output exists, start the adjudication view:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.ui --mode adjudication --round calibration
```

It presents all independent labels and evidence, agreement counts, and related
critical-violation flags. A reviewer must explicitly enter the final label,
reason, and `KEEP` / `REVISE` / `SIMPLIFY` / `DROP` decision; the UI never
preselects a majority label.

Validate completed annotation documents against the exact visible assignment used
by that annotator:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli validate-annotations B --scenarios research/scenario_annotation/assignments/round_calibration/ai_a/module_b.jsonl <annotation-files>
```

This validation checks record/document envelope consistency, target and evidence
references, event-family subtype/lifecycle vocabularies, fact value types,
participant evidence-span containment, and the bot response send-time boundary.

After human review has produced the revision log and an actual v1.0 candidate
manual, evaluate Gate A:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli evaluate-manual-gate
```

The gate writes an auditable PASS/FAIL result. Missing AI-A/B/C/Human records,
analysis outputs, construct decisions, or the revised manual are blocking.

After the 96-scenario blind round, evaluate Gate B and then freeze only if every
required artifact exists:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli evaluate-semantic-gate
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli freeze-representation --manual-version 1.0 --scenario-version 1.0
```

The freeze command validates prior gates, the final manual, 72 Main + 24 Edge
scenarios, formal Gold coverage, and final construct decisions. It will not
overwrite an existing versioned manifest.

Gate B separates zero-tolerance protocol violations from semantic misunderstanding
rates. The project thresholds are versioned in
`settings/quality_thresholds_v1.json`; they are engineering settings, not claimed
as universal statistical laws.
