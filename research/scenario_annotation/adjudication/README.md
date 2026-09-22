# Adjudication boundary

`disagreement_queue.jsonl` is produced by analysis. A human reviewer then records
reasoned decisions and creates `gold.jsonl` using the adjudication schema.

Majority vote is not Gold. Formal scenarios become reference records only after
review of evidence spans, scenario ambiguity, manual ambiguity, construct overlap,
and label granularity. This directory intentionally contains no generated Gold.
