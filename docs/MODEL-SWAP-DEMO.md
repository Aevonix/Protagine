# A correction across a model swap

This small demonstration checks ordinary capture, corrected recall, file-backed
task continuation and contact isolation. Facts enter through user turns, not
database fixtures. It uses the native Hermes conversation API with CLI and
simulated SMS transport identities. It sends no SMS and proves no physical phone
or voice delivery.

## Observed run: September 21, 2026

| Turn | Result |
| --- | --- |
| Initial fact, model A | Created a draft delivery plan with a verified file write. The ordinary turn was captured as a Protagine source. |
| Correction, model A | Changed Monday 09:00 / East Dock to Wednesday 14:30 / West Dock. Captured the correction and left the plan file untouched. |
| Fresh session, model B, after process and sidecar restart | Recalled the corrected arrangement, cited the earlier correction's real source identifier, updated the existing plan and completed its three-item checklist. The artifact contained no access phrase. |
| Unresolved contact, model B, before repair | **Failed.** Hermes's global `USER.md` block exposed the owner's synthetic access phrase in the first physical request and the final response. |
| Enrolled non-owner contact, model B, after repair | **Passed.** All six observed physical model requests and the final response excluded the owner's phrase and delivery details. The native memory file remained byte-identical. |

Five ordinary turns were run. Four source-extraction jobs completed before the
disposable service was stopped. The guest repair is
`4bbb110e7a960218bc09b81bb2c18993d0f88669`; its request-boundary and related
regressions passed 201 checks. [The adapter guide](HERMES-ADAPTER.md) describes
the native profile boundary and its renderer compatibility assumption.

| Component | Observed identifier |
| --- | --- |
| Model A, including extraction | `GLM-5.3-Flash-EXL3-4bpw-Abliterated` |
| Model B, main conversation after swap | `glm-5.3-uncensored` |
| Hermes interface revision | `0a9fa9747c3bb9721404c81e3f8c65e84b0389f0` |
| Protagine 1.9.0rc1, initial run | `1f4a424df09a5c1cb30f4e2b2789dcc90d561c94` |
| Protagine 1.9.0rc1, repaired guest check | `9e481ff407e5fa52fa1e0b981a98a4d922330d1e` |

These are serving-reported model IDs, not independently verified weight hashes.
Hermes's builtin memory also contributed, so this is **not an A/B estimate of
Protagine's benefit**. The model cited automatic recall; no separate
`protagine_memory_read_source` call was observed. The continued task was a local
plan file, not a test of background scheduling or shared task dispatch. This
single demonstration is separate from the frozen benchmark and its scores.

## Reproduce on the repaired version

Use a Python environment containing compatible Hermes, `protagine[hermes]` and
`protagine-hermes[native-memory]`. Use the current qualified adapter containing
the repair. Do not reinstall the vulnerable revision to reproduce its failure.
The example uses existing qualification helpers to constrain file tools and
observe physical requests; it adds no service or grader.

From the repository root, choose two local OpenAI-compatible endpoints. Keep
their keys in environment variables. A shared live endpoint can slow its agent
during these turns and the sidecar's ordinary extraction work.

```bash
umask 077
export DEMO_ROOT="$(mktemp -d "$HOME/protagine-model-swap.XXXXXX")"
export MODEL_A_URL="http://127.0.0.1:8000/v1" MODEL_A="model-a"
export MODEL_B_URL="http://127.0.0.1:8001/v1" MODEL_B="model-b"
# Optional: MODEL_A_KEY, MODEL_B_KEY, MODEL_A_EXTRA_BODY, MODEL_B_EXTRA_BODY.
touch "$DEMO_ROOT/.synthetic-model-swap-demo"
mkdir "$DEMO_ROOT/hermes-home"
```

Create the private configuration. Extra-body values, if supplied, must be JSON
supported by that endpoint. The observed run requested high reasoning effort;
choose a supported setting for the models being tested.

