#!/usr/bin/env python3
"""Create a local-only semantic config from the downloaded model manifest.

Does not run inference, read recordings, enable cloud providers, or install services.
Existing configuration is never overwritten.
"""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path,
                        default=Path.home() / 'Library/Application Support/HemoryLocal')
    args = parser.parse_args()
    root = args.data_dir.expanduser().resolve()
    manifest = root / 'semantic/models/manifest.json'
    if not manifest.is_file():
        parser.error('Run scripts/setup_local_models.py for this data directory first.')
    local = json.loads(manifest.read_text())['config']
    for field in ('vad_model_path', 'asr_model_path'):
        if not Path(local[field]).exists():
            parser.error('A model path in the manifest is missing; verify the model setup.')
    os.umask(0o077)
    private = root / 'private'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = private / 'semantic.json'
    config = {'paused': False, 'local': local,
              'cloud': {'enabled': False, 'price_verified': False,
                        'monthly_budget_usd': 0, 'trial_budget_usd': 0}}
    try:
        with target.open('x', encoding='utf-8') as out:
            json.dump(config, out, ensure_ascii=False, indent=2)
            out.write('\n')
    except FileExistsError:
        parser.error('semantic.json already exists; inspect it locally instead of overwriting it.')
    target.chmod(0o600)
    print('Local semantic configuration created; cloud disabled. No worker was started.')


if __name__ == '__main__':
    main()
