# Function-router recovery qualification

Two development cases and three private holdouts exercise the existing
`LLMRouter` with temporary HTTP faults in front of explicitly selected primary
and fallback bindings. The proxy never changes a real endpoint. Successful
requests relay real upstream responses through the router's existing local
address validation, pinned client and redirect restrictions.

| Scenario | Required observation |
| --- | --- |
| Primary returns HTTP 503 | A real fallback completion preserves the requested word and records the failed primary attempt. |
| Primary recovers after cooldown | Fallback first, an actual elapsed 15-second cooldown, then a real primary completion without a preceding failed attempt on that call. |
| Primary response is withheld | The router's own attempt deadline expires and a real fallback completion succeeds. The primary request never reaches the real server. |
| Another call arrives during cooldown | The failed primary receives no second request. The router records its cooldown skip and both callers get fallback responses. |
| Both endpoints are unavailable | The finite router loop reports unavailability without fabricating a completion. Neither real endpoint receives a request. |

The concurrent cooldown case has at most two fallback requests in flight. It
starts the second only after the actual runtime records the first failure. This
avoids mistaking a slow first fallback, lasting beyond the cooldown, for a router
bug. The recovery case waits real wall time rather than changing the clock or
replacing availability policy.

These are router reliability checks. They do not qualify native gateway recovery,
restart recovery, channel delivery or a model's intelligence. The existing runner
keeps fallback success separate from primary outcome. A successful response from
the fallback must never become a primary-model pass. The total-outage case has
no candidate completion at all. Returned model IDs and actual request evidence
remain in private results; configured labels alone do not establish attribution.

## Integration

```python
from protagine.qualification.router_recovery import (
    cases, RecoveryRouter, CONSUMERS, EVALUATORS, recipe_metadata,
)

suite = cases()  # separately: cases(private_fixture_path)
recipe.update(recipe_metadata(suite))
# Existing runner.evaluate(...,
#     lambda case: RecoveryRouter(config, primary_binding, fallback_binding))
```

Both bindings must already exist in the supplied configuration and must not alias
the same endpoint/model pair. The candidate and supporting fallback are explicit,
independently selected deployment roles. The benchmark copies the configuration,
temporarily replaces the two URLs with owned loopback proxies, and gives the
chat role those two candidates. It does not modify files, active role mappings,
endpoint health or production routing. Context/model declarations remain intact;
the fixture intentionally replaces the chat role's candidate constraints to
exercise fallback. This is not a claim that a production role permits that path.

The case recipe contains attempt budgets: 90 seconds normally, 25 seconds for
the withheld-response case, a 190-second per-call deadline, at most two calls and
a 360-second case deadline. The timeout case requires the real fallback to finish
within the same 25-second attempt budget. A timeout failure is meaningful and
must not be rerun under undisclosed larger limits. Fixture budgets can be changed
only as a new declared recipe version before a comparison.

Owned proxies bind only to loopback, cap request/response sizes, retain no
credentials in evidence, and close in all exit paths. Remote GPU cancellation is
not observed; closing a client does not prove a serving engine stopped computing.
Controlled tests use local HTTP model responders, real router deadlines and the
real cooldown. They validate mechanisms and grading, not live model quality.
