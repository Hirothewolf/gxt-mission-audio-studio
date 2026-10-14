"""Regenerate independent game-file index. No production/game modifications."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.audio.library_context import read_manifest
from studio.core.utils import json_atomico
from studio.script.native_index import build

def progress(done, total, message):
    print(json.dumps({'event': 'progress', 'done': done, 'total': total, 'message': message}, ensure_ascii=False), flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', required=True)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    root = Path(args.project).resolve()
    report = build(args.game, read_manifest(root), progress)
    if report['scm']['incomplete'] or report['scm']['invalid_branch_targets']:
        raise ValueError('SCM não decodificado integralmente; índice anterior preservado: ' + str(report['scm']['incomplete']))
    for source in report['sources'].values():
        stat = Path(source['path']).stat()
        source['signature'] = [stat.st_size, stat.st_mtime_ns]
    json_atomico(root / 'workspace/native_audio/index.json', report)
    print(json.dumps(report['summary'], ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
