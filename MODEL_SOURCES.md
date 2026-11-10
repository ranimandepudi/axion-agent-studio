# Model sources and licenses

The default is the pinned Gemma 4 E4B text GGUF below. Model weights and the llama.cpp server image are obtained separately and are not included in this source package. See `NOTICE.md` for the application's license and external dependency notices, and `VALIDATION.md` for recorded runtime verification.

| Field | Pinned value |
| --- | --- |
| Model repository | `ggml-org/gemma-4-E4B-it-GGUF` |
| Revision | `b8093469224f83f5c38f691eb906c380e9e63114` |
| File | `gemma-4-E4B-it-Q4_0.gguf` |
| Bytes | `4590807392` |
| SHA-256 | `a555b900214b477d8880e7832e0b8925e139b0159640036b09fe472b6f2097f2` |
| llama image | `ghcr.io/ggml-org/llama.cpp@sha256:21397f7329ff6fd5705b0bcedcb7e2ce10b5ce97cc12e6564278ac7133523ea6` |
| Historical image platform/version | Linux Arm64, b11580 |
| Historical image source revision | `4413a192410fad1f054e9216114087a3e01527a0` |
| Server binary | `/app/llama-server` |

The downloader retains the pinned E2B variant for explicit developer tests, but E2B is not this tutorial's baseline. The E4B filename, alias and checksum are consistent across configuration, staging and image build. Do not substitute a model without updating and revalidating the contract.

Sources: [pinned model tree](https://huggingface.co/ggml-org/gemma-4-E4B-it-GGUF/tree/b8093469224f83f5c38f691eb906c380e9e63114), [Google llama.cpp integration](https://ai.google.dev/gemma/docs/integrations/llamacpp), and [pinned llama image documentation](https://github.com/ggml-org/llama.cpp/blob/4413a192410fad1f054e9216114087a3e01527a0/docs/docker.md). Source pins establish the selected inputs; runtime checks establish whether they work in the chosen deployment.

The [model card at the pinned revision](https://huggingface.co/ggml-org/gemma-4-E4B-it-GGUF/blob/b8093469224f83f5c38f691eb906c380e9e63114/README.md) declares `apache-2.0` and identifies the Google Gemma 4 E4B source models. Google's [Gemma 4 license](https://ai.google.dev/gemma/apache_2) provides the Apache 2.0 terms. If you redistribute the GGUF or an image containing it, include that license and retain applicable upstream notices. The sample-code license does not replace the model license. The separately obtained llama.cpp server retains its [upstream license](https://github.com/ggml-org/llama.cpp/blob/4413a192410fad1f054e9216114087a3e01527a0/LICENSE) and dependency notices.

Supply the exact GGUF locally or use `python worker/download_model.py --model e4b`; the downloader retrieves approximately 4.6 GB and verifies the pin. Neither access tokens nor model weights belong in the source archive.
