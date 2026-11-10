# Companion validation

Release candidate: 2026-11-09. The checks below cover the portable companion source, local Linux Arm64 containers, and local application contracts. A fresh GKE walkthrough remains pending. These results do not establish real Gemini access, Gemma output quality, GKE isolation, or clean-cloud reproducibility.

| Check | Environment | Recorded result |
| --- | --- | --- |
| Application, worker, validator, helper, and manifest tests | Local Linux `aarch64` container, Python 3.12.16 | 414 passed; 15 subtests passed; one deprecation warning |
| Same Python suite on the workstation | macOS Arm64, Python 3.12.13 | 411 passed; three Linux-only validator tests skipped; 15 subtests passed; one deprecation warning |
| Frontend tests | Local Linux container, Node.js 22 | 76 passed; no failures or skips |
| Worker image build | Docker Buildx, `linux/arm64` | Passed; local image loaded |
| Studio image build | Docker Buildx, `linux/arm64` | Passed; local image loaded |
| Worker and studio Python/application imports | Local Linux `aarch64` containers, Python 3.12.16 | Passed; resolved dependency versions recorded |
| Portable configuration, both manifest phases, digest requirements, model-byte rejection, and telemetry address handling | Included in the Python suites | Passed with synthetic project/network fixtures |
| Model-storage image, registry upload, live model inference, GKE admission/deployment, checkout interaction, telemetry, and cloud cleanup | Fresh tutorial environment | Not run |

The Linux and macOS suites overlap; do not add their totals as unique tests. Unit tests use controlled fixtures and HTTP doubles. The warning concerns the existing Starlette/HTTPX test-client interface and did not fail the suite. The Linux tests ran locally in Docker, not on Google Axion or under GKE gVisor.

The builds resolved the Python base to `python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df`. Both Dockerfiles pin that same digest, and both were rebuilt after pinning. The final local build outputs are:

| Image | Image index digest |
| --- | --- |
| Worker | `sha256:c975f50b6f81b7ef67050dcc026e21d5ef296497cf8a8f1666d7987050f03936` |
| Studio | `sha256:58c04c005793d3e22718a72c76e1ed8ab282759e069ef8cd7b871ab35ab301d8` |

These local image names/digests are validation records, not public image download locations. Build your own images, inspect their architecture, and pin the resulting registry digests. A fresh build can resolve different transitive Python dependencies.

The source release includes the repository's sample-code and documentation licenses plus separate dependency/model notices. It excludes credentials, private deployment configuration, recorded application runs, model weights, dependency binaries, caches, and workstation paths. Model weights and the llama.cpp server image are obtained separately under their upstream terms.

Before enabling Learning Path publication, reproduce the complete instructions from this source release on an existing compatible GKE Arm cluster. Capture controller and image versions, real Gemini planning, all three local Gemma workers, independent validation, original/updated checkout interactions, actual Arm node placement, CPU samples, and scoped cleanup. Preserve failures and skips alongside passes.
