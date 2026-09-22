# Operations boundary

The governed general plugin does not install anything from this directory.
These files are optional deployment diagnostics and local helpers.

- `protagine-doctor.py` performs static/read-only integration checks and reports a
  non-zero exit status on drift.
- `protagine-doctor-cron.sh` writes the doctor result to a local log. It does not
  send a message.
- `pre-restart-summary.py` reads the local agent log and Protagine timeline, then
  writes `~/.hermes/.post_restart_resume`; it is a local-state-writing summary
  helper and is not installed by the plugin.

Effects such as outbound notices, service restarts, or source changes must be
requested through the deployment's authenticated action plane.

Runtime qualification uses the packaged, versioned patch set described in
[PATCHES.md](PATCHES.md).
