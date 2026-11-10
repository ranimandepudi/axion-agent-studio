# Axion Agent Studio

Read `LICENSE`, `NOTICE.md`, and `MODEL_SOURCES.md` before using this companion. It contains runtime source and tests, with historical data and private deployment configuration excluded. The deployment instructions use an existing compatible GKE cluster. A fresh end-to-end Arm/GKE walkthrough remains pending; see `VALIDATION.md` for the checks completed so far.

The application uses hosted Gemini planning, three CPU-only local Gemma workers, and independent generated-code validation. The regular manager serves the UI and prepared shop. Backend and Frontend work concurrently; QA follows both; only accepted validation unlocks the changed checkout. The code supports the prepared shop's percentage-discount/free-delivery policy, not arbitrary repositories.

## Get the source

Clone the public repository and enter the application directory:

```bash
git clone https://github.com/ranimandepudi/axion-agent-studio.git
cd axion-agent-studio
```

For the Learning Path, check out the exact commit specified in its preparation instructions before building. That commit identifies the source reviewed with the tutorial; the latest `main` branch can change. Sample code uses BSD-3-Clause-Clear and documentation uses CC BY-SA 4.0, as described in `LICENSE-SUMMARY`. Models and dependencies retain their separate licenses in `NOTICE.md`.

To update a development checkout after completing the tutorial, first commit or otherwise save your local changes, then return to `main` and pull updates:

```bash
git switch main
git pull --ff-only
```

Do not pull updates midway through a pinned tutorial run. A detached checkout remains on the selected commit until you explicitly switch branches or select another commit.

## Inspect locally

Use Bash, Python 3.12 for the intended container/runtime contract, and Node.js 22 for frontend checks. Run the commands from this source directory and keep the activated environment for the remaining application commands:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest tests infra/test_manifests.py -q
node --check web/app.js
node --test tests/frontend_live_console.test.cjs
```

Tests that require the isolated Linux validator skip outside that environment. Keep those skips separate from passes; local contract tests do not establish live model quality or GKE isolation.

For a UI-only localhost review, start with no configured cloud/worker/validator endpoints and a new data directory:

```bash
STUDIO_DATA_DIR="$PWD/data/local-review" STUDIO_ENABLE_TEST_FIXTURES=0 \
  GOOGLE_CLOUD_PROJECT= GEMINI_API_KEY= GOOGLE_API_KEY= \
  WORKER_BACKEND_URL= WORKER_FRONTEND_URL= WORKER_QA_URL= VALIDATOR_URL= \
  .venv/bin/python -m uvicorn studio.app:app --host 127.0.0.1 --port 8093
```

Open `http://127.0.0.1:8093/?view=booth`. Live execution is unavailable until the real services are configured. This preview performs no model inference and must not be presented as a live Arm result. Stop the process with Ctrl-C. Never set `VALIDATOR_ISOLATED=1` on the host merely to bypass a skipped test; real isolation is established by the deployment, not the environment variable.

## Build three Arm64 images

Use Docker Buildx with access to a learner-owned Artifact Registry repository. Set `REGISTRY` to `REGION-docker.pkg.dev/YOUR_PROJECT/YOUR_REPOSITORY`, choose a new `TAG`, and set `MODEL_FILE` to your supplied E4B GGUF. Follow `MODEL_SOURCES.md` for the pin and upstream review. No cloud deployment is performed by these commands, but `--push` writes images to the selected registry and can incur storage/build costs.

```bash
export MODEL_FILENAME=gemma-4-E4B-it-Q4_0.gguf
python tools/stage_model.py "$MODEL_FILE"
docker buildx build --platform linux/arm64 -f worker/Dockerfile \
  -t "$REGISTRY/worker:$TAG" --push .
docker buildx build --platform linux/arm64 -f infra/Dockerfile.studio \
  -t "$REGISTRY/studio:$TAG" --push .
docker buildx build --platform linux/arm64 \
  -f "$PWD/infra/Dockerfile.model-local" \
  --build-arg "BASE_IMAGE=$REGISTRY/worker:$TAG" \
  --build-arg "MODEL_FILENAME=$MODEL_FILENAME" \
  -t "$REGISTRY/model:$TAG" --push .build/model
```

Staging checks the exact size/SHA-256 and copies only the chosen model into `.build/model`; do not use your home directory or an unrelated model directory as Docker context. Allow space for the original and staged copies of the approximately 4.6 GB model, plus Docker images and its build cache. Worker and studio Dockerfiles resolve Arm64 wheels on the builder CPU and avoid target-stage `RUN` operations. Both Dockerfiles pin the Python base image by digest. Transitive Python dependency resolution can still change: retain build output, resolved dependency versions, and resulting image digests for each fresh build. The llama server is a separate configured public image; it is not built by these commands.

## Configure and render without cloud calls

