"""Replace locally configured build tags with verified registry digests."""
import argparse
import json
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.local.json'))
    for role in ('worker', 'studio', 'model'):
        parser.add_argument('--' + role + '-digest', required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    for role in ('worker', 'studio', 'model'):
        digest = getattr(args, role + '_digest')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            parser.error(role + ' digest must be sha256 followed by 64 lowercase hexadecimal characters')
        image = config[role + '_image']
        repository = image.split('@', 1)[0].rsplit(':', 1)[0]
        config[role + '_image'] = repository + '@' + digest
    args.config.write_text(json.dumps(config, indent=2) + '\n')
    print('Pinned worker, studio and model images. No registry or cluster was contacted.')


if __name__ == '__main__':
    main()
