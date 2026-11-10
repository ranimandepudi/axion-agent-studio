"""Local companion helpers use temporary synthetic data and no external calls."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def command(script, *args):
    return subprocess.run([sys.executable, str(ROOT / script), *map(str, args)],
                          capture_output=True, text=True)


def configured(tmp_path):
    path = tmp_path / 'config.json'
    result = command('tools/configure.py', '--project', 'review-project',
                     '--registry', 'us-central1-docker.pkg.dev/review-project/studio-review',
                     '--tag', 'test', '--kube-api-cidr', '10.42.0.2/32',
                     '--node-cidr', '10.42.0.0/20', '--pod-cidr', '10.44.0.0/16',
                     '--service-cidr', '10.45.0.0/20', '--output', path)
    assert result.returncode == 0, result.stderr
    return path


def test_configure_render_and_pin_contract(tmp_path):
    path = configured(tmp_path)
    config = json.loads(path.read_text())
    assert config['qa_pool'] == 'c4a-sandbox'
    assert config['validator_pool'] == 'n4a-sandbox'
    assert config['manager_pool'] == 'n4a-regular'
    assert config['model_name'] == 'gemma-4-E4B-it-Q4_0'
    assert config['model_filename'] == config['model_name'] + '.gguf'
    assert command('infra/render.py', '--config', path, '--require-digests').returncode != 0
    args = [arg for role in ('worker', 'studio', 'model') for arg in ('--' + role + '-digest', 'sha256:' + 'a' * 64)]
    assert command('tools/pin_images.py', '--config', path, *args).returncode == 0
    for phase in ('checkpoint', 'full'):
        rendered = command('infra/render.py', '--config', path, '--phase', phase, '--require-digests')
        assert rendered.returncode == 0, rendered.stderr
        assert json.loads(rendered.stdout)['kind'] == 'List'


def test_wrong_model_is_rejected_without_staging(tmp_path):
    model = tmp_path / 'wrong.gguf'
    model.write_bytes(b'Explicit fake model fixture, not weights')
    output = tmp_path / 'staged'
    result = command('tools/stage_model.py', model, '--output-dir', output)
    assert result.returncode != 0
    assert not output.exists()


def test_placeholder_example_cannot_render():
    assert command('infra/render.py', '--config', ROOT / 'infra/config.example.json').returncode != 0
