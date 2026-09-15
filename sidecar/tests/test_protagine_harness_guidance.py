"""Generated guidance and skill discovery use one canonical installation."""
from protagine.harness_integration import (
    PROTAGINE_CONTEXT_TEMPLATE, write_protagine_context, write_protagine_skill,
)
from protagine.harness_integration import skills


def test_context_generation_preserves_unrelated_private_instructions(tmp_path):
    identity = tmp_path / 'SOUL.md'
    identity.write_text('Private agent identity.')
    assert write_protagine_context(tmp_path)
    assert (tmp_path / 'PROTAGINE.md').read_text() == PROTAGINE_CONTEXT_TEMPLATE
    assert identity.read_text() == 'Private agent identity.'


def test_skill_install_updates_one_discovered_directory(tmp_path, monkeypatch):
    target = tmp_path / 'protagine-diagnose'
    monkeypatch.setitem(skills.SKILL_PATHS, 'codex', str(target))
    assert write_protagine_skill('codex')
    (target / 'SKILL.md').write_text('outdated instruction')
    assert write_protagine_skill('codex')
    assert list(tmp_path.iterdir()) == [target]
    assert 'name: protagine-diagnose' in (target / 'SKILL.md').read_text()
