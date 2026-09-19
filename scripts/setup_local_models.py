#!/usr/bin/env python3
"""Install a separate local semantic environment and checksum-pinned models.

Downloads public software/model artifacts only, with no authentication tokens.
Never opens originals, the upload queue, private config, or worker state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request

SILERO_REV = "be95df9152c0d7618fa1edfeb296fc3dae32376f"
ASR_REV = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
WHISPER_REV = "86098128c0b4f24f0e2aa2994de830614b474227"
MLX_EXAMPLES_REV = "796f5b53cab69a3d48a44233ce21aae889e94a08"
ASR_REPO = "mlx-community/whisper-large-v3-turbo"
ASR_DIR = "whisper-large-v3-turbo-" + ASR_REV[:12]
VAD_DIR = "silero-v6.2-" + SILERO_REV[:12]
GITHUB = "https://raw.githubusercontent.com"
HF = f"https://huggingface.co/{ASR_REPO}/resolve/{ASR_REV}"
# Commit-pinned bytes verified against their actual LICENSE, not README badges.
ARTIFACTS = [
    (f"{VAD_DIR}/LICENSE", f"{GITHUB}/snakers4/silero-vad/{SILERO_REV}/LICENSE", "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"),
    (f"{ASR_DIR}/LICENSE.openai-whisper", f"{GITHUB}/openai/whisper/{WHISPER_REV}/LICENSE", "b5d65a59060e68c4ff940e1eddfa6f94b2d68fdf58ed7f4dd57721c997e35e9d"),
    (f"{ASR_DIR}/LICENSE.mlx-examples", f"{GITHUB}/ml-explore/mlx-examples/{MLX_EXAMPLES_REV}/LICENSE", "ccfab7ccb2ea306f71531c8ca77bb55507606cd90768b1e32b8b52ab5b48cf01"),
    (f"{VAD_DIR}/silero_vad.onnx", f"{GITHUB}/snakers4/silero-vad/{SILERO_REV}/src/silero_vad/data/silero_vad.onnx", "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
    (f"{ASR_DIR}/config.json", HF + "/config.json", "b34fc29e4e11e0a25e812775dd67f4dd16fc2c8eb43d28ae25ff7d660ecb6379"),
    (f"{ASR_DIR}/README.md", HF + "/README.md", "78f2d3dc65072211d957fc1b646c2d96e8e8f96dd45ceb2ac5f2643d0c5c05da"),
    (f"{ASR_DIR}/weights.safetensors", HF + "/weights.safetensors", "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6"),
]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_verified(url, target, expected, offline=False):
    if target.exists():
        if sha256(target) != expected:
            raise ValueError("existing model artifact checksum mismatch: " + target.name)
        return
    if offline:
        raise FileNotFoundError("offline verification: missing " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    print("Downloading " + target.name, flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "HemoryLocal-model-setup/1"})
    # Public GET only: no token lookup, cookie jar or netrc authentication.
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    if sha256(temporary) != expected:
        raise ValueError("download checksum mismatch: " + target.name)
    temporary.replace(target)


def ensure_environment(project, python=None, offline=False):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("this locked MLX environment requires Apple Silicon macOS")
    environment = project / ".venv-semantic"
    executable = environment / "bin/python"
    if not executable.exists():
        if offline:
            raise FileNotFoundError("offline verification: .venv-semantic missing")
        interpreter = python or shutil.which("python3.13")
        if not interpreter:
            raise RuntimeError("Python 3.13 is required for the checked dependency lock")
        version = subprocess.check_output([interpreter, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], text=True).strip()
        if version != "3.13":
            raise RuntimeError("use Python 3.13; the dependency lock was verified on 3.13")
        subprocess.run([interpreter, "-m", "venv", str(environment)], check=True)
    version = subprocess.check_output([str(executable), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], text=True).strip()
    if version != "3.13":
        raise RuntimeError("existing .venv-semantic must use Python 3.13")
    if not offline:
        subprocess.run([str(executable), "-m", "pip", "install", "-r", str(project / "requirements-semantic.txt")], check=True)
    subprocess.run([str(executable), "-m", "pip", "check"], check=True)
    locked = dict(line.strip().split("==", 1) for line in (project / "requirements-semantic.txt").read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#"))
    verify = "import importlib.metadata,json,sys; names=json.loads(sys.argv[1]); print(json.dumps({n:importlib.metadata.version(n) for n in names}))"
    installed = json.loads(subprocess.check_output([str(executable), "-c", verify, json.dumps(list(locked))], text=True))
    if installed != locked:
        raise RuntimeError("installed semantic dependencies differ from the checked lock")
    return executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path.home() / "Library/Application Support/HemoryLocal")
    parser.add_argument("--python", help="Python 3.13 executable for a new venv")
    parser.add_argument("--offline", action="store_true", help="verify existing files only; perform no downloads/install")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    executable = ensure_environment(project, args.python, args.offline)
    models = args.data_root.expanduser().resolve() / "semantic/models"
    if not args.offline:
        models.mkdir(parents=True, exist_ok=True)
    artifacts = []
    # LICENSEs are retrieved and hash-verified before any weights.
    for relative, url, expected in ARTIFACTS:
        target = models / relative
        download_verified(url, target, expected, args.offline)
        if "LICENSE" in target.name:
            license_text = target.read_text()
            if "MIT License" not in license_text or "Permission is hereby granted" not in license_text:
                raise ValueError("unexpected license: " + target.name)
        artifacts.append({"path": relative, "sha256": expected, "bytes": target.stat().st_size, "url": url})
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise FileNotFoundError("install FFmpeg/ffprobe before using semantic audio")
    config = {"vad_model_path": str(models / VAD_DIR / "silero_vad.onnx"),
              "asr_model_path": str(models / ASR_DIR), "language": "zh",
              "ffmpeg_path": ffmpeg, "ffprobe_path": shutil.which("ffprobe")}
    manifest = {"schema_version": 1, "python": "3.13", "venv_python": str(executable),
                "dependency_lock_sha256": sha256(project / "requirements-semantic.txt"),
                "silero": {"repository": "snakers4/silero-vad", "revision": SILERO_REV, "license": "MIT"},
                "asr": {"repository": ASR_REPO, "revision": ASR_REV, "upstream": "openai/whisper-large-v3-turbo",
                        "upstream_model_revision": "41f01f3fe87f28c78e2fbf8b568835947dd65ed9",
                        "license_basis": "OpenAI Whisper MIT (code and weights); converted repository has no standalone LICENSE",
                        "openai_code_revision": WHISPER_REV, "mlx_examples_revision": MLX_EXAMPLES_REV},
                "artifacts": artifacts, "config": config}
    if not args.offline:
        for name, value in (("manifest.json", manifest), ("local-model-config.json", config)):
            temporary = models / (name + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(models / name)
    print(json.dumps({"verified": True, "model_root": str(models), "venv_python": str(executable), "config": config}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
