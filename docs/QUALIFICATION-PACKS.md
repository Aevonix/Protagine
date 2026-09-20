# Portable behavioral benchmark packs

`protagine models packs` plans and runs the existing behavioral consumers. It uses the same evaluator, immutable attempt records, reports and batch inspection as the screening command. It does not deploy models, change agent configuration, discover private fixtures or add a learning service.

## Choose a pack

All counts below are executions in the public development pack. They are coverage inventory, not passing results. A selected subset retains its actual denominator. Held-out packs are separate explicit inputs.

| Pack | Development executions | Required resources | Measured boundary |
| --- | ---: | --- | --- |
| `formation` | 4 | Host config and candidate binding | Actual formation, admission review, correction, replay and lexical recollection |
| `perspective` | 4 | Host config/candidate; native config/fixed reader/interpreter | Durable opinions and appraisals, then native inspection or correction |
| `planning` | 2 | Native config/binding/interpreter; sandbox config | Tool planning with actual offline inventory effects |
| `recovery` | 6 | Native config/binding/interpreter; sandbox config | Tool errors, lost acknowledgment, stale revision and bounded recovery |
| `semantic` | 6 | Native config/binding/interpreter; supporting host config; retrieval config | Actual formation, semantic/lexical retrieval, native request injection and answers |
| `authority` | 4 | Native config/binding/interpreter; sandbox config | Verified synthetic sender identity, scoped context and native authorization |
| `identity-audience` | 5 | Native config/binding/interpreter | Identity linking, audience scope and separate context-exposure/disclosure checks |
| `interactive` | 11 | Native config/binding/interpreter | Native foreground/worker conversations and durable task transitions |
| `unified` | 6 | Native config/binding/interpreter | Four shared-work scenarios, plus two base-Hermes comparison arms |
| `router-recovery` | 2 | Host config/candidate and independent fallback binding | Actual router fallback and cooldown through owned fault proxies |
| `evidence` | 15 | Host config and candidate binding | Endpoint grounding, planning and long-context evidence |
| `interaction` | 5 | Host config and candidate binding | Endpoint visual evidence and fixed-transcript reasoning |

`unified` has four scenarios, not six independent scenarios. Its two comparison arms have distinct case IDs and `comparison_arm` labels. `interactive` adds different task transitions; it does not replace these controls. The screening, native-memory, native-learning and coding commands remain available with their existing contracts.

The manifests describe missing coverage. In particular, endpoint transcript reasoning is not speech recognition, native task APIs are not physical WhatsApp/SMS delivery, imported canonical sources are not native memory writing, and an observed fallback answer is not a primary-model pass. These packs do not establish full agent qualification.

## Freeze, execute, inspect

Plan reads configuration, hashes installed code and resolves case budgets without calling inference endpoints. Native packs inspect the selected Hermes installation. Sandbox packs also make a read-only query to the declared local Docker daemon to confirm the pinned image is already installed. Plan never downloads images or starts containers. It does not check model endpoint liveness.

```sh
protagine models packs plan \
  --pack formation --config /private/host-models.json --binding candidate \
  --label candidate-formation-01 --output /private/results/formation-01

protagine models packs run \
  --config /private/host-models.json --output /private/results/formation-01

protagine models packs inspect --output /private/results/formation-01
```

Use `--case-ids formation.duplicate-delivery-idempotence` at plan time for a bounded subset. Unknown or duplicate IDs are rejected. The run command takes resource paths again; pack, bindings, split, cases and labels come from the frozen manifest. A missing, changed or unnecessary resource is rejected before inference. Relocated files with identical content are accepted; a changed configuration, runtime, fixture or evaluator requires a new plan and output directory.

Add `--resume` to `run` to execute only untouched cases. Completed results remain unchanged. A started attempt without a result becomes interrupted and is not replayed. A batch lock prevents concurrent runners on one directory. Unconfirmed native cleanup stops the batch. Repetitions use new plans, not retries concealed inside an existing run.

The factories retain their existing case deadlines, output limits and iteration limits. The runner splits batches at 128 cases or 3,600 summed declared seconds, with at most 600 seconds per case. `declared_seconds` in the plan shows the total case allowance; it is not a latency estimate. Direct endpoint cases use the binding's configured output allowance. Changing allowances after observing a failure creates a different recipe.

## Native and supporting resources

