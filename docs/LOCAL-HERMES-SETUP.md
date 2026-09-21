# Create or attach a private Hermes agent

The **1.9.0rc1 release candidate** prepares official Hermes **0.21.3** plus the
[packaged compatibility patchset](HERMES-HOOK-COMPATIBILITY.md). You need Python
3.12, Git and one local OpenAI-compatible chat endpoint. No fork is required.
The new runtime preparation commands are not available in the published 1.8
packages. Phase 1 remains in development and validation.

Follow the [Get started commands](../README.md#get-started) from the candidate
checkout, then run `protagine init --prepare-hermes`. Setup fetches the qualified
official source, applies exact patches in a separate directory, installs native
dependencies and checks capabilities before attachment. No manual source edits
are needed. Models, containers and machine services are not downloaded by this
runtime preparation, and the existing Hermes installation stays intact.

To use an existing official checkout, pass its `--hermes-python`. Missing core
interfaces trigger preparation from that clean, supported source. An unknown
revision or tracked source modification is rejected; setup does not silently
substitute an older version. Explicit `--prepare-hermes` selects the documented
qualified revision.

Use a Python version supported above. Replace the interpreter placeholder with
the Python from the Hermes runtime you actually run. The sidecar, Hermes adapter
and hostworker are versioned independently; an adapter-only release does not
require a sidecar version change. Follow the component versions and compatibility
notes in the [changelog](../CHANGELOG.md), rather than forcing their version
numbers to match. `protagine init --help` lists the wizard's optional and
unattended flags.

A separate Hermes environment needs its own native core dependencies. It does
not need Protagine's CLI dependency `typer` or a preinstalled Protagine adapter; setup
can attach the private adapter directly from the Protagine environment.

The wizard asks for your name, optional owner messaging accounts, the agent's name, the model API root and model,
whether to enable accepted local drafts, whether to enable general native tasks
(default no), then whether to start Protagine. An API
key is prompted without echo. For unattended
setup use `PROTAGINE_MODEL_API_KEY` in the process environment, never a command-line
key. A model that requires no key works too. Fresh model configurations use Hermes's `custom` provider with the selected endpoint; existing model configuration is preserved.

```bash
protagine init --non-interactive \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --hermes-home "$HOME/.hermes-orion" \
  --agent-name Orion --contact-name Owner \
  --model-url http://127.0.0.1:8000/v1 --model my-local-model --local-work --start
```

`--hermes-home` wins over `HERMES_HOME`. Without either selection, guided setup
lists live profiles through the selected Hermes runtime and asks which home to
attach. Listing reads profile names and paths, not their private configuration.
Noninteractive setup retains the `~/.hermes` default. Only the selected home's
configuration is inspected or changed. `--dir` selects private Protagine state,
otherwise the selected instance or `PROTAGINE_STATE_DIR` is used. New instances
default to `<selected Hermes home>/protagine`. Both the selected Hermes home and Protagine
state must stay outside Git checkouts, including when `--dir` is separate.
Use the private instance's selected interpreter and home when launching Hermes:

```bash
protagine --instance "$HOME/.hermes-orion/protagine" hermes run gateway run
```

For an existing managed gateway, update its interpreter through the deployment's
normal lifecycle after validation. Do not start a second gateway alongside it.
Preparation and attachment never restart the existing gateway or silently edit
its service command.

Setup checks native runtime capabilities, canonical adapter resources, the
selected model with one neutral completion, the free local sidecar port, and
configuration conflicts before it writes the private instance. `--adapter-wheel`
can select an already-built canonical wheel instead of an installed
`protagine-hermes` distribution. This is also usable from built Protagine wheels with
no editable source checkout.

The selected model hostname is recorded for runtime routing, and setup checks
its addresses with the router's existing local-network rules. A LAN hostname
can therefore serve extraction as well as the initial chat probe. Runtime calls
continue to resolve and check that configured host when its address changes.

## Prepare or inspect Hermes separately

```bash
protagine hermes prepare \
  --source /path/to/official/hermes \
  --destination /path/to/new/runtime
protagine hermes check /path/to/new/runtime
protagine init --hermes-python /path/to/new/runtime/.venv/bin/python
```

Omit `--source` to fetch the exact official revision qualified by the packaged
patchset. Omit `--destination` to use its directory under
`~/.local/share/protagine/hermes/`. Preparation copies tracked official source,
checks patch preimages and postimages, installs an isolated environment and
records its source and capabilities. Untracked files and private profiles are
not copied. A failed candidate stays unselected. An existing successful candidate
is rechecked before reuse.

Each Hermes update needs a patchset qualified against that exact official
revision. Release CI applies the built wheel's patches and runs the full adapter
suite plus the native patch tests in separate processes. The daily latest-stable
check tests unlisted revisions in a separate qualification directory when exact
patch preimages match; conflicts name the affected files. It does not upgrade,
downgrade or restart a deployment. Upstream acceptance is not required.

## Preserve an explicit model configuration

For a new instance, add `--model-config /private/path/models.json` to the setup
command. This accepts the same JSON host-model configuration as the runtime:
`models`, `modelPool`, `functionRoles`, `taskRoles`, local hosts and networks,
capabilities, request extras, credentials, output limits and role deadlines.
Setup validates it before model probes or instance writes and stores the complete
object in the private instance's `.protagine-llm-config.json` with mode `0600`.
Keep the input file private too; never commit credentials.

`--model-url` and `--model` still select the wizard's chat connectivity probe and
the initial Hermes model when that home has none. An existing Hermes chat
configuration stays selected. The supplied JSON configures Protagine's cognition
roles separately; setup does not map that chat model over the supplied roles.
Without `--model-config`, the simple wizard configuration is unchanged.

With `--local-work`, `--native-reviews` or `--ordinary-skill-review`, supply an explicit tool-capable local
`planning` role. Setup uses it for the native worker and checks its selected
model's function calling. It preserves the rest of the model pool and role
settings. `--native-goals` retains the selected Hermes profile's model. These
connectivity checks do not qualify a model's memory, planning or answer quality;
the installed instance remains `configured_not_behaviorally_verified`.

The option is only for new instances. Existing instances retain their model
configuration; edit that private runtime configuration through its normal update
path. `--model-config` cannot be combined with `--preferences-only` or
`--skills-only`.

## Scheduled ordinary-skill review

The wizard offers ordinary-skill review separately from operational reviews,
default off. Opt in on a new or existing instance with:

```bash
protagine init --non-interactive --hermes-home "$HOME/.hermes-orion" \
  --ordinary-skill-review --skill-review-schedule '0 */6 * * *'
```

This adds one script job to that home's existing Hermes scheduler. Keep its
gateway running. It uses the current `planning` role and native review ledger;
setup adds no profile, service or separate review store. Without an evaluator,
review produces pending proposals. To permit qualified application, explicitly
select a private evaluator declaration with `--skill-review-evaluator PATH` and
set its `allow_apply` authority. For captured tool failures, the declaration must
also select their tool/error signatures in `native_failures`; unmatched failures
remain proposals. Native observation ancestry is preserved through measurement,
application and later audits. A configured evaluator does not establish that any
proposal has passed its checks. The evaluator scope and measured-update
contract are described in [the adapter guide](HERMES-ADAPTER.md).

The opt-in also enables the existing passive tool-failure capture in ordinary
resolved owner turns. Matching failures across distinct turns can supply one
review batch; qualification, system and review turns are excluded. Separately,
the same consumer can use complete task assessments explicitly submitted by a
host or evaluator. It does not automatically grade the factual quality of every
ordinary artifact. Neither a recurring tool error nor a submitted review proves
that a skill caused it or that a proposal will help.

The default cadence is every six hours. `--skill-review-schedule` accepts a
recurring native schedule. Existing enabled instances can update the schedule
or evaluator through the same command; an empty evaluator value (`''`) returns
to proposal-only review. Omitting all options preserves the existing choice.
`--refresh-adapter` refreshes an enabled cadence's launcher using the retained
schedule and evaluator. Upgrades that introduce this feature require refreshing
an older copied adapter before enabling it.

Use `--no-ordinary-skill-review` to remove only the recorded managed job and its
unchanged launcher. Other cron jobs remain intact. Setup retains the last job
identity and configuration in `instance.json`; locally edited managed jobs or
scripts must be reconciled before setup changes them. A failed activation leaves
the prepared job paused, and repeating the same enable command retries it.

## WhatsApp read receipts for a selected profile

For an existing Hermes home, preview and then apply the supported channel
preference without initializing an identity, attaching Protagine, probing a model
or restarting a service:

```bash
protagine init --hermes-home "$HOME/.hermes-orion" \
  --preferences-only --whatsapp-read-receipts on --preview
protagine init --hermes-home "$HOME/.hermes-orion" \
  --preferences-only --whatsapp-read-receipts on
```

Use `off` to disable receipts. Omitting the option preserves the existing
setting. Preference-only mode requires an existing `config.yaml` and rejects
instance/setup options. It does not require an Protagine instance manifest.
`--hermes-home`, then `HERMES_HOME`, then `~/.hermes` selects the profile in this
mode. The preview lists changed setting paths without displaying other config
values. Applying uses the existing atomic writer and retains the exact previous
config in a private `.config.yaml.protagine-backup-*` file; repeating an unchanged
choice adds no backup.

The preference is qualified against the current qualification build. It writes
native `send_read_receipts` settings in the profile's existing layout, including
existing alternate spellings that could override each other. Channel enablement,
sender/group policy, credentials and unrelated extras are retained. A profile
without explicit WhatsApp channel selection or existing connection settings is
rejected: adding preference-only extras can otherwise implicitly enable a stock
Hermes channel. Select `enabled: true` or `enabled: false` explicitly in that
profile's WhatsApp config first. An explicitly disabled channel stays disabled.
The option also works during ordinary init when that channel selection already
exists; ordinary init still performs its documented setup checks.

Use the selected gateway's normal reconnect/restart procedure to load the new
setting. This command changes config only. Native receipts apply to accepted
incoming messages and remain subject to WhatsApp account privacy behavior;
they do not retroactively mark a backlog as read. Read receipts do not imply
online status. Custom bridges may expose their own presence configuration.

## What the local profile enables

Ordinary native turns enter the durable outbox and canonical source ledger.
Source quotations, temporal claim projections, contacts, commitments, execution
observations and self state use the existing SQLite stores. Text recall uses
lexical retrieval; it needs no embedding model. The native memory provider
injects selected evidence before inference. Compression uses Hermes' native
checkpoint contract with durable local capture. General-plugin turn capture is
the single ordinary writer.

Ordinary sources also support cited appraisals and revisable working judgments.
They do not trigger a second legacy mood-analysis call or inject numeric mood
estimates into conversation context. Existing affect history and explicit affect
APIs remain available for inspection.

The selected model supplies the legacy SMALL, MEDIUM and LARGE role bindings.
Optional named role/capability configuration can refine that later. Fresh homes
also get that model in native Hermes config. An existing Hermes model is kept.

Accepted local drafts are optional. With `--local-work`, setup checks function
calling and creates a named `planning` role bound to the selected local model.
It creates a native `protagine-drafts` board and constrained worker profile.
The selected Hermes gateway dispatcher must be running.
The board remains idle until the owner accepts a specific question and local text
sources through `protagine_accept_local_draft`. No existing commitment is required;
an optional commitment ID associates the draft with a broader obligation.

The worker reads those sources, produces a cited draft and retains its execution
and report in the instance. It cannot send the draft or change its source files.
The gateway refreshes its planning profile from the current function role before
promoting new work and on nonempty dispatch ticks. A running attempt keeps its
selected snapshot; subsequent attempts use the refreshed profile. The
two environments need no shared dependencies. See
[accepted local work](ACCEPTED-LOCAL-WORK.md) for limits and cancellation.
If native registration fails after attachment, rerun the same command with
`--local-work`. It resumes the prepared profile using the retained planning
role, identity and credentials. Restart an already-running Protagine instance and
Hermes gateway to load the new binding.

Graph/vector retrieval, embedding downloads and consequential background workers
are disabled in this profile. The `graph` and `vectors` package extras retain
optional dependencies; installing them does not qualify those features or
enable them in this setup. Model quality still determines extraction and
reasoning quality. Lexical retrieval does not promise semantic recall of every
paraphrase. This setup is a growing local base, not a claim that every autonomous
behaviour or public channel is ready.

## Persistent native tasks

`--native-goals` is a separate opt-in for general background tasks, including
native `goal_mode` continuation. It works on a new attachment or an existing
private instance:

```sh
protagine init --non-interactive --hermes-home "$HOME/.hermes-orion" --native-goals
```

For a fresh attachment, also supply the interpreter, identity and model options
shown above. Unattended init without `--native-goals` or `--local-work` retains
the memory and observation profile. The interactive accepted-draft choice keeps
its existing default; general native tasks default to no.

Setup adds `kanban` to the existing profile's global toolsets and CLI selection,
preserving other selections, identity, model and authority configuration. Hermes
gates Kanban globally: enabling it can expose task tools to authorized turns on
other channels of the same profile even when those channels have saved tool
lists. Guest authority remains constrained by the existing Protagine middleware.
This is not blanket consent for consequential external actions.

The agent can use native `kanban_create` with the existing profile as `assignee`,
`goal_mode: true` and a chosen `goal_max_turns`. Setup prints that profile name
and selects the current native board plus the exact accepted-draft board, when
installed. An existing explicit `PROTAGINE_HERMES_WORK_BOARDS` list is retained;
other boards remain outside the observation view. No board is enumerated or
created by this opt-in, and no new worker profile or executor is added.

The selected Hermes gateway must be running to dispatch tasks. Protagine does not
start or restart it. For a new profile, run it in a separate terminal using the
same interpreter selected during setup:

```bash
HERMES_HOME="$HOME/.hermes-orion" \
  /path/to/hermes/.venv/bin/python -m hermes_cli.main gateway run
```

For an existing deployment, use Hermes' existing host lifecycle after configuration;
setup reports an explicitly disabled dispatcher configuration or environment
override instead of silently overriding it. Enabling config is not proof that
the gateway has acquired native dispatch ownership or completed a task.
Existing `HERMES_KANBAN_HOME` and `HERMES_KANBAN_DB` overrides must match the
selected native root and observed board; conflicting paths are rejected before
configuration changes.

An explicitly configured `auxiliary.goal_judge` is preserved. If its routing is
unset or automatic and the main provider/model are explicit, setup binds the
judge to that main configuration snapshot while retaining its limits. Later
main-model changes do not rewrite this explicit judge role. An implicit main
binding leaves automatic judging intact and is reported. Existing native
fallback behavior still applies: this setting does not establish global LAN
confinement. Use the existing Hermes auxiliary configuration to select another
judge later. Accepted local drafts keep their separate constrained worker and
planning role.

## Identity and authority

A real owner contact is created, with a generated exact-person API credential.
The server has no global legacy bearer key. The native local CLI is explicitly
bound to the owner. During creation you can also enroll your messaging accounts:

```bash
protagine init --hermes-python /path/to/hermes/.venv/bin/python \
  --hermes-home "$HOME/.hermes-orion" \
  --owner-handle telegram=123456789
```

Use the exact sender ID reported by the authenticated Hermes channel, not a
display name, group ID or another person's account. Repeat `--owner-handle` for
each account, or enter them in guided setup. Enrollment is a local owner decision,
not a claim accepted from a conversation. The existing private contact database
stores verified bindings; the credential remains restricted to that owner and
the enrolled transports. An unknown, unverified or other-person account does
not inherit the owner identity, and read resolution does not create contacts.
Enrolled phone and email handles use the contact store's existing normalization;
RCS handles share SMS storage, while transport admission remains explicit.

Hermes still owns channel credentials, sender admission and delivery. Enrollment
does not enable a channel, alter its allowlist, send a message or connect a device.
It is available when creating an Protagine instance, including attaching to an
existing Hermes home. Rerunning setup preserves identity and credentials;
passing enrollment flags to an existing Protagine instance returns an explicit error
instead of silently changing its owner. Other people and hardware require their
authenticated transport integration and scoped grants.
Native owner tools remain available; consequential effects remain subject to
the existing application consent rules. Public guest context needs the existing
scoped projection contract and is not enabled by this local profile.

Canonical adapter bytes are retained in private state. If the selected Hermes
interpreter already has both native Protagine entry points, setup verifies their
package bytes against the selected artifact and uses that installed package.
It records the loading mode, package version and source paths in `instance.json`.
A different or incomplete installed adapter is rejected before attachment;
upgrade it explicitly or select its matching artifact or another interpreter.
With a separate interpreter lacking those entry points, tiny profile-local
forwarders load the private adapter copy. These bindings are separate from
runtime preparation. Setup does not install competing forwarders that native
precedence would ignore. An explicit later
upgrade of a shared installed package affects every home using that interpreter.
Other profiles and running Hermes sessions are not restarted or modified by
attachment. Start a new Hermes session afterward.

For a new home, `SOUL.md` contains the chosen identity. An existing SOUL, channels,
model and unrelated settings are retained. An incumbent non-Protagine memory
provider requires an explicit wizard choice or `--replace-memory-provider`;
its data is retained. An existing Protagine directory adapter or native JSON config
requires an explicit upgrade rather than being silently replaced.

## Update an existing attachment

This procedure updates a current Protagine attachment. It is not a Protagine
migration or a guarantee that arbitrary old databases can be downgraded.

Stop the selected Hermes gateway and its workers using their existing host
lifecycle, then stop this Protagine instance (`protagine --instance /private/path stop`,
or `service stop` for a managed instance). Complete or cancel in-flight work
through Hermes before stopping it. Keep the private instance and Hermes home.

If changing Hermes, prepare and qualify its candidate before stopping the active
services. Select the new interpreter with `--hermes-python` during adapter refresh,
then update the managed gateway's command through its normal lifecycle. Retain the
previous interpreter for rollback. Preparation alone does not switch a service.

Fetch the release you intend to use into a source checkout, then update both
Protagine distributions from that checkout in the environment that runs Protagine:

```sh
python -m pip install --upgrade "/path/to/Protagine[native-memory]" "/path/to/Protagine/sidecar[hermes]"
protagine init --non-interactive --hermes-home "$HOME/.hermes-orion" --refresh-adapter
```

For a pinned deployment, select the same release tag for both packages.
A Hermes interpreter with installed Protagine entry points also needs that adapter updated in its
own environment before refresh. That package update affects all homes using the
interpreter. Refresh verifies those installed bytes and records the binding;
it does not copy a second active adapter or upgrade that installed adapter package itself.
Keep the attachment's existing loading mode. Refresh rejects a switch between
a package installed in Hermes and profile-local directory adapters before
writing anything.

The ordinary separate-environment attachment uses a copied adapter. Updating
Python packages alone does not update that copy. `--refresh-adapter` replaces
it with the selected canonical resources, updates the known profile and draft
worker manifests, and retains the previous directory as `adapter-previous-*`.
It retains identity, credentials, config, model roles, databases, native boards
and worker configuration. Local changes to managed adapter files are reported
before replacement. Repeating the same refresh leaves matching bytes unchanged.
The instance records the Protagine environment running this command; supply
`--hermes-python` only when deliberately selecting another supported native
interpreter. No model probe, service restart or new consent process runs here.
If the Protagine interpreter moved and this instance uses a user service, run the
existing `service install` command from the new environment while the service
is stopped, then `service start`. Refresh preserves the old service definition;
updating the instance manifest alone does not move the service interpreter.

Start Protagine and Hermes through their existing lifecycle, then check `status`
and `doctor` and recall a harmless fact from a new session. A package version
alone is not evidence that the attached code or retained memory works. On an
ordinary write failure refresh restores the files it changed. If the process
itself is interrupted, keep both runtimes stopped and restore the retained
adapter directory and corresponding `.protagine-backup-*` manifest files before
retrying. Database downgrade or rollback is outside this code-only refresh.

## Develop from a checkout

For source development, install both distributions from the public repository
root in your development environment:

```bash
python -m pip install . ./sidecar
protagine init --hermes-python /path/to/hermes/.venv/bin/python \
  --hermes-home "$HOME/.hermes-protagine-dev"
```

Keep generated profiles and private state outside the checkout. After changing
managed adapter code, use the same stopped-runtime refresh procedure above;
installing changed source packages alone does not update an existing copied
adapter. Source development is optional for a normal published installation.

## Start, observe and recover

```bash
protagine --instance "$HOME/.hermes-orion/protagine" start --detach
protagine --instance "$HOME/.hermes-orion/protagine" status
protagine --instance "$HOME/.hermes-orion/protagine" doctor
protagine --instance "$HOME/.hermes-orion/protagine" stop
```

With the selected `HERMES_HOME`, start/status/stop also discover the instance
from `plugins.protagine.instance_dir`. They never fall back to another instance's
`.env`. The private instance's `.env` governs startup; edit that file for lasting
changes. Its `sidecar.log` and process record belong to that instance. A busy port
is not permission to stop its occupant. Stop checks the recorded process's
creation time and command before signaling it.

`doctor` checks the selected local instance's files, configured model, HTTP
health and scoped source-job status using its client credential. It does not
require a global administrator credential, graph services or background-effect
workers. Reported extraction errors remain visible; a healthy diagnostic is
not a substitute for the real recollection check below.

For automatic restart and login startup, use the existing CLI's user-service
commands from the Python environment you want to run. First stop any detached
instance process. Installation enables the selected service but does not start
it; `service start` waits for both the manager's process and authenticated HTTP
health before reporting readiness.

```sh
protagine --instance /private/path service install
protagine --instance /private/path service start
protagine --instance /private/path service status
protagine --instance /private/path service stop
protagine --instance /private/path service uninstall
```

Linux uses `systemctl --user`; macOS uses a LaunchAgent in the logged-in user's
GUI session. Each label derives from the resolved instance directory. Definitions
and logs live under that instance's `service/` directory; the user manager gets
only a link to its definition. Commands never use sudo or change login policy.
Other instances retain their definitions, processes and data. Uninstall stops
and removes this instance's manager registration while retaining its private
state, logs and definition.

These are user services. A macOS LaunchAgent runs while that user is logged in.
A Linux user service's lifetime follows the existing user-manager policy; boot
before login or operation after logout requires an already configured lingering
user manager. The CLI does not enable lingering. See the native
[launchd lifecycle](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
and [systemd user service contract](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

The definition preserves the exact Python interpreter used for installation,
including its virtual environment. To change that environment, stop the service,
run `service install` with the new environment, then start it. The previous
definition is retained as `service/<name>.previous`, and a failed installation
restores its bytes. This does not roll back databases or application releases.
If startup fails, inspect the private `service/sidecar.log`; the manager remains
installed for recovery. An occupied port is an error, never permission to stop
its occupant.

The opt-in `sidecar/tests/test_instance_service_live.py` qualification creates
two disposable instances through the installed CLI and actual native manager.
It checks HTTP turn capture and later recall, kills only one newly created
service PID to verify manager recovery, verifies the second instance stays up,
and uninstalls both registrations while retaining their private data. Normal
unit runs skip this test. Set `PROTAGINE_TEST_USER_SERVICE=1`,
`PROTAGINE_TEST_SERVICE_PYTHON` to the installed Protagine interpreter, and
`PROTAGINE_TEST_HERMES_PYTHON` to a supported Hermes interpreter to run it explicitly.
It uses a loopback model fixture; this is process/persistence evidence, not a
model-quality or actual reboot test.

Verify the real loop: tell Hermes a distinctive harmless fact, exit, open a new
session and ask about it. Check the answer and retained source through the
scoped context view. HTTP health and configured modules are not a memory test.
CI performs that actual sequence using the built packages, full sidecar and
native Hermes against a disposable local streaming model fixture; it proves
capture and injection rather than judging a real model's recall quality.

Setup keeps the original config and environment in the private instance's
`hermes-original/` directory.
YAML values are preserved, but formatting/comments may normalize. New secrets
are kept in private files; generated config contains environment references.
On an attachment write failure, only the installer's exact written bytes are
undone. Concurrent owner edits are retained. Prepared private state remains for
inspection, with no running process restarted. Re-running a completed init
retains it; it is not an upgrade command.

To undo an attachment, stop this instance and Hermes, restore the original
`config.yaml` and `.env` from `hermes-original` (remove only wizard-created files
when no original existed), and remove the selected `plugins/protagine` and `plugins/protagine-memory`
adapters only if this setup created them in private-directory mode. Keep the private Protagine
state and Hermes transcripts. No database rollback is part of installation or
recovery. Compare files before restoring if you have edited them since setup.

Private identity comes from the guided setup and the agent's retained experience.
The obsolete `protagine seed` command and `/v1/host/seed` endpoint have been removed.
