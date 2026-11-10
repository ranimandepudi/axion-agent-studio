# Axion Agent Studio source notices

This companion is sample code for Arm Learning Paths. Its sample code is
available under the BSD-3-Clause-Clear license in `LICENSE-SAMPLECODE`. Its
documentation is available under the Creative Commons Attribution-ShareAlike
4.0 International license in `LICENSE`. `LICENSE-SUMMARY` describes this split.
These three files are copied unchanged from the Arm Learning Paths repository.
Retain the applicable copyright notices, license conditions and disclaimers
when redistributing the source or images built from it.

This source package contains the application, prepared example shop, tests and
build/configuration tooling. It excludes credentials, private deployment
configuration, recorded runs, model weights and third-party package binaries.
See `VALIDATION.md` for the recorded verification coverage.

## External dependencies

The requirement files identify Python packages installed separately by the
learner or image build. Those packages and their transitive dependencies retain
their own licenses. The upstream projects for the directly used dependencies
are:

- [FastAPI](https://github.com/fastapi/fastapi),
  [Uvicorn](https://github.com/Kludex/uvicorn),
  [HTTPX](https://github.com/encode/httpx), and
  [Pydantic](https://github.com/pydantic/pydantic).
- [Google Auth](https://github.com/googleapis/google-auth-library-python),
  [psutil](https://github.com/giampaolo/psutil),
  [PyYAML](https://github.com/yaml/pyyaml), and
  [Requests](https://github.com/psf/requests).
- [pytest](https://github.com/pytest-dev/pytest) and
  [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) for tests.

The Dockerfiles preserve the installed packages' metadata and license files
when copying `/deps` to `/opt/deps`. The Python and operating-system base image
also contains separately licensed components; see the
[Docker Official Python image](https://hub.docker.com/_/python) and the licenses
included in the selected image. Retain upstream notices if you redistribute
these dependencies or base-image contents. This notice does not relicense them.

## Models and services

`MODEL_SOURCES.md` records the model file, immutable revision, checksum and
llama.cpp image used by the example. Model weights and the llama.cpp server
image are obtained separately.

- The selected [Gemma 4 E4B GGUF model card](https://huggingface.co/ggml-org/gemma-4-E4B-it-GGUF/blob/b8093469224f83f5c38f691eb906c380e9e63114/README.md)
  declares Apache-2.0. Google's [Gemma 4 license](https://ai.google.dev/gemma/apache_2)
  supplies the license text. When redistributing model weights, include their
  license and preserve any applicable upstream attribution notices.
- [llama.cpp](https://github.com/ggml-org/llama.cpp) has its own
  [license at the recorded source revision](https://github.com/ggml-org/llama.cpp/blob/4413a192410fad1f054e9216114087a3e01527a0/LICENSE)
  and third-party notices. Those terms apply to the separately obtained server
  image and its contents.
- Hosted Gemini calls use a separately configured service. See the
  [Google Cloud terms](https://cloud.google.com/terms) for Vertex AI and the
  [Gemini API terms](https://ai.google.dev/gemini-api/terms) for the optional
  API-key backend.

The model storage image uses the built worker image as `BASE_IMAGE` and inherits
its application license files. Its model weights remain subject to their own
license. Product names identify the technologies used; this package does not
grant trademark rights or imply endorsement.
