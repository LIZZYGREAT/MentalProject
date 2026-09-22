# Representation Revision Log instructions

Create `representation_revision_log.jsonl` only after all four calibration
annotations and disagreement reports are available. Each JSON line must validate
against `schemas/revision_log.schema.json` and record one construct-level human
decision (`KEEP`, `REVISE`, `SIMPLIFY`, or `DROP`).

At minimum, resolve every construct required by the Manual Ready gate. Link the
decision to disagreement IDs and changed manual sections. Do not use majority vote
as the reason, and do not mark a decision resolved before a named reviewer has
examined the evidence spans.

Example shape (illustrative only; do not copy as a completed decision):

```json
{"revision_id":"REV-EXAMPLE","construct":"U_CONTEXT","decision":"REVISE","reason":"Explain the observed boundary confusion here","source_disagreement_ids":["DIS:..."],"changed_manual_sections":["A10"],"reviewer":"<human reviewer>","reviewed_at":"<ISO-8601 timestamp>","status":"RESOLVED"}
```
