"""Build a source deployment bundle with an explicit allowlist and SHA-256 manifest.

Usage: python deploy/build_release.py /absolute/path/release.tar.gz
Environment files, credentials, logs, reports and virtual environments are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(output: Path):
    paths = [ROOT / name for name in (
        'pyproject.toml', 'README.md', 'SERVICE.md', 'LICENSE', 'requirements.lock',
        'requirements-test.txt', 'requirements.txt',
    )]
    for name in ('TikTokLive', 'live_service', 'tests'):
        paths.extend(p for p in (ROOT / name).rglob('*')
                     if p.is_file() and not p.is_symlink() and p.suffix in {'.py', '.json', '.txt'}
                     and '__pycache__' not in p.parts)
    paths.extend(ROOT / 'examples' / name for name in (
        '__init__.py', 'fastapi_ws_server.py', 'redis_helper.py', 'log_config.py',
    ) if (ROOT / 'examples' / name).exists())
    paths.extend(ROOT / 'deploy' / name for name in (
        'quality_probe.py', 'live_probe.py', 'piratetok_bootstrap.py', 'configure_nginx.py',
        'tiktok-live.service', 'tiktok-live@.service', 'build_release.py',
    ))
    paths.append(ROOT / 'plans/unsigned-production-rollout.md')
    contents = {str(p.relative_to(ROOT)): p.read_bytes() for p in sorted(set(paths))}
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    contents['RELEASE-MANIFEST.json'] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'x:gz') as tar:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return {'artifact': str(output), 'files': len(manifest), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    print(json.dumps(build(parser.parse_args().output.resolve())))
