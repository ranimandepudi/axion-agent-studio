"""Downloader integrity tests use tiny explicit fake model bytes, never a model."""
import hashlib
import io

import pytest

from worker import download_model as downloader


@pytest.fixture
def tiny_models(monkeypatch):
    blobs = {"e2b": b"test E2B bytes", "e4b": b"test E4B bytes"}
    specs = {name: downloader.Model("test/fixture", "test-revision", name + ".gguf",
                                    len(blob), hashlib.sha256(blob).hexdigest())
             for name, blob in blobs.items()}
    monkeypatch.setattr(downloader, "MODELS", specs)
    return blobs, specs


def test_default_verify_uses_e4b_and_selected_model_changes_hash(tmp_path, tiny_models):
    blobs, _ = tiny_models
    path = tmp_path / "model.gguf"
    path.write_bytes(blobs["e2b"])
    assert downloader.verify(path, "e2b")
    assert not downloader.verify(path)
    path.write_bytes(blobs["e4b"])
    assert downloader.verify(path, "e4b")


@pytest.mark.parametrize("variant", ["e2b", "e4b"])
def test_selected_model_uses_only_its_pinned_url_and_atomic_verified_output(tmp_path, monkeypatch,
                                                                         tiny_models, variant):
    blobs, specs = tiny_models
    seen = []

    def fake_download(url, timeout):
        seen.append((url, timeout))
        return io.BytesIO(blobs[variant])

    monkeypatch.setattr(downloader.urllib.request, "urlopen", fake_download)
    path = tmp_path / "model.gguf"
    args = [str(path)] if variant == "e4b" else ["--model", variant, str(path)]
    downloader.main(args)
    assert seen == [(specs[variant].url, 60)]
    assert path.read_bytes() == blobs[variant]
    assert not path.with_suffix(".partial").exists()


@pytest.mark.parametrize("bad_bytes", [b"wrong contents", b"too many bytes to match any of the model fixtures"])
def test_invalid_download_preserves_existing_target_and_cleans_partial(tmp_path, monkeypatch,
                                                                      tiny_models, bad_bytes):
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(bad_bytes))
    path = tmp_path / "model.gguf"
    path.write_bytes(b"prior existing artifact")
    with pytest.raises(RuntimeError):
        downloader.main(["--model", "e4b", str(path)])
    assert path.read_bytes() == b"prior existing artifact"
    assert not path.with_suffix(".partial").exists()


def test_unknown_variant_is_rejected_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Unexpected network call"))
    with pytest.raises(SystemExit):
        downloader.main(["--model", "https://arbitrary.example/model", str(tmp_path / "model.gguf")])


def test_verified_existing_model_does_not_download_again(tmp_path, monkeypatch, tiny_models):
    blobs, _ = tiny_models
    path = tmp_path / "model.gguf"
    path.write_bytes(blobs["e4b"])
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Unexpected network call"))
    downloader.main([str(path), "--model", "e4b"])

