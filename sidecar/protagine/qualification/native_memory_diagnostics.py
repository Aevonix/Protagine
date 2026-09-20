"""Versioned derived views; never overwrite original benchmark checks."""
import re

VERSION = 'native-memory-narrative-attribution-2'
_SCOPED = re.compile(r'\b(?:fiction(?:al)?|fictional character|in (?:the |a |my |their |his |her )?story|'
                     r'within (?:the |a )?story|story character|in (?:the |a )?novel|in roleplay)\b', re.I)
_UNQUALIFIED_OWNERSHIP = {'owns', 'own', 'ownership', 'possesses', 'has', 'badge code'}


def narrative_attribution(observed, source_id):
    """Allow explicitly scoped narrative records, not unqualified owner facts.

    Evidence quotations alone cannot rescue an unqualified scalar assertion.
    Unrecognized paraphrases are unknown and need semantic review. This view
    does not change the original zero-claims/promotion-policy check or certify
    that the fiction record is useful enough to keep.
    """
    formation = observed.get('effects', {}).get('formation', {})
    jobs = formation.get('jobs', [])
    job = next((row for row in jobs if row.get('turn_id') == source_id), None)
    if not job or job.get('status') != 'complete' or 'claims_before' not in formation:
        return {'scorer_version': VERSION, 'attribution_preserved': None, 'basis': 'incomplete_evidence'}
    claims = [row for row in formation['claims_before'] if row.get('source_id') == source_id
              and not row.get('superseded_by') and not row.get('retracted_by')]
    results = []
    for row in claims:
        identity = ' '.join(str(row.get(key, '')) for key in ('subject', 'predicate', 'value'))
        if _SCOPED.search(identity) and not re.search(r'\b(?:not|non)\s+(?:fiction|fictional|in story)\b', identity, re.I):
            results.append(True)
        elif row.get('subject_key') == 'speaker' and row.get('predicate', '').casefold() in _UNQUALIFIED_OWNERSHIP:
            results.append(False)
        else:
            results.append(None)
    preserved = False if False in results else None if None in results else True
    return {'scorer_version': VERSION, 'attribution_preserved': preserved,
        'basis': ('no_promoted_claim' if not claims else 'explicit_narrative_scope' if preserved is True
                  else 'unqualified_owner_assertion' if preserved is False else 'semantic_review_required'),
        'v1_zero_promoted_claims': not claims,
        'memory_usefulness': 'not_assessed'}
