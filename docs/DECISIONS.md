# Decisions: a fast decision layer

Some of what Protagine decides is a short typed decision over a few words: what the owner's reply to an
outreach says, whether a contact opted out, whether a new item is one the person wants no reminders about,
whether an interest is settled, whether an owner message judges the agent's work. Each has an answer today,
from a deterministic phrase table or from a model call that does more than this one decision. A
non-generative decision model ("System One") answers such a question in one forward pass, with a probability
per answer and no text to parse, in tens of milliseconds. `protagine.decisions` lets those decision points ask
one, and only where it was measured to be at least as accurate as the existing path.

## The contract

`POST <url>/v1/decide` (protocol `local-decision.v1`) with a `state` (the short text) and one typed question:
a `choice` among labels, each with a description, or a `noul` (yes/no). The answer carries a probability per
label, or the probability of yes. The client (`protagine.decisions.Decider`) returns a `Decision` (the label
and its calibrated probability) or `None`, and `None` always means: keep the existing path. It is `None` when

- no url is configured, or the point is not enabled;
- the input is longer than the model reads: a state over 1,200 characters (the model's context is 512 tokens,
  the question and its options included) is not sent, never cut to fit;
- the call takes longer than `timeout_ms` (250 ms by default; 1 to 5000 ms, and a value outside that from the
  environment, a non-finite one included, is the default), the endpoint is busy (429), failing or
  unreachable;
- the answer is not the typed answer asked for (another protocol, an input the model did not read whole
  (`input_truncated` not `false`), answers to other questions, other labels, a probability out of range, or a
  choice whose probabilities do not sum to one within 0.01);
- the calibrated probability falls inside the point's abstain band.

Calibration is temperature scaling: a probability p becomes p^(1/T), renormalised (for yes/no the log-odds are
divided by T). `abstain: [lo, hi]`: a yes/no answer is yes at or above `hi`, no at or below `lo`, and no
answer in between; a choice counts when its top calibrated probability is at least `hi`.

The model reads words; it grants nothing. At every point it can only do what the existing path could have done
with the same words, and only in one direction (below). Nothing it answers is logged with the text it read.

## Configuration

```yaml
decisions:
  url: ""                  # the decision model's endpoint; empty: every point keeps its existing path
  timeout_ms: 250
  points:                  # overrides of the measured defaults, per point
    owner_verdict: {enabled: false}                                    # turn a point off
    opt_out: {enabled: true, temperature: 0.33, abstain: [0.15, 0.85]}  # or on, after your own measurement
```

