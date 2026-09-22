# Decision role screen 1

This public synthetic screen tests three proposed helper decisions. It does not
run Hermes, retrieve a memory, or establish that adding a helper improves an agent.
The cases and labels are AI-authored with explicit evidence rationales. No
candidate outputs were used as labels, and there has been no independent human
review. All labels were authored before any candidate inference.

| Family | Independent cases | Choices |
| --- | ---: | --- |
| Memory usefulness | 8 | useful, duplicate, irrelevant, protected |
| Retrieval need | 8 | current, history, both, clarify |
| Evidence-supported completion | 8 | completed, not_completed, unknown |

Memory usefulness classifies an existing candidate for the current request. It
does not decide whether a conversation deserves permanent retention. A protected
correction or forgetting instruction stays subject to deterministic processing.
Retrieval decisions concern which evidence is missing, not whether a tool call
succeeded. Completion requires a matching independent result; a promise, an
assistant assertion or a post-submission timeout cannot prove completion.

There are also eight fixed perturbations: two source-order changes, two request
paraphrases, three option-order reversals and one irrelevant distractor. Each
shares its original `group_id`. Report their consistency separately from the
24 independent base cases. Do not count 32 independent examples or select a
preferred variant after examining answers.

Two overflow probes put decisive evidence near the beginning or end of a long
packet. Grade them separately. A safe outcome is rejection or explicit abstention
before truncation, or a correct answer after accepting the entire packet. A larger
context model may legitimately answer. Neither a plausible answer after silent
truncation nor a service accepting a request establishes that all input was seen.

## Wire contract

Send only `state` and the two questions:

```json
{
  "state": "compact JSON serialization of the state object",
  "questions": {
    "decision": "the case's question object",
    "evidence": "the case's evidence_question object"
  }
}
```

The strings above describe fields; the actual `decision` and `evidence` values
are typed-choice objects with `type`, `instructions` and `criteria`. Preserve
declared option order. Both questions receive the same state. Keep all expected
labels, rationale, evidence mappings, family/group metadata and overflow contract
outside the model request.

Grade the main label and evidence label independently. Each evidence question
asks for concrete named records, such as the export receipt or the current and
earlier port records. It does not ask which records constitute the full logical
proof of the main classification. `evidence_options` maps each evidence label to
exact source IDs; compare the selected set with `evidence_ids`. This is
constrained source identification, not generated citation accuracy.
`relevant_ids` is provided only for memory usefulness: useful and
protected candidates are required; duplicates and irrelevant candidates are not.
The evidence answer does not change the main answer's grade.

Base packets are deliberately short. The runner must count state plus each full
question with the actual selected tokenizer or report that accounting as unknown.
Do not truncate to fit, tune a threshold on these cases, or treat the larger
overflow probes as ordinary accuracy failures. No probability threshold is frozen
by this pack. Confidence/calibration uses the returned label distribution, not a
separate API confidence field that could instead mean entropy or another metric.

## Freeze and interpretation

`manifest.json` pins the exact scenario bytes and records their origin and scope.
Keep v1 unchanged once inference starts; a substantive correction becomes a new
version. Preserve all failures and missing answers. Show base-family accuracy,
evidence selection, perturbation consistency and overflow behavior separately.

The first two families have two examples per label; completion has three
completed, two not-completed and three unknown examples. These tiny authored
counts do not establish calibrated probabilities, deployment reliability,
multilingual quality or downstream task benefit. An existing reranker is a
relevant comparator for candidate usefulness; it is not a substitute for all
three classification tasks. Adoption still requires a later paired native-agent
comparison with and without the optional helper.

## Capture and grade

`protagine.qualification.decision_screen.request_for(case)` builds the allowed
request fields. `request_hash(case)` identifies their exact bytes. An adapter
captures one record per case with `case_id`, `request_sha256`, `status`,
`answers`, `input_truncated` and `latency_ms`. Answers use the question keys
`decision` and `evidence`, each containing `choice` and `probabilities`.
Status is `ok`, `error` or `input_rejected`. A verified rejection before any
truncation also supplies `rejection_reason: input_would_truncate` and
`input_truncated: false`. Never infer absence of truncation from successful HTTP.

Grade a captured JSONL file without contacting any endpoint:

```sh
python -m protagine.qualification.decision_screen \
  --records attempts.jsonl --backend backend.json --output report.json
```

The backend file declares `id`, `revision` and `latency_boundary`; include the
hardware and serving recipe used. Output creation is exclusive. Duplicate
attempts and mismatched input hashes are rejected. Missing/error cases stay in
accuracy denominators. Evidence selection is graded independently of the main
decision. Calibration uses normalized label probabilities, accepting only small
rounding discrepancies, and reports how many answers were valid.

Unknown input coverage remains an explicit limitation beside observed answer
accuracy; it is not treated as proof of a complete-input comparison. Overflow
cannot pass on unknown coverage. Fixed confidence cutoffs illustrate error versus
coverage only. They do not select a policy or alter a live agent.
