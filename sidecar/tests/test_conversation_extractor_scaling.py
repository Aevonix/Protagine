"""Large canonical inputs retain the same entity evidence and pass priority."""

from dataclasses import asdict
import re

import pytest

from protagine.world_model.extraction import conversation_extractor as module


def expected(text, rows):
    return [dict(text=name, entity_type=kind, start_char=start, end_char=end,
                 confidence=confidence, context_window=text[max(0, start - 40):end + 40],
                 linked_entity_id=None)
            for name, kind, start, end, confidence in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize('text, rows', [
    ('Alice@Example.com Bob https://Acme.com/Alice Acme Corp Near Alice Example.com', [
        ('Alice@Example.com', 'person', 0, 17, 0.7000000000000001),
        ('https://Acme.com/Alice', 'product', 22, 44, 0.6000000000000001),
        ('Bob', 'person', 18, 21, 0.65),
        ('Alice Example', 'location', 60, 73, 0.55),
    ]),
    ('Zelda speaks at Vega Institute. https://Example.com/Acme alice@Example.com First Last example.dev', [
        ('alice@Example.com', 'person', 57, 74, 0.7000000000000001),
        ('https://Example.com/Acme', 'product', 32, 56, 0.6000000000000001),
        ('Vega Institute', 'company', 16, 30, 0.75),
        ('Zelda', 'person', 0, 5, 0.45000000000000007),
        ('First Last', 'person', 75, 85, 0.55),
        ('example.dev', 'company', 86, 97, 0.6000000000000001),
    ]),
])
async def test_prior_pass_precedence_preserves_every_candidate_field(text, rows):
    result = await module.ConversationExtractor().extract(
        text, 'source', existing_entities=['Bob', 'Vega Institute'])
    assert result.source_id == 'source'
    assert result.relationships == []
    assert [asdict(candidate) for candidate in result.entities] == expected(text, rows)


@pytest.mark.asyncio
async def test_touching_spans_survive_but_intersections_do_not(monkeypatch):
    # Adjacent spans from different priority passes are distinct evidence.
    # The organization candidate intersects both the email and URL candidates.
    monkeypatch.setattr(module, '_EMAIL_RE', re.compile('Alice'))
    monkeypatch.setattr(module, '_URL_RE', re.compile('Bob'))
    monkeypatch.setattr(module, '_ORG_SUFFIX_RE', re.compile('ceBob'))
    monkeypatch.setattr(module, '_CAPITALIZED_NAME_RE', re.compile('Acme'))
    monkeypatch.setattr(module, '_DOMAIN_RE', re.compile('(moss)'))
    text = 'AliceBobAcmemoss; this is the source.'
    result = await module.ConversationExtractor().extract(text, 'source')
    assert [asdict(candidate) for candidate in result.entities] == expected(text, [
        ('Alice', 'person', 0, 5, 0.7000000000000001),
        ('Bob', 'product', 5, 8, 0.6000000000000001),
        ('Acme', 'person', 8, 12, 0.45000000000000007),
        ('moss', 'company', 12, 16, 0.6000000000000001),
    ])


@pytest.mark.asyncio
async def test_large_repeated_catalog_retains_every_occurrence_in_order():
    # Public tool catalogs repeat proper names thousands of times. Retaining
    # all their positions is part of source provenance, even for identical names.
    text = 'Alpha;' * 16000
    result = await module.ConversationExtractor().extract(text, 'catalog')
    assert [asdict(candidate) for candidate in result.entities] == expected(text, [
        ('Alpha', 'person', start, start + 5, 0.45000000000000007)
        for start in range(0, len(text), 6)
    ])
