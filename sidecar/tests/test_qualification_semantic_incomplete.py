import asyncio
from types import SimpleNamespace
from protagine.qualification import native_semantic_recall as module


def test_semantic_wrapper_keeps_incomplete_effects_for_separate_grading(tmp_path,monkeypatch):
    received={}
    async def consume(inputs,context,**kwargs):
        received.update(kwargs)
        return {'output':'unfinished','effects':{'native_turn_complete':False,'semantic_recall':{'initialized':True}}}
    monkeypatch.setattr(module,'memory_consume',consume)
    result=asyncio.run(module.consume({},SimpleNamespace(state_dir=tmp_path)))
    assert received['allow_incomplete_results'] is True
    assert result['effects']['semantic_recall']['initialized'] is True
    assert result['effects']['native_turn_complete'] is False