Fill the environment variables below with your own project and the actual cluster network ranges. `KUBE_API_CIDR` is the observed control-plane endpoint IPv4 `/32`, not the Kubernetes Service IP. The node, Pod and Service ranges must be private, bounded and non-overlapping. The dedicated namespace is intentionally fixed to `axion-agent-studio` so the checkpoint verifier and identity bindings agree.

```bash
python tools/configure.py --project "$PROJECT_ID" --registry "$REGISTRY" --tag "$TAG" \
  --kube-api-cidr "$KUBE_API_CIDR" --node-cidr "$NODE_CIDR" \
  --pod-cidr "$POD_CIDR" --service-cidr "$SERVICE_CIDR" \
  --output config.local.json
python infra/render.py --config config.local.json --phase full --output generated/full.json
```

`configure.py --help` lists optional hosted-model/location and llama-image/binary arguments. To show verified placement/current kubelet telemetry, supply `--node-addresses node-addresses.local.json`, a JSON object mapping the actual assigned node names to their private IPv4 addresses. The renderer grants observation access only to those exact names and TCP 10250 addresses. An empty map grants no node privileges or kubelet egress; placement remains unknown, and missing gVisor metrics stay unavailable. Do not insert invented node evidence to fill the UI.

The default full placement is manager `n4a-regular`, Backend/Frontend/QA `c4a-sandbox`, validator `n4a-sandbox`. These are values of the custom node label `studio.axion.dev/pool`, not GKE pool names. Configure them on the actual pools so replacements retain them. Every Pod also selects `kubernetes.io/arch=arm64`; execution selects `sandbox.gke.io/runtime=gvisor` and `runtimeClassName=gvisor`. Use exactly one eligible C4A node to reproduce the Learning Path's shared-worker placement. Its allocatable capacity must accommodate the three workers' combined requests of 12.75 CPUs, 25.5 GiB memory, and 21 GiB ephemeral storage, with room for system and runtime overhead. Image storage also consumes node disk space. Checkpoint phase places one Backend worker and the validator on C4A and intentionally leaves the complete workflow unavailable.

Inspect the built images with `docker buildx imagetools inspect` and record their immutable `sha256:...` digests after checking `linux/arm64`. Replace the generated build tags before deployment:

```bash
docker buildx imagetools inspect "$REGISTRY/worker:$TAG"
docker buildx imagetools inspect "$REGISTRY/studio:$TAG"
docker buildx imagetools inspect "$REGISTRY/model:$TAG"
docker buildx imagetools inspect \
  ghcr.io/ggml-org/llama.cpp@sha256:21397f7329ff6fd5705b0bcedcb7e2ce10b5ce97cc12e6564278ac7133523ea6
python tools/pin_images.py --config config.local.json \
  --worker-digest "$WORKER_DIGEST" --studio-digest "$STUDIO_DIGEST" \
  --model-digest "$MODEL_DIGEST"
python infra/render.py --config config.local.json --phase full \
  --require-digests --output generated/full.json
```

## Deploy to an existing compatible cluster

Arrange the prerequisites with the cluster administrator: GKE Standard, Dataplane V2, Workload Identity, regular Arm and gVisor Arm node pools, a running Agent Sandbox controller with the compatible `agents.x-k8s.io/v1beta1` Sandbox CRD and `gvisor` RuntimeClass, sufficient capacity/quotas, registry pull access, and manager-only Vertex access. Keep the emitted network policy, resource caps, service accounts, non-root filesystem controls and exact selectors.

The deployer needs creation, patch, update, and deletion permissions for the dedicated Namespace and manifest resources, including Deployments. Inspection requires read access to Nodes, RuntimeClasses, the Sandbox CRD, controller and application Deployments, Pods, events, EndpointSlices or Endpoints, and Pod logs. Readiness waits require `watch` access to Deployments, Sandboxes, and Pods. Dashboard access requires `create` on `pods/portforward`; the optional checkpoint also requires `create` on `pods/exec` for the manager. An authorized administrator must apply and remove the optional cluster-scoped node observer role and binding.

Set `KUBE_CONTEXT` to the intended existing project/cluster context. Verify the controller's readiness, configured image, and resolved Pod image IDs, using its actual Deployment and namespace names. Record these with the CRD schema and cluster version. An installed CRD alone does not establish a functioning controller.

Choose the phase before the initial application deployment. The full phase runs all three workers and requires the N4A validator pool. The optional checkpoint runs one backend worker and the validator on C4A, without calling hosted Gemini. Set `PHASE=checkpoint` only for that narrower test:

```bash
export PHASE=full
export DEPLOYMENT_MANIFEST="generated/$PHASE.json"
python infra/render.py --config config.local.json --phase "$PHASE" \
  --require-digests --output "$DEPLOYMENT_MANIFEST"
kubectl --context="$KUBE_CONTEXT" get namespace axion-agent-studio
```

