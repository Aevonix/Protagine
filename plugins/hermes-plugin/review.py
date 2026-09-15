"""Keep autonomous skill changes in Hermes' existing proposal mechanism."""
import json
import hashlib


def editable_operation(arguments, *, allow_create=False):
    """One native main-file change, with creation explicitly selected by callers."""
    operations = arguments.get('operations')
    if operations is not None:
        if not isinstance(operations, list) or len(operations) != 1 or not isinstance(operations[0], dict):
            return None
        operation = dict(operations[0])
        operation['name'] = operation.get('name') or arguments.get('name')
    else:
        operation = arguments
    actions = {'edit', 'patch', 'create'} if allow_create else {'edit', 'patch'}
    if operation.get('action') not in actions or operation.get('file_path') not in {None, '', 'SKILL.md'}:
        return None
    if operation.get('content'):
        if not isinstance(operation['content'], str) or operation.get('old_string') or operation.get('new_string') is not None:
            return None
    elif (operation.get('action') != 'patch' or not isinstance(operation.get('old_string'), str)
            or not operation['old_string'] or not isinstance(operation.get('new_string'), str)
            or type(operation.get('replace_all', False)) is not bool):
        return None
    return operation


def stage_skill_change(arguments):
    """Native review proposes; an owner or configured evaluator applies later.

    Foreground requests never use this adapter. Existing native ownership
    guards still run before staging and when the proposal is eventually applied.
    """
    from tools import skill_manager_tool as manager, write_approval as approval
    from .review_successors import candidate_guard, record_failure
    denied = candidate_guard(arguments)
    if denied:
        return json.dumps({'success': False, 'error': denied})
    # The worker binds this scope before staging, which returns before native
    # pre-tool hooks. An unattributed failure cannot justify an existing edit.
    if arguments.get('_pacomind_review_create_only') is True:
        selected = editable_operation(arguments, allow_create=True)
        if selected is None or selected.get('action') != 'create':
            return json.dumps({'success':False, 'error':'Unattributed review may only propose one new main skill file; existing skills have not been implicated.'})
    try:
        from tools.skill_manager_batch import _BATCH_MAX_OPS
    except ModuleNotFoundError as error:
        if error.name != 'tools.skill_manager_batch':
            raise
        from tools.skill_manager_tool import _BATCH_MAX_OPS  # Hermes 0.21.0
    operations = arguments.get('operations')
    if operations is not None and (not isinstance(operations, list) or not operations):
        return json.dumps({'success':False, 'error':'operations must be a non-empty array.'})
    if operations is not None and len(operations) > _BATCH_MAX_OPS:
        return json.dumps({'success':False, 'error':f'operations is capped at {_BATCH_MAX_OPS} ops per call.'})
    if operations is not None and len(operations) != 1 and any(
            isinstance(operation, dict) and operation.get('action') == 'delete' for operation in operations):
        return json.dumps({'success':False, 'error':'delete must be the sole operation in its call.'})
    steps = operations if operations is not None else [arguments]
    actions = {'create', 'patch', 'delete', 'write_file', 'remove_file'}
    if operations is None:
        actions.add('edit')  # Native legacy flat-input alias, not a batch action.
    for operation in steps:
        if not isinstance(operation, dict):
            return json.dumps({'success':False, 'error':'Skill operation must be an object'})
        action = operation.get('action')
        if not isinstance(action, str) or action not in actions:
            return json.dumps({'success':False, 'error':'Skill operation requires a supported action'})
        name = operation.get('name') or arguments.get('name', '')
        if not isinstance(name, str) or not name.strip():
            return json.dumps({'success':False, 'error':'Skill operation requires a name'})
        denied = manager._background_review_preflight(action, name)
        if denied is not None:
            return json.dumps(denied)
        if action == 'create':
            content = operation.get('content')
            if not isinstance(content, str):
                return json.dumps({'success':False, 'error':'Create content must be the full SKILL.md text.'})
            # Dispatch is intercepted here. Return the native create contract
            # error to this reviewer before accepting an unusable proposal.
            invalid = (manager._validate_name(name) or manager._validate_category(operation.get('category'))
                or manager._validate_frontmatter(content, new_skill=True) or manager._validate_content_size(content))
            if invalid:
                record_failure(arguments, kind='validation', diagnostic=invalid, phase='proposal_validation')
                return json.dumps({'success':False, 'error':invalid})
        # Staging intercepts native dispatch, so retain Hermes' existing
        # review-local read requirement as well as its ownership preflight.
        # Native read marks identify paths, not a content-freshness guarantee.
        if action in {'edit', 'patch', 'write_file', 'remove_file'}:
            existing = manager._find_skill(name)
            if existing:
                file_path = operation.get('file_path') or 'SKILL.md'
                target = existing['path'] / file_path
                if target.exists():
                    denied = manager._background_review_read_before_write_guard(
                        name, target, action, file_path)
                    if denied is not None:
                        return json.dumps(denied)
    summary = ('Review skill operation batch' if operations is not None else
               approval.skill_gist(arguments.get('action', ''), arguments.get('name', ''),
                   content=arguments.get('content') or '', file_path=arguments.get('file_path') or '',
                   old_string=arguments.get('old_string') or '', new_string=arguments.get('new_string') or ''))
    payload = dict(arguments)
    # Source references come from the actual native request captured before
    # Hermes forked this review, never from the proposed skill's arguments.
    from .review_evidence import current
    payload['_pacomind_review_evidence'] = current()
    operation = editable_operation(arguments, allow_create=True)
    if operation is not None:
        current = manager._find_skill(operation.get('name', ''))
        if operation['action'] == 'create':
            # Absence comes from the native catalog, never proposed metadata.
            payload['_pacomind_review_base_absent'] = current is None
            payload['_pacomind_review_base_sha256'] = None
        elif current:
            payload['_pacomind_review_base_sha256'] = hashlib.sha256((current['path'] / 'SKILL.md').read_bytes()).hexdigest()
    record = approval.stage_write(approval.SKILLS, payload, summary=summary, origin='background_review')
    # Native staging is best-effort. Never report a stored proposal when its
    # writer failed; the later evaluator also reads this same pending record.
    if approval.get_pending(approval.SKILLS, record['id']) != record:
        return json.dumps({'success':False, 'error':'Native skill proposal was not persisted'})
    return json.dumps({'success':True, 'staged':True, 'pending_id':record['id'], 'gist':summary,
                      'message':'Skill change proposed for independent evaluation or owner review; active skills unchanged'})
