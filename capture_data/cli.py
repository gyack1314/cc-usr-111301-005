"""输出原始清单概况，并提供受限 DAP2 约束采集入口。"""
import argparse
import json
from pathlib import Path

from .dap2 import Dap2Error, request_from_manifest, ReplayServer, Dap2Client
from .io import load
from .domain import summarize


def _capture(file):
    manifest, _ = load(file)
    root = Path(file).parent
    request = request_from_manifest(manifest)
    record = {
        'request': {'method': 'GET', 'url': request.url},
        'response': {
            'status': 200,
            'headers': {},
            'asset': manifest['response'],
            'chunk_lengths': list(manifest['chunk_lengths']),
        },
    }
    client = Dap2Client(ReplayServer([record], root))
    return client, request, client.capture(request)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['inspect', 'capture'])
    p.add_argument('file')
    a = p.parse_args()
    if a.command == 'inspect':
        v, digest = load(a.file)
        print(json.dumps({'manifest_sha256': digest,
                          'summary': summarize(v, Path(a.file).parent)},
                         ensure_ascii=False, indent=2))
        return
    try:
        _client, _request, result = _capture(a.file)
    except (Dap2Error, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
