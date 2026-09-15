"""Only exact observed native user-tail repairs may recover a current suffix."""
import os
from conftest import run_python


def test_mismatched_native_tail_cannot_restore_context(artifacts,tmp_path):
    installed=artifacts[3]
    run_python('-I','-c',r'''
import sys
sys.path.insert(0,sys.argv[1])
from protagine_hermes.request_memory import _restore_current_suffix
current={'role':'user','content':'Continue the accepted task.',
 'api_content':'Continue the accepted task.\n\n<memory-context>current native suffix</memory-context>'}
tail=['Earlier instruction.','Continue the accepted task.']
for observed in (current, {'role':'user','content':tail[-1]}):
 for text in ('Unrelated text.','Earlier instruction.\nContinue the accepted task.',
  'Earlier instruction.\n\nContinue the accepted task.\nAn extra user-authored clause.'):
  request={'messages':[{'role':'user','content':text}]}
  restored,repair=_restore_current_suffix(request,tail,observed)
  assert restored is request and repair is None
''',installed,cwd=tmp_path,env={key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ})


def test_exact_unenriched_native_tail_splits_without_adding_context(artifacts,tmp_path):
    installed=artifacts[3]
    run_python('-I','-c',r'''
import sys
sys.path.insert(0,sys.argv[1])
from protagine_hermes.request_memory import _restore_current_suffix, _recombine_current_suffix
tail=['Earlier instruction.','Continue the accepted task.']
current={'role':'user','content':tail[-1]}
for key in ('messages','input'):
 request={key:[{'role':'system','content':'Keep this instruction.'},
  {'role':'user','content':'\n\n'.join(tail),'name':'native-current'},
  {'role':'assistant','content':'Keep this following row.'}]}
 restored,repair=_restore_current_suffix(request,tail,current)
 # Erasure filtering must see the current human input separately even when
 # native plain-user repair merged it with a reminder without api_content.
 assert repair is not None
 assert restored[key][1:3] == [{'role':'user','content':text} for text in tail]
 assert restored[key][0] == request[key][0] and restored[key][-1] == request[key][-1]
 # Splitting certifies no context or provenance and preserves the fresh wire.
 assert _recombine_current_suffix(restored,repair) == request
''',installed,cwd=tmp_path,env={key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ})
