"""Download one explicitly selected, pinned public Gemma GGUF and verify it."""
import argparse
from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import urllib.request


@dataclass(frozen=True)
class Model:
    repository: str
    revision: str
    filename: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repository}/resolve/{self.revision}/{self.filename}"


MODELS = {
    "e2b": Model("ggml-org/gemma-4-E2B-it-GGUF", "b4243c156154b6dca9324415f8c7ccc098b4aed1",
                 "gemma-4-E2B-it-Q4_0.gguf", 2_841_481_184,
                 "8e30dff3ac4c8434c49a7036fa15564bdbb6044e42bf04550bf1a096ad7e6a52"),
    "e4b": Model("ggml-org/gemma-4-E4B-it-GGUF", "b8093469224f83f5c38f691eb906c380e9e63114",
                 "gemma-4-E4B-it-Q4_0.gguf", 4_590_807_392,
                 "a555b900214b477d8880e7832e0b8925e139b0159640036b09fe472b6f2097f2"),
}

# E2B constants retained for explicit callers; the local review default is E4B.
REVISION = MODELS["e2b"].revision
FILENAME = MODELS["e2b"].filename
EXPECTED_SIZE = MODELS["e2b"].size
SHA256 = MODELS["e2b"].sha256
URL = MODELS["e2b"].url


def model_spec(name: str) -> Model:
    if name not in MODELS:
        raise ValueError("Only pinned models e2b and e4b are supported")
    return MODELS[name]


def verify(path: Path, model: str = "e4b") -> bool:
    spec = model_spec(model)
    if not path.exists() or path.stat().st_size != spec.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == spec.sha256


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", type=Path, help="Output GGUF path; defaults to models/<pinned filename>")
    parser.add_argument("--model", choices=tuple(MODELS), default="e4b", help="Pinned model variant (default: e4b)")
    args = parser.parse_args(argv)
    spec = model_spec(args.model)
    target = args.target or Path("models") / spec.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if verify(target, args.model):
        print(f"Verified existing {target}")
        return
    partial = target.with_suffix(".partial")
    try:
        with urllib.request.urlopen(spec.url, timeout=60) as response, partial.open("wb") as output:
            total = 0
            while chunk := response.read(8 * 1024 * 1024):
                output.write(chunk)
                total += len(chunk)
                if total > spec.size:
                    raise RuntimeError("Download exceeds expected size")
        if not verify(partial, args.model):
            raise RuntimeError("Model checksum or size verification failed")
        os.replace(partial, target)
        print(f"Verified {target}: {spec.size} bytes; SHA256 {spec.sha256}")
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
