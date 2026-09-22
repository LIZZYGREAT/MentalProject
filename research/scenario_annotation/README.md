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

Validate a scenario file:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli validate-scenarios research/scenario_annotation/scenarios/calibration.jsonl
```

Run the package tests:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m pytest research/scenario_annotation/tests -q -p no:cacheprovider
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

After all independent annotation documents have passed schema validation, run the
field-level analysis pipeline:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli analyze --round calibration
```

The pipeline writes `field_metrics.csv`, confusion matrices, critical violation
records and rates, orthogonality checks, a disagreement/adjudication queue, and
construct-level Markdown reports. It refuses to run on an empty annotation drop
and never edits the coding manual automatically.

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
