# Native coding effects

This pack checks whether Hermes actually edits a synthetic repository and whether the resulting code works. It does not grade a selected patch answer or accept a claim that tests passed.

The development pack contains six small Python repositories: pagination, timezone ordering, retry schedules, exact invoice arithmetic, CSV handling, and configuration precedence across files. Each includes repository instructions and a visible smoke test. Independent executable checks remain outside the agent's messages and editing container. The six-task held-out pack is operator supplied and must remain outside public source control until the comparison round is finalized.

These tasks cover small repairs, edge cases, one change across multiple files, repository instructions and edit scope. They do not establish performance on large repositories, every language, interrupted coding sessions or production deployment.

## Execution

The existing qualification runner owns runs, immutable attempts, deadlines and outcomes. The adapter invokes the selected native Hermes runtime with its real file tools, terminal tools and conversation loop. The model sees only synthetic files and the task. Protagine memory and production channel access are intentionally absent from this coding boundary.

Each attempt receives an owned offline Docker container. The selected image must already exist and be pinned by digest. The container has a read-only root, a writable temporary workspace, no host mounts, no GPU, a nonroot user, and limits of one CPU, 1 GiB RAM and 128 processes. The trusted controller may use a Unix socket forwarded over SSH; the container never receives this socket or provider credentials. A CPU Python image with an empty entrypoint is recommended. The Hermes resource probe runs the selected image before the task, so inference entrypoints are unsuitable.

Hermes automatically discovers skill/cache mounts. A fixture-scoped adapter suppresses that asset discovery and verifies Docker's actual mount list. It does not replace the native tool executor or model transport. A second fixture wrapper supplies the native conversation's documented `task_id` so the loop uses the container that received the repository. Both adaptations live in this test process and make no runtime source changes.

After the native loop exits, the controller copies bounded source files to a private immutable `coding-artifact.json`. A fresh offline container then imports the edited implementation and runs independent function calls. Expected values remain outside both containers. No generated code executes on the controller host. Protected file changes, missing edits, missing native tool calls, failed checks or unconfirmed container cleanup prevent a pass.

Timeout cancellation terminates only the owned worker. Cleanup is confirmed before the runner removes attempt state. If cleanup cannot be established, state is retained and the batch stops. Verifier state records the owned container ID for inspection.

## Campaign API

```python
from protagine.qualification.coding import (
    load_pack, cases, recipe_metadata, CONSUMERS, EVALUATORS,
)
from protagine.qualification.native import configuration, native_context

pack = load_pack()  # Or an explicit private held-out JSON path.
sandbox = {"image": "python-image@sha256:<digest>", "docker_host": None}
selected, recipe = configuration(config_path, binding, hermes_python=python)
recipe.update(recipe_metadata(pack, sandbox))
suite = cases(pack, sandbox, deadline_seconds=480, max_output_tokens=8192, max_iterations=18)

def router(case):
    context = native_context(selected, recipe)
    context.coding_oracle = case.oracle
    return context

# Pass suite, CONSUMERS, EVALUATORS and router to qualification.runner.evaluate.
```

A shard contains up to six tasks to stay within the runner's declared 3,600-second suite budget. Consolidated campaigns should import these factories and use ordinary qualification runs, rather than introducing another scheduler. A standalone entrypoint is available:

```sh
python -m protagine.qualification.coding --list
python -m protagine.qualification.coding \
  --config /private/benchmark-config.yaml --binding candidate \
  --hermes-python /path/to/hermes/python \
  --image python-image@sha256:<digest> --output /private/coding-run
```

The container image, fixture pack and adapter sources are hashed into private recipe metadata. Do not copy this metadata directly into public reports: Docker socket paths, source files, task bodies and hidden checks are private. Use the existing allowlisted public exporter. Native attribution remains unverified unless the transport separately observes the actual requested and returned model identity.

## Validation

`test_qualification_coding_effects.py` validates development defects against hand-written repairs and checks that scope violations or unproved claims fail. Only these trusted fixtures execute locally.

`test_qualification_coding_container.py` is an opt-in integration test. Set `PROTAGINE_TEST_CODING_SANDBOX_JSON` to an owned sandbox descriptor and `PROTAGINE_TEST_HERMES_PYTHON` to the selected interpreter, with Docker on `PATH`. A controlled loopback HTTP transcript drives the real Hermes loop through native edits and terminal execution, followed by independent container checks. Its evidence mode is `controlled`. It makes no model calls and is not a model score.

The pack contains twelve distinct development plus held-out tasks. A campaign that already includes a coding screen case should select eleven pack tasks toward its twelve-case category and label the remaining task supplemental. Preserve this choice in the frozen manifest.
