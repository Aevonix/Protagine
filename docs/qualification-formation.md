# Source formation inventory

Eight additional fixtures exercise the actual canonical source ledger, `SourceClaimProjection`, admission review, store reopening and lexical recollection. They add four public development cases and accept four private held-out cases through the same versioned pack schema. No production code or memory policy is replaced.

Development covers correction of one independently mutable property while preserving another, retention of conflicting reports without inventing a correction, idempotent repeated source delivery, and an explicit negative property. Additional supported checks cover an event date distinct from report time, a complete conditional procedure, fallible transcript provenance and instructions embedded in source material.

The extraction candidate is selected only for `source_claim_extraction`. Admission review remains on its configured supporting role. Jobs get one processing attempt. A failure stops later source ingestion for that case and remains part of its result. Replayed delivery must create no source, new claim, processing job or model call.

Checks inspect stored values, exact evidence, source links, retirement relationships, explicit temporal fields and recalled provenance. A useful raw quotation cannot compensate for failure to form the requested durable fact. Equal reports remain separate unless an explicit correction or change supports retirement. Predicates need not match a particular string, while the fixture's literal value terms and required scope must survive. This is a conservative structured check, not a general semantic judge.

The audio fixture supplies a synthetic WAV and a separately supplied transcript through the normal media storage path. It measures whether derived recognition remains labelled unverified, retains asset/segment/recognizer/source lineage and preserves that distinction in recall. It does not run ASR, grade acoustic transcription, measure a voice call or verify that the waveform contains the supplied words. Native turn injection, transport capture, embedding and reranking are also outside this pack. Those boundaries stay explicit in each artifact.

## Campaign API

```python
from protagine.qualification.formation_extended import (
    cases, recipe_metadata, CONSUMERS, EVALUATORS,
)
from protagine.qualification.runner import evaluate, router_for

suite = cases()  # Append cases(private_heldout_path) for the complete inventory.
recipe.update(recipe_metadata(suite))

def router(case):
    return router_for(config, candidate_binding, [case])

# Pass suite, recipe, CONSUMERS, EVALUATORS and router to evaluate.
```

Each case has a 360-second deadline. All eight fit within the existing suite budget. Freeze the pack and implementation hashes before model calls. Keep private fixture bodies, oracles and raw artifacts outside Git and use the allowlisted public exporter for published findings.

`sidecar/tests/test_qualification_formation_extended.py` contains nine controlled tests of the real formation, replay and provenance paths, including failures where raw recall or a new value falsely suggests that a correction worked. Controlled transport responses establish harness behavior, not model quality.
