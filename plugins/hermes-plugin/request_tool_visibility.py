"""Narrow current-request tool visibility; no scope policy or catalog store."""
import json
import re

_CATALOG_HEADER = 'Deferred tool catalog (call schemas via `tool_describe`, invoke via `tool_call`):'


def without_tool(request, name):
    """Edit current schemas and exact native listing entries, never history.

    A summary-only catalog has no named entry to remove. Its aggregate counts
    remain native catalog counts; discovery filters the actual named result.
    """
    def description(text):
        if not isinstance(text, str) or _CATALOG_HEADER not in text:
            return text
        prefix, listing = text.split(_CATALOG_HEADER, 1)
        lines, group, removed = listing.splitlines(), None, False
        for index, line in enumerate(lines):
            if re.fullmatch(r'.+ tools \(\d+\):', line):
                group = index
            if line.startswith('- '):
                names = [line[2:].split(':', 1)[0]]
                replacement = None
            else:
                names = [name.strip() for name in line.split(',')]
                replacement = ', '.join(item for item in names if item != name) or None
            if name not in names:
                continue
            lines[index], removed = replacement, True
            if group is not None:
                match = re.fullmatch(r'(.+ tools \()(\d+)(\):)', lines[group])
                if match:
                    count = max(0, int(match[2]) - 1)
                    lines[group] = f'{match[1]}{count}{match[3]}' if count else None
        if not removed:
            return text
        prefix = re.sub(r'^Search (\d+) additional tools',
            lambda match: f'Search {max(0, int(match[1])-1)} additional tools', prefix, count=1)
        return prefix + _CATALOG_HEADER + '\n'.join(line for line in lines if line is not None)

    result = dict(request)
    for key in ('tools', 'functions'):
        if not isinstance(request.get(key), list):
            continue
        rows = []
        for row in request[key]:
            fn = row.get('function', row) if isinstance(row, dict) else None
            if isinstance(fn, dict) and fn.get('name') == name:
                continue
            if isinstance(fn, dict) and fn.get('name') == 'tool_search':
                changed = description(fn.get('description'))
                if changed != fn.get('description'):
                    fn = {**fn, 'description': changed}
                    row = {**row, 'function': fn} if 'function' in row else fn
            rows.append(row)
        result[key] = rows
    for key in ('tool_choice', 'function_call'):
        choice = request.get(key)
        fn = choice.get('function', choice) if isinstance(choice, dict) else None
        if isinstance(fn, dict) and fn.get('name') == name:
            result.pop(key)  # Restore the provider default; unrelated tools remain available.
        elif not result.get('tools') and not result.get('functions'):
            result.pop(key, None)  # A required/any choice cannot refer to an empty tool set.
    return result


def without_discovery_tool(value, context, hidden_name):
    """Remove one proven returned member; retain native aggregate metadata otherwise."""
    name = context.get("tool_name")
    if name not in {"tool_search", "tool_describe"} or not isinstance(value, str):
        return value
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return value
    if (not isinstance(result, dict) or not isinstance(result.get('tools'), dict)
            or hidden_name not in result['tools']):
        return value
    result['tools'].pop(hidden_name)
    if name == 'tool_describe':
        result.setdefault('not_found', []).append(hidden_name)
    else:
        if type(result.get('total_available')) is int:
            result['total_available'] = max(0, result['total_available'] - 1)
        for group in result.get('results', []):
            if isinstance(group, dict) and isinstance(group.get('matches'), list):
                group['matches'] = [item for item in group['matches'] if item != hidden_name]
    return json.dumps(result, ensure_ascii=False)