The namespace lookup must report `NotFound`. If the namespace exists, use another existing compatible cluster or coordinate with its owner before proceeding. Inspect the rendered resources, then create the dedicated namespace before server-side dry-run; dry-running namespace creation does not make it available to later objects:

```bash
kubectl --context="$KUBE_CONTEXT" create namespace axion-agent-studio
kubectl --context="$KUBE_CONTEXT" apply --dry-run=server -f "$DEPLOYMENT_MANIFEST"
```

Resolve admission errors before applying the workloads. After dry-run succeeds, apply the reviewed manifest and wait for its manager and Sandboxes:

```bash
kubectl --context="$KUBE_CONTEXT" apply -f "$DEPLOYMENT_MANIFEST"
kubectl --context="$KUBE_CONTEXT" -n axion-agent-studio rollout status \
  deployment/studio-manager --timeout=300s
kubectl --context="$KUBE_CONTEXT" -n axion-agent-studio wait \
  --for=condition=Ready sandboxes.agents.x-k8s.io --all --timeout=900s
kubectl --context="$KUBE_CONTEXT" -n axion-agent-studio wait \
  --for=condition=Ready pod -l app=axion-agent-studio --timeout=300s
kubectl --context="$KUBE_CONTEXT" -n axion-agent-studio get sandbox,pods,services -o wide
```

Expect four Ready Sandboxes for `full`, or two for `checkpoint`. Check actual Pod node assignments against Arm64 Node labels and machine types, verify `runtimeClassName=gvisor` for execution Pods, and record resolved container image IDs alongside the configured pinned images. The model-copy init containers and local model health must pass before inference. A dry-run does not test the Pods subsequently created by the controller. If readiness times out, inspect Sandbox conditions, Pod events, and logs.

For the full workflow, open a second terminal with the same `KUBE_CONTEXT` and forward the regular manager service:

```bash
kubectl --context="$KUBE_CONTEXT" -n axion-agent-studio port-forward \
  --address=127.0.0.1 service/studio-manager 8090:8080
```

Open `http://127.0.0.1:8090/?view=booth`. Require `/api/config` to report `live_available: true` before the full run. That check verifies service health and the presence of hosted-model configuration; the first live planning request tests actual Gemini credentials and access. GKE Sandbox Pods do not support direct `kubectl port-forward`; reach workers and validator from the regular manager.

If you deployed the checkpoint phase, run its verifier through the manager:

```bash
kubectl --context="$KUBE_CONTEXT" -n axion-agent-studio exec deployment/studio-manager -- \
  python scripts/verify_live_checkpoint.py --in-cluster \
  --worker-url http://worker-backend:8081 --validator-url http://validator:8081 \
  --expect-family c4a --project /app --output /data/checkpoint.json
```

Export checkpoint evidence before replacing the installation. To start a fresh full run after the checkpoint, remove the dedicated checkpoint installation using its reviewed manifest, wait for its namespace deletion, and repeat deployment with `PHASE=full`. Verify the validator now runs on N4A. Its accepted execution state is in memory. A successful checkpoint covers backend inference and validation only; the complete Learning Path also requires hosted planning, frontend and QA workers, checkout interaction, and metrics evidence.

## Costs and cleanup

Every provisioned node remains billable while idle. Count the actual manager, worker, validator and any retained system/default nodes, boot disks, control-plane charges where applicable, registry/model storage, builds, logs, egress and hosted Gemini calls. ResourceQuota does not cap VM or API bills. Use current calculator inputs rather than historical hourly estimates.

Stop the port-forward and export the run results, artifacts, metrics, and placement evidence. With the same authorized deployer or administrator, confirm the context and namespace contents before removing the dedicated application installation:

```bash
kubectl --context="$KUBE_CONTEXT" -n axion-agent-studio get pods,services,sandboxes.agents.x-k8s.io
kubectl --context="$KUBE_CONTEXT" delete -f "$DEPLOYMENT_MANIFEST"
kubectl --context="$KUBE_CONTEXT" wait --for=delete \
  namespace/axion-agent-studio --timeout=300s
```

The manifest includes the namespace and any generated node observer ClusterRole and ClusterRoleBinding. Namespace deletion removes everything inside it; coordinate first if others have added resources. Verify that the namespace and any generated observer RBAC have been removed.

The existing cluster, node pools, network, and registry are prerequisites and remain in place. Deleting the namespace or closing the UI does not stop node billing. Have the cluster owner scale down or remove only temporary tutorial capacity according to the organization's procedure; do not stop individual GKE-managed VMs. Remove only tutorial-specific image versions and an IAM principal binding created solely for this exercise when they are no longer needed. Manager run data is ephemeral and validator state is in memory, so restarts or deletion lose new results.