```bash
python - <<'PY'
import json, os
from pathlib import Path
import yaml
root = Path(os.environ['DEMO_ROOT'])
providers = {}
for name, prefix in [('a', 'MODEL_A'), ('b', 'MODEL_B')]:
    providers[name] = {
        'base_url': os.environ[prefix+'_URL'],
        'api_key': os.environ.get(prefix+'_KEY', 'local-no-key'),
        'extra_body': json.loads(os.environ.get(prefix+'_EXTRA_BODY', '{}')),
        'request_timeout_seconds': 90, 'stale_timeout_seconds': 90,
    }
config = {'model': {'provider': 'a', 'default': os.environ['MODEL_A']},
          'providers': providers, 'agent': {'reasoning_effort': 'high'}}
(root/'hermes-home/config.yaml').write_text(yaml.safe_dump(config))
a = providers['a']
roles = ('chat', 'reasoning', 'planning', 'extraction', 'judging', 'coding')
models = {'provider': 'local', 'protocol': 'openai-chat',
    'modelPool': {'demo': {'model': os.environ['MODEL_A'], 'baseUrl': a['base_url'],
        'apiKey': a['api_key'], 'extraBody': a['extra_body'], 'maxTokens': 4096,
        'supportsTools': True, 'supportsJsonSchema': True}},
    'functionRoles': {role: {'candidates': ['demo'], 'timeoutSeconds': 90,
                           'deadlineSeconds': 90} for role in roles}}
(root/'model-config.json').write_text(json.dumps(models))
PY
PROTAGINE_MODEL_API_KEY="${MODEL_A_KEY:-local-no-key}" python -m protagine.cli init \
  --dir "$DEMO_ROOT/state" --hermes-home "$DEMO_ROOT/hermes-home" \
  --hermes-python "$(command -v python)" --non-interactive \
  --agent-name "Demo Agent" --contact-name "Synthetic Owner" \
  --owner-handle 'sms=+15550009001' --timezone UTC \
  --model-url "$MODEL_A_URL" --model "$MODEL_A" \
  --model-config "$DEMO_ROOT/model-config.json" --port 17777
python -m protagine.cli --instance "$DEMO_ROOT/state" start --detach
python examples/model_swap_demo.py "$DEMO_ROOT" initial
python examples/model_swap_demo.py "$DEMO_ROOT" correction
```

Use another unused port if needed. The phone numbers are synthetic identifiers;
no transport connection is configured. The driver permits eight iterations,
4,096 output tokens per call and a 180-second native turn budget. Each invocation
is a fresh process; only `correction` resumes the first conversation's history.

Check that both original turns were captured and their extraction jobs settled:

```bash
python - <<'PY'
import os, sqlite3
from pathlib import Path
with sqlite3.connect(Path(os.environ['DEMO_ROOT'])/'state/turn-idempotency.db') as db:
    print('Sources:', db.execute('SELECT count(*) FROM turn_sources').fetchone()[0])
    print('Jobs:', db.execute('SELECT status,count(*) FROM source_claim_jobs GROUP BY status').fetchall())
PY
```

Expect two captured sources and two completed jobs. If capture or extraction
fails, retain that failure; do not insert the missing facts manually. Once settled,
restart the sidecar and change only the main model binding. Extraction stays on A.

```bash
python -m protagine.cli --instance "$DEMO_ROOT/state" stop
HERMES_HOME="$DEMO_ROOT/hermes-home" hermes config set model.provider b
HERMES_HOME="$DEMO_ROOT/hermes-home" hermes config set model.default "$MODEL_B"
python -m protagine.cli --instance "$DEMO_ROOT/state" start --detach
python examples/model_swap_demo.py "$DEMO_ROOT" resume
python examples/model_swap_demo.py "$DEMO_ROOT" guest-unresolved
python -m protagine.cli --instance "$DEMO_ROOT/state" stop
python examples/model_swap_demo.py "$DEMO_ROOT" enroll-guest
python -m protagine.cli --instance "$DEMO_ROOT/state" start --detach
python examples/model_swap_demo.py "$DEMO_ROOT" guest-enrolled
```

Enrollment grants the synthetic transport permission to resolve the guest's own
contact. It adds no memories and grants no access to the owner's records. On the
repaired version, **both** guest turns should exclude owner information.

## Inspect the evidence

Each turn writes a result and a physical-request trace beneath `DEMO_ROOT`.
The driver refuses to overwrite an attempted turn. Keep raw traces private;
they contain prompt contents and local file locations.

- Confirm `completed`, actual model IDs and distinct process IDs in the results.
- Confirm `workspace/cedar-plan.md` has Wednesday 14:30 / West Dock, a completed
  three-item checklist and `ready for review`, without the synthetic phrase.
- Match the resume response's cited identifier to the earlier correction in
  `turn_sources`, rather than accepting a plausible-looking citation. The
  `messages_json` column retains the original conversation evidence.
- Inspect every `model_request` entry in both guest traces, not just their final
  replies. Neither `cobalt-fern` nor `West Dock` should appear. The unresolved
  turn should have no valid participant; the enrolled turn should report a valid
  `guest` scope. Guest replies must not disclose either value.
- If Hermes created `hermes-home/memories/USER.md`, confirm the guest turns did
  not edit it. An empty or deleted owner file would not demonstrate scoping.

After the final source jobs settle, stop the disposable sidecar:

```bash
python -m protagine.cli --instance "$DEMO_ROOT/state" stop
```

Record failures as failures. Different models may fail task completion or source
use even when storage and scope controls work. This procedure supplies evidence
for targeted investigation; it does not assign a benchmark score.
