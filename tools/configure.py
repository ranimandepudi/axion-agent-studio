"""Write a local deployment config without contacting Google Cloud or Kubernetes."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.render import render
from worker.download_model import MODELS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='Your Google Cloud project ID')
    parser.add_argument('--registry', required=True, help='REGION-docker.pkg.dev/PROJECT/REPOSITORY')
    parser.add_argument('--tag', required=True, help='New image tag for this review build')
    parser.add_argument('--kube-api-cidr', required=True, help='Observed control-plane endpoint IPv4/32, not Service IP')
    parser.add_argument('--node-cidr', action='append', required=True, help='Actual node subnet CIDR; repeat if needed')
    parser.add_argument('--pod-cidr', action='append', required=True, help='Actual Pod secondary range; repeat if needed')
    parser.add_argument('--service-cidr', action='append', required=True, help='Actual Service secondary range; repeat if needed')
    parser.add_argument('--node-addresses', type=Path, help='Optional JSON object mapping observed node names to private IPv4s')
    parser.add_argument('--gemini-model', default='gemini-3.8-flash')
    parser.add_argument('--location', default='global', help='Vertex model location')
    parser.add_argument('--llama-image', default='ghcr.io/ggml-org/llama.cpp@sha256:21397f7329ff6fd5705b0bcedcb7e2ce10b5ce97cc12e6564278ac7133523ea6', help='Verified linux/arm64 CPU llama image')
    parser.add_argument('--llama-binary', default='/app/llama-server')
    parser.add_argument('--output', type=Path, default=Path('config.local.json'))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', args.project):
        parser.error('Supply a valid project ID')
    registry = args.registry.rstrip('/')
    if not re.fullmatch(r'[a-z0-9-]+-docker\.pkg\.dev/' + re.escape(args.project) + r'/[a-z0-9][a-z0-9._-]*', registry):
        parser.error('Registry must belong to the selected project')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', args.tag):
        parser.error('Supply a valid container image tag')
    model = MODELS['e4b']
    config = dict(project=args.project, namespace='axion-agent-studio',
                  worker_image=f'{registry}/worker:{args.tag}',
                  studio_image=f'{registry}/studio:{args.tag}',
                  model_image=f'{registry}/model:{args.tag}',
                  llama_image=args.llama_image, llama_binary=args.llama_binary,
                  model_name=model.filename.removesuffix('.gguf'), model_filename=model.filename,
                  model_sha256=model.sha256, model_bytes=model.size,
                  gemini_model=args.gemini_model, gemini_backend='vertex', google_cloud_location=args.location,
                  kube_api_cidr=args.kube_api_cidr, node_cidrs=args.node_cidr,
                  pod_cidrs=args.pod_cidr, service_cidrs=args.service_cidr,
                  llama_threads=4, llama_context=8192,
                  manager_pool='n4a-regular', qa_pool='c4a-sandbox', validator_pool='n4a-sandbox',
                  node_metrics_addresses=json.loads(args.node_addresses.read_text()) if args.node_addresses else {})
    render(config, 'full')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(config, indent=2) + '\n')
    print(f'Wrote {args.output}; image tags must be replaced with digests before deployment.')


if __name__ == '__main__':
    main()
