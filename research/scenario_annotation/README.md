# Scenario Annotation (Stage 1)

This package is the offline research workflow defined by `docs/modeling/12_Stage1_Scenario_Annotation_Implementation.md`.

It is intentionally isolated from the production runtime, CTSSM, databases, and forecast path. Its job is to validate representation semantics before Event V2,appraisal, bot-representation, synthetic, or latent-dynamics work begins.

## Artifact boundary

- `scenarios/` contains only annotator-visible facts available by each scenario's
  `known_at_cutoff`.
- `hidden/` contains design metadata and is never copied into assignments.
- `annotations/` contains independent coder output.
- `adjudication/` is populated only after independent annotation and review.
- `manifests/` is populated only after all freeze gates pass.

No generator output is treated as Gold. No annotation produced by this package contains latent stress, EMA, support effect, or forecast values.

## Environment

Run commands from the repository root with the project interpreter:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli --help
```

The isolated package dependencies used by CI are pinned in `requirements.txt` and `requirements-dev.txt`; they do not install or start the production runtime, PostgreSQL, or the bot service.

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

Later sections document assignment, analysis, reporting, and freeze commands once their corresponding workflow parts are available.

Build the four calibration assignments (AI-A, AI-B, AI-C, Human):

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli build-assignments --round calibration --seed 12001
```

Each annotator receives a separately randomized Module A/B/C file and a manifest binding the manual and scenario versions. Natural/structured counterparts are counterbalanced so no annotator sees both in the same round.

Export one-scenario provider-neutral packets for an AI annotator:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli export-ai-packets --round calibration --annotator AI-A
```

The command creates one packet per Scenario × Module. A packet includes only the relevant manual section, that annotator's purpose-scoped scenario view, and the model-controlled output contract. Submit packets independently to different model families; do not expose another annotator's output, Human output, hidden metadata,analysis, or Gold.

Import a returned JSON object through the runner instead of editing system fields:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli import-ai-output --round calibration --annotator AI-A --module B --scenario-id CAL_019 --input returned.json --provider provider-a --model model-a --request-id request-001
```

The runner owns IDs, versions, annotator/round metadata, timestamps, and provenance. It records provider/model/settings/request/attempt and the raw-output digest, then validates the enriched draft against the exact assignment scenario. Transport-valid but incomplete output is rejected by the same completeness contract used by Human drafts, export, gates, and adjudication.

Start the Human calibration UI from the repository root:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.ui --round calibration --annotator Human
```

The server listens only on `127.0.0.1`. The requested round and annotator are resolved through the assignment manifest, including the selected Coding Manual and draft location; annotation mode has no reader for hidden design metadata, AI output, Gold, or analysis reports. Drafts are written atomically as one JSON file per Scenario × Module so work can be resumed without rewriting a shared JSONL file. The same command naturally supports `--round validation` and `--round reannotation` after their manifests exist.

When every scenario in a module is marked complete, export and revalidate the formal annotation documents:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli export-annotations --round calibration --annotator Human
```

The export refuses missing or incomplete drafts and validates target/evidence references against the exact Human assignment before replacing an output file.

After all independent annotation documents have passed schema validation, run the field-level analysis pipeline:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli analyze --round calibration
```

The pipeline writes `field_metrics.csv`, confusion matrices, semantic violation records and eligible-opportunity rates, an explicit Artifact Validity result, orthogonality checks, a disagreement/adjudication queue, construct-level Markdown reports, and `analysis_manifest.json`. The manifest binds the exact annotations, scenarios, hidden design inputs, threshold settings, manual content, bounded analysis implementation files, and Git revision using repository-relative paths. Gates and adjudication reject stale analysis after any bound input or analysis version changes. Reports include unit-level rater coverage as well as agreement.

Only after all four independent assignment sets have been exported and the full calibration analysis output exists, start the adjudication view:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.ui --mode adjudication --round calibration
```

It presents all independent labels and evidence, agreement counts, and related critical-violation flags. A reviewer must explicitly enter the final label,reason, and `KEEP` / `REVISE` / `SIMPLIFY` / `DROP` decision; the UI never preselects a majority label.

Build the complete Reference Set after adjudication:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli build-reference-set --annotations-dir <formal-annotations> --scenarios <formal-scenarios.jsonl> --adjudications <adjudication-dir> --assignments-root <formal-assignments> --analysis-manifest <gate-b-analysis-manifest.json> --manual <coding-manual.md> --output <reference-set.jsonl>
```

Before building the Reference Set, the command verifies that every assigned annotator submitted a complete module document. Each reference record records the actual and expected annotator counts. It writes `reference_set_manifest.json` next to the Reference Set, binding it to the source analysis manifest, annotation and assignment filesets, coding manual, and scenario files. Only full unanimity produces `UNANIMOUS`; every disagreement requires an explicit adjudication and produces `ADJUDICATED`. A majority vote never becomes Gold automatically.

Validate completed annotation documents against the exact visible assignment used by that annotator:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli validate-annotations B --scenarios research/scenario_annotation/assignments/round_calibration/ai_a/module_b.jsonl <annotation-files>
```

This validation checks record/document envelope consistency, target and evidence references, event-family subtype/lifecycle vocabularies, fact value types, participant evidence-span containment, and the bot response send-time boundary.

After human review has produced the revision log and an actual v1.0 candidate manual, evaluate Gate A:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli evaluate-manual-gate
```

The gate writes an auditable PASS/FAIL result. Missing AI-A/B/C/Human records, analysis outputs, construct decisions, or the revised manual are blocking.

After the 96-scenario blind round, evaluate Gate B and then freeze only if every required artifact exists:

```powershell
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli evaluate-semantic-gate
& 'D:\Miniconda\envs\MentalProject\python.exe' -m research.scenario_annotation.cli freeze-representation --manual-version 1.0 --scenario-version 1.0
```

The freeze command validates prior gates and their bound analysis manifests, the final manual, 72 Main + 24 Edge scenarios, complete target×variable Reference Set coverage, its source analysis binding, and final construct decisions. Its immutable manifest includes hashes for the manual, scenario corpus, Reference Set and its provenance manifest, quality thresholds, analysis inputs, and schema bundle. It will not overwrite an existing versioned manifest.

Artifact Validity is a strict PASS/FAIL gate for schema, hidden metadata, evidence integrity, temporal boundaries, envelope consistency, and structural double encoding. Semantic misunderstanding metrics are reported separately with their own eligible denominators. The settings currently require at least one eligible opportunity per metric as a zero-coverage guard. These floors are explicitly provisional, not a claim of adequate statistical power: Gate B remains blocked until the final formal Scenario Bank is designed and the floors are reviewed and marked `APPROVED_AFTER_FORMAL_BANK_REVIEW`. The project thresholds are versioned in `settings/quality_thresholds_v1.json`; they are engineering settings, not claimed as universal statistical laws.