Defaults (the typed-decisions checkpoint's, below): `owner_verdict` on, temperature 0.25, abstain
[0.03, 0.97]; `outreach_reply` off (0.33, [0, 0.55]); `opt_out` off (0.33, [0.15, 0.85]); `no_reminders` and
`interest_settled` off (0.25, [0.1, 0.9]). They hold for that checkpoint only: another model needs its own
measurement.

`protagine.yaml` validates the section by name (a known point, a temperature above 0, `0 <= lo <= hi <= 1`)
and exports it to the sidecar as `PROTAGINE_DECISIONS_URL` (when a url is set), `PROTAGINE_DECISIONS_TIMEOUT_MS`
and `PROTAGINE_DECISIONS_POINTS` (JSON). A name already in the process environment wins, one name at a time:
an endpoint a service unit pins still runs under the file's time limit and points. The endpoint should be on a private network: the
states are the owner's and contacts' words.

## The points

| Point | Question | Existing path | What the model may change |
|---|---|---|---|
| `outreach_reply` | how the owner answers an unprompted message: engaged, dig deeper, not interested, not now, stop | the reactions phrase table (`mind/reactions.py`); a reply linked by its topic with no class is engagement | only a topic-linked reply the phrases read nothing in: it takes the model's class; a stop so read pauses outreach as a linked stop does (`reaction.read: decision`) |
| `opt_out` | does a contact ask for no more messages at all | the opt-out phrases (`contacts/optout.py`), and the appraisal's flag as a second net | only words the phrases miss: a yes lowers `may_contact` to `never`, audited as the model's reading without the words; never the owner |
| `no_reminders` | does the person ask not to be reminded about this item | the capture call's HOLD rule for a first mention | only a new item the call gave a due time or a heads-up: a yes creates it held (open, no time, no heads-up); a notice, check-in or cadence for a contact is never asked |
| `interest_settled` | does the owner say their question about this topic is answered or no longer wanted | the capture call's complete or cancel on a listed interest | only an open interest the turn names (`outreach.similar`) that the call left open: a yes settles it (`Mind.settle_interest`) |
| `owner_verdict` | does the owner's message judge or correct the agent's earlier reply, rather than ask for something new | the night's lesson call reports verdicts; the checks admit one that follows an agent reply and is quoted exactly | only an admitted verdict: a no withdraws it (`lesson_verdicts_vetoed`) |

At most three questions per point per captured turn.

## Measurements (2026-09-26)

The decision model measured is Laya (Apache-2.0; ModernBERT-large, 512-token context, SDK 0.3.4, model revision
`1c5edc17`), both its English root and its typed-decisions checkpoint, served one request at a time on one GPU.
The existing path's model calls ran on the evaluation model (GLM-5.3-Flash, 4-bit EXL3) on a shared endpoint.
Sets: 98 replies, 102 opt-out phrases, 30 reminder turns, 38 interest turns, 77 owner messages (345 in all; of
them 68 are `novel`). Five owner messages are left out of `owner_verdict`: the lesson call's answer did not
parse, on two tries each (the night would have learned nothing from them either).

Typed-decisions checkpoint (the defaults in `protagine.decisions` are this checkpoint's). Accuracy is correct/n,
with the repository's own items in brackets; ECE is the expected calibration error of the top answer;
"with fallback" is the composition the sidecar wires, held-out over five folds; helped / hurt counts the answers
it changed from the existing path's, right and wrong; T and the threshold are fitted on the whole set (1.01 is
"never act").

| Point | Existing path | Model alone, zero-shot / ECE | Model alone, T fitted / ECE | With fallback | Helped / hurt | T, threshold | Enabled |
|---|---|---|---|---|---|---|---|
| `outreach_reply` | 74/98 (66/68) | 62/98 / 0.25 | 62/98 / 0.11 | 71/98 (60/68; novel 11/30) | 3 / 6 | 0.33, 0.55 | no |
| `opt_out` | 89/102 (79/86) | 83/102 / 0.17 | 83/102 / 0.04 | 89/102 (75/86; novel 14/16) | 5 / 5 | 0.33, 0.85 | no |
| `no_reminders` | 27/30 (20/22) | 24/30 / 0.28 | 24/30 / 0.14 | 27/30 (20/22; novel 7/8) | 0 / 0 | 0.25, never | no |
| `interest_settled` | 37/38 (31/32) | 32/38 / 0.23 | 32/38 / 0.16 | 37/38 (31/32; novel 6/6) | 0 / 0 | 0.25, never | no |
| `owner_verdict` | 63/72 (56/64) | 67/72 / 0.29 | 67/72 / 0.10 | **71/72 (63/64; novel 8/8)** | 8 / 0 | 0.25, 0.03 | **yes** |

English root checkpoint, for comparison: `outreach_reply` 56/98 alone, 70/98 with fallback (hurt 7);
`opt_out` 62/102 alone, 84/102 with fallback (hurt 7); `no_reminders` 26/30 alone; `interest_settled` 35/38
alone; `owner_verdict` 69/72 alone (ECE 0.23, 0.03 with T fitted) but 65/72 with fallback (helped 5, hurt 3).
Neither checkpoint beats the phrase tables at a point they cover; the typed-decisions checkpoint is the one to
serve.

Latency. The decision model's own time was 16 to 18 ms at the median and under 19 ms at p95 for every point
(inputs up to 196 tokens). A round trip on the same private network was 19 ms at the median (20 ms p95); across
two extra network hops it was 37 to 42 ms (p95 86 to 103 ms), inside the 250 ms limit. The phrase tables take
under 0.2 ms. The model calls they would stand beside took 11 s (`interest_settled`), 23 s (`no_reminders`) and
35 s (`owner_verdict`) at the median on the shared endpoint, queueing included, and up to 80 s at p95; the
decision model adds its answer to those paths, it does not replace the call.

What the numbers say:

- `owner_verdict` is enabled. The lesson call reported eight of the owner's plain requests that follow an agent
  reply ("p-95 has a shipment of 52 kg going out. I need the fee.") as verdicts on the work, and missed one
  verdict. The decision model withdrew all eight and no real verdict, held out. It errs only toward learning
  less: a withdrawn verdict admits and scores nothing. It acts only when the calibrated probability of a
  verdict is at most 0.03 (about 0.30 before calibration); the requests measured 0.15 to 0.37 before
  calibration and the verdicts 0.36 and above, so the margin is narrow and the band keeps to the clear side.
- `outreach_reply` and `opt_out` stay off. The phrase tables are right on almost every phrase the repository
  labels, and where they say nothing the model more often invents a class ("I need to focus on the grant report
  this week" read as not now, "Drop it" and "Not interested" read as an opt-out) than finds a missed one. On
  the `novel` paraphrases it helps (opt-out 14/16 against 11/16), which is not enough to enable it.
- `no_reminders` and `interest_settled` stay off: the capture call gets most of them right, and the model is
  not sure enough of the few it misses to fix any without breaking others. The capture call gave a reminder to
  two no-reminder turns of the repository's own, the capture test's sentence ("I am handling it myself. No
  reminders about it.") among them, which the typed-decisions checkpoint put at 0.55 and 0.49 (the English
  root put the test's sentence at 0.79); and it left "that one is theirs and I do not want you on it" open,
  which the model put at 0.38.
- The existing paths' own misses the sets exposed: a contact's "Stop checking in on me", "Only message me when I
  ask" and five like them are an owner's stop cues (`mind/reactions.py`) but not contact opt-out phrases
  (`contacts/optout.py`); and a reply whose positive cue is aimed at a detail the outreach did not name ("Look
  into it further and find out its test method") is engagement, not "dig deeper".

The full results, the held-out errors included, are in `benchmarks/decisions/results/2026-09-26.json`.

## Measuring again

`benchmarks/decisions/` holds the labelled sets and the measurement. The sets are built from the repository's
own words: owner turns of the dev families (`benchmarks/paired/generators`, dev seeds 7, 11 and 13, at most
four per template and label; never a held-out split), the phrases the sidecar suite labels (the reactions
paraphrases and ordinary speech, the opt-out family, close variants and near misses, the capture and lesson
fixtures), and a smaller set of paraphrases written for this measurement in forms neither the phrase tables nor
the templates use (`novel`, reported apart).

```sh
python benchmarks/decisions/measure.py current --model-url <openai-compatible /v1> --model <name> --out current.json
python benchmarks/decisions/measure.py decide --url <decision endpoint> --label <checkpoint> --out answers.json
python benchmarks/decisions/measure.py analyse --current current.json --answers answers.json [...] --out summary.json
```

`current` runs each point's existing path: the phrase tables, or the production prompt (the capture call's
`SYSTEM` and `build_prompt`, the night's `LESSON_SYSTEM` over a one-session packet) on a model endpoint, read the
way the sidecar reads it. `decide` asks the decision model every item's question, one at a time. `analyse`
reports, per point and checkpoint: the existing path's accuracy and time; the model alone, zero-shot and with a
fitted temperature, with its expected calibration error (ten equal-width bins over the top answer's
probability); and the model with the fallback, composed as the sidecar wires it, with the temperature (least
negative log-likelihood) and the abstain threshold (most accurate; among equals, the one that lets the model
act least, "never" included) fitted by five-fold cross-validation, so the accuracy reported is the held-out
folds'. A point is enabled by default only when that is at least the existing path's accuracy overall and on
the repository's own items alone, and the threshold fitted on the whole set is not "never".
