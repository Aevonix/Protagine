"""Owner-facing diagnostics from existing execution observations."""
import json
import re

import httpx


def render(report):
    lines = [f"Execution {report['execution_id']}: {report['state']}",
        f"Retained requests: {report['total_requests']}; complete callback pairs: "
        f"{report['complete_observed_pairs']}"]
    for index, call in enumerate(report['calls'], start=report['offset'] + 1):
        request, response = call['request'], call['response']
        def shown(value):
            return 'unknown' if value is None or value == '' else str(value)
        lines.append(f"{index}. {shown(request['requested_model'])} [{shown(request['provider'])}] "
            f"-> reported {shown(response['response_model'])}; "
            f"seconds={shown(response['api_duration'])}; stop={shown(response['finish_reason'])}; "
            f"text_chars={shown(response['assistant_content_chars'])}; "
            f"tool_calls={shown(response['assistant_tool_call_count'])}")
        if call['observations']:
            lines.append('   Observed: ' + ', '.join(call['observations']))
    if report['more']:
        lines.append(f"More retained calls: use --offset {report['offset'] + len(report['calls'])}.")
    lines.append(report['evidence_boundary'])
    return '\n'.join(lines)


def diagnose(args):
    from protagine.doctor import default_protagine_url
    if (not re.fullmatch(r'[a-f0-9]{64}', args.execution_id)
            or not 0 <= args.offset <= 128 or not 1 <= args.limit <= 128):
        raise ValueError('Invalid execution ID or page')
    credential = json.loads(args.credential_file.read_text())
    if not isinstance(credential, dict) or not all(
            isinstance(credential.get(key), str) and credential[key]
            for key in ('principal', 'secret')):
        raise ValueError('A scoped private client credential is required')
    url = (args.url or default_protagine_url()).rstrip('/') + '/v1/host/executions/model-calls'
    # Explicit destination, no redirect or environment proxy forwarding of credentials.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
        try:
            response = client.get(url, params={'contact_id': args.contact_id,
                'execution_id': args.execution_id, 'offset': args.offset, 'limit': args.limit},
                headers={'Authorization': 'Bearer ' + credential['secret'],
                         'X-Protagine-Principal': credential['principal']})
            response.raise_for_status()
            report = response.json()
        except httpx.HTTPStatusError as exc:
            print(f"Model diagnostics unavailable (HTTP {exc.response.status_code}).")
            return 1
        except httpx.RequestError:
            print('Model diagnostics unavailable: sidecar request failed.')
            return 1
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0