`--native-config` is a Hermes YAML file with an enabled named provider, explicit endpoint and default model. `--native-binding` selects that provider. `--hermes-python` selects its installed runtime; it may be omitted only when the supplied configuration identifies an instance with an inspectable interpreter. No deployed home, contacts or memories are loaded. Provider credentials must be in the private configuration or explicitly named environment variables.

Native Protagine packs require the sidecar and both adapters installed in that interpreter. Semantic recall additionally requires `lancedb` and `pyarrow`. Planning and tool recovery need Hermes and the existing Docker backend. All native outputs need owner-controlled directory ancestry; the plan fails before source formation if that is unavailable.

For `formation`, `--config` contains the candidate and the fixed supporting admission reviewer. For `perspective`, it contains the candidate reasoning role plus the fixed extractor/reviewer. Existing task-role mappings for support calls are preserved. Only each case's declared candidate tasks are pinned. A support task mapped onto the pinned candidate role is rejected; assign it a separate supporting role before freezing the comparison. The supporting role may use the same model, but remains independently configured. Do not change supporting configuration between candidate comparisons.

The perspective native reader is separate from its reasoning candidate:

```sh
protagine models packs plan \
  --pack perspective --config /private/candidate-host.json --binding thinker \
  --native-config /private/fixed-reader.yaml --native-binding reader \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --output /private/results/perspective-01
```

The recipe records the fixed reader separately. Native reader answers do not become reasoning-candidate attribution. `--distinct-reader` declares an intentionally different processor for the portability case; that case still requires different observed returned identities. Without the declaration it remains unsupported, without making a request. Different labels alone do not pass it.

Semantic recall fixes extraction/review independently of the native candidate:

```sh
protagine models packs plan \
  --pack semantic --native-config /private/candidate-hermes.yaml --native-binding candidate \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --support-config /private/fixed-support-models.json \
  --retrieval-config /private/retrieval.json \
  --output /private/results/semantic-01
```

`retrieval.json` uses the existing semantic pack schema. URLs and models describe the deployment; they are not baked into Protagine:

```json
{
  "embedding": {"base_url": "http://127.0.0.1:8101/v1", "model": "installed-embedder", "dimensions": 1024},
  "reranker": {"base_url": "http://127.0.0.1:8102/v1", "model": "installed-reranker", "cutoff": 0.0}
}
```

Use the real embedding dimensions and deployed reranking cutoff. This pack currently supports explicit credential-free retrieval URLs; it does not add another credential or transport contract.

Sandbox config uses the existing coding sandbox schema: exactly `image` (an already installed `sha256:...` or repository digest) and `docker_host` (null for the local default socket, an explicit `unix:///...` socket or a forwarded `tcp://127.0.0.1:...` daemon). Sandbox workers retain the existing offline, owned-container restrictions. The plan does not change them.

Router recovery requires `--fallback-binding` in addition to the host candidate. Both bindings must exist and cannot alias the same endpoint/model pair. The candidate and fallback recipes stay separate; configured faults affect only the owned proxy, never a real server. Retain fallback successes as system-recovery evidence, not candidate reasoning gains.

## Explicit private holdouts

There is no fixture search path. `--fixture-pack` selects only that held-out pack, replacing the public development cases for this plan. It must match the pack's existing private schema and a predeclared canonical JSON digest:

```sh
python -c 'from protagine.qualification.records import digest, read; print(digest(read("/private/heldout.json")))'

protagine models packs plan \
  --pack evidence --config /private/host-models.json --binding candidate \
  --fixture-pack /private/heldout.json --fixture-sha256 YOUR_PREDECLARED_DIGEST \
  --output /private/results/evidence-heldout-01
```

The digest uses `qualification.records.digest`, not raw-file `sha256sum`. Freeze it before opening a comparison round. Every run/resume also requires the explicit fixture path. The `unified` pack has no held-out factory and rejects this option. Fixture bodies and independent oracles remain private; they are never added to the public registry.

`benchmark.json` freezes case/input/oracle hashes, split, resource/config hashes, selected native and supporting identities, installed implementation bytes and bounds. Child directories contain ordinary qualification `run.json`, attempts and numbered reports. These include synthetic inputs and private runtime information. Publish findings only through the existing allowlisted exporter, not by serving the result directory.

Controlled tests exercise CLI dispatch, the real router/evaluator, immutable resume, fixture selection, support-role separation and early resource/identity rejection. They validate orchestration. Actual model quality still requires authorized runs against the intended serving recipe.
