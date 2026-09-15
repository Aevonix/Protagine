# Native follow-up reviews

An expected reply belongs to an existing accepted commitment. Timing starts
from its actual dispatch receipt. No receipt means unknown delivery, not a
late reply. Reply, cancellation, expiry and source corrections remain in the
same commitment ledger.

With native reviews installed, the existing Hermes dispatch tick assigns a
due wait to the `protagine-reviews` profile. It uses the configured planning
role and the same two tools as operational reviews:

- `protagine_read_work_source(0)` reads current wait and parent state.
- Numbered sources open the listed canonical task evidence, with attribution
  and bounded excerpts. A truncated excerpt does not establish full coverage.
- `protagine_review_report` completes or blocks only that native review.

The reader verifies the current native task, run and claim. Reporting checks
the wait again. A reply or cancellation arriving during review supersedes
stale proposed report text with the current disposition. Completing a review
does not fulfill its parent commitment.

These workers cannot send messages. Existing prepared owner-authorized
follow-ups remain with the deployment's outbox, which independently checks
current task state, intake coverage and consent before delivery. Silence alone
is not evidence about a person's character.

The worker uses its selected root plugin's normal client configuration and
Hermes secret scope, including the root's `.env` in multiplexed deployments.
Deployments with a scoped credential loader may set
`plugins.protagine.native_reviews.client_factory_file` to an absolute private
Python file exporting `client(config) -> (connection, owner_contact_id)`.
The factory receives the selected root plugin configuration; its returned
owner must match the worker. No credential is copied into the review profile,
and a configured factory failure does not fall back to another credential.

Controlled native tests exercise dispatch, canonical reads, completion and
reply/cancellation races. They do not demonstrate human response prediction,
physical message delivery or a useful model-generated recommendation.
