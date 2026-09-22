# Atomic annotation drafts

AI imports and the local Human UI store one JSON document per Scenario × Module
under an annotator/round/module directory. The tools write via a temporary sibling
and atomic replace; they do not continually rewrite one large JSONL file.

Drafts are not Gold and must not contain hidden metadata or another annotator's
labels. Use `export-annotations` to build the formal per-module artifacts only after
all assigned scenarios are complete and individually valid.
