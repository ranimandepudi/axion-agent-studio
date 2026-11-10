"""Verify a reviewer-supplied E4B GGUF and stage only that file for Docker."""
import argparse
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker.download_model import MODELS, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model_file', type=Path)
    parser.add_argument('--output-dir', type=Path, default=Path('.build/model'))
    args = parser.parse_args()
    model = MODELS['e4b']
    if not verify(args.model_file, 'e4b'):
        parser.error('Supplied model does not match the pinned E4B byte count and SHA-256')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / model.filename
    if args.model_file.resolve() != target.resolve():
        shutil.copyfile(args.model_file, target)
    if not verify(target, 'e4b'):
        raise SystemExit('Staged model failed integrity verification')
    print(f'Verified {target}: {model.size} bytes, SHA-256 {model.sha256}')


if __name__ == '__main__':
    main()
