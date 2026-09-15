"""Temporary transport for reviewed edits; no network, eval, or user data access.

Payload is base64(zlib(UTF-8 JSON)), split only to fit connector text limits.
Every edit is guarded by both its exact original and resulting Git blob hashes.
The final commit contains readable source changes, not this transport program.
"""
from pathlib import Path, PurePosixPath
import base64
import hashlib
import json
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[1]
PARTS = [ROOT / 'scripts' / name for name in (
    '_market_consistency_payload.1.b64',
    '_market_consistency_payload.2.b64',
    '_market_consistency_payload.b64',
)]
MANIFEST_SHA256 = '7a1562258b1829b4744f6c7efb4eeddffa1613fe19c4c0700bff6696fec7aaba'


def blob(content: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


def main() -> None:
    checkout = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=ROOT, text=True).strip()
    if Path(checkout).resolve() != ROOT:
        raise RuntimeError('Wrong checkout root')
    packed = ''.join(path.read_text(encoding='ascii').strip() for path in PARTS)
    raw = zlib.decompress(base64.b64decode(packed, validate=True))
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise RuntimeError('Transport digest mismatch; no source changes applied')
    manifest = json.loads(raw)
    pending = []
    seen = set()
    for entry in manifest:
        relative = PurePosixPath(entry['path'])
        if (relative.is_absolute() or '..' in relative.parts or relative.as_posix() in seen
                or not (relative.as_posix() == 'README.md' or relative.parts[0] in ('backend', 'frontend', 'scripts', 'tests', 'docs'))):
            raise RuntimeError('Unexpected target path')
        seen.add(relative.as_posix())
        path = ROOT / relative
        if path.is_symlink():
            raise RuntimeError('Symbolic link targets are not allowed')
        if entry['base'] is None:
            if path.exists():
                raise RuntimeError(f'New target already exists: {relative}')
            original = b''
        else:
            original = path.read_bytes()
            if blob(original) != entry['base']:
                raise RuntimeError(f'Baseline changed: {relative}; refusing overwrite')
        lines = original.decode('utf-8').splitlines(keepends=True)
        previous_end = 0
        for start, end, replacement in entry['edits']:
            if not (previous_end <= start <= end <= len(lines)) or not isinstance(replacement, str):
                raise RuntimeError('Invalid or overlapping edit range')
            previous_end = end
        for start, end, replacement in reversed(entry['edits']):
            lines[start:end] = replacement.splitlines(keepends=True)
        updated = ''.join(lines).encode('utf-8')
        if blob(updated) != entry['result']:
            raise RuntimeError(f'Result hash mismatch: {relative}')
        pending.append((path, updated))
    # Validate every target first; an incompatible branch is never partially patched.
    for path, content in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    subprocess.run(['git', 'diff', '--check'], cwd=ROOT, check=True)
    for path in PARTS:
        path.unlink()
    Path(__file__).unlink()
    print(f'Applied {len(pending)} hash-checked source, test and document changes.')


if __name__ == '__main__':
    main()
