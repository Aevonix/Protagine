# Owner preferences (the working perspective)

This increment gives the agent source-backed communication and initiative
preferences. Explicit owner corrections can change optional research
ordering. Runtime outcomes do not establish cognitive ability,
general opinions, emotions, consciousness or a worldview.
Identity prose and deployment-specific values remain private configuration.

## Owner corrections

The existing `PreferenceLearner` projects corrections from canonical, attributed
ordinary owner turns into the existing turn ledger. It keeps the original source
turn and message hash, correction time, and supersession reference. Checkpoints,
other contacts, assistant prose and quoted instructions cannot make these owner
corrections. Replaying a source does not add another correction.

The parser accepts a limited direct style vocabulary, such as `Be concise.`,
`Actually be detailed and thorough.`, `Use bullet points please.`, and
`Don't use emoji.` It also accepts explicit general communication preferences:
`I prefer brief replies.` or `I want detailed answers.` These enter the dedicated
owner-preference context on subsequent turns without competing for a semantic
recall slot. First-person requests must name replies, responses or answers;
`I want a code example.` and ambiguous `I want prose.` remain ordinary evidence.
Ambiguous negations, comparisons, questions, reported
speech and task-specific requests stay raw source evidence. This is not a general
natural-language preference extractor. A longer request can still influence the
current conversation without becoming a standing preference.

The same source path supports three explicit operational corrections:

```
Prefer research initiatives.
Deprioritize research initiatives.
Use normal priority for research initiatives.
```

`knowledge acquisition` can replace `research`. These set a priority preference,
not permission to execute, send, spend or change production. These explicit owner
corrections are the only weights this perspective applies, until a later correction
or erasure. Implicit observations cannot overwrite them. A late-delivered older correction
does not displace a newer one. Source occurrence time is used when available;
otherwise the source's observed ingestion time supplies the ordering fallback.

Erasure deletes the linked derived correction in the source transaction. Reads
also verify current source and message membership. A value-free head marker
prevents erasure or a late replay from reactivating an older preference/cache
value. Earlier non-erased corrections remain historical, not active. Original
legacy cache values are retained for keys that have never entered the source
path; their earlier missing provenance is not invented or retroactively repaired.

`GET /v1/host/preferences` returns current preferences, the source-backed state
and up to 100 recent surviving corrections. The source-backed deployment's
`POST /v1/host/preferences/learn` accepts `source_id` and reprojects that retained
owner source. It no longer turns arbitrary endpoint text into a new correction.
Ordinary owner messages remain the main capture path.

## Qualification and remaining acceptance

Neutral tests drive actual source ingestion, SQLite stores, scoped HTTP
middleware and context assembly. A fresh store instance and another session
produce the same stored preference. Tests also cover replay, late delivery,
source erasure, guest denial, preserved correction precedence and uncertain
wording retained as evidence.

The automatic opinion revisions and the attention snapshot that an earlier
initiative ranker kept next to these preferences left with the drives
milestone: self-initiated work is now chosen by the mind's one ranker
([MIND.md](MIND.md)), and `protagine upgrade` drops the two legacy tables after
its backup. Production acceptance still requires an actual owner correction
through the deployed native adapter and a later session observing it. Do not
call this a complete self or general opinion system before that behavior is
observed, and do not expand it into a personality simulator first.
