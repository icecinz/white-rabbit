#!/usr/bin/env python3
"""Keep content's private standalone primitive equal to its single source.

Edit only rabbit_runtime/directory_identity.py, then run this tool with --sync.
The generated file has identical bytes: provenance lives here and in the
content README so a generated banner cannot obscure a differing implementation.
This tool reads/writes source files only; it never imports either package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = '原型/rabbit_runtime/directory_identity.py'
MIRROR = '原型/rabbit_content/_directory_identity.py'


def _regular(path):
    if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
        raise ValueError('Directory identity source must be a regular file: ' + str(path))
    return path.read_bytes()


def check_payloads(payloads):
    """Check actual export bytes; historical payloads without both are unchanged."""
    if CANONICAL not in payloads and MIRROR not in payloads:
        return
    if CANONICAL not in payloads or MIRROR not in payloads or payloads[CANONICAL] != payloads[MIRROR]:
        raise ValueError('Directory identity mirror differs from canonical source; run sync_directory_identity.py --sync.')


def check(root=ROOT):
    root = Path(root)
    payloads = {name: _regular(root / name) for name in (CANONICAL, MIRROR)}
    check_payloads(payloads)
    return {'canonical': CANONICAL, 'mirror': MIRROR,
            'sha256': hashlib.sha256(payloads[CANONICAL]).hexdigest(), 'equal': True}


def sync(root=ROOT):
    root = Path(root)
    data = _regular(root / CANONICAL)
    target = root / MIRROR
    if target.is_symlink() or any(p.is_symlink() for p in target.parents):
        raise ValueError('Directory identity mirror path must not be symlinked.')
    if target.exists() and not target.is_file():
        raise ValueError('Directory identity mirror must be a regular file.')
    target.write_bytes(data)
    return check(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--sync', action='store_true')
    args = parser.parse_args(argv)
    print(json.dumps(sync() if args.sync else check(), ensure_ascii=False))


if __name__ == '__main__':
    main()
