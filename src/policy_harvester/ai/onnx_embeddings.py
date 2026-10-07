"""Internal embeddings: ONNX models run on the CPU with onnxruntime, no external provider.

Each supported model has a spec: where its ONNX graph and tokenizer live, how a sentence vector
is formed (mean pooling over tokens, or the graph's own ``sentence_embedding`` output), and the
prefixes it was trained with for queries and documents. Files are read from ``ONNX_MODELS_DIR``
(baked into the image) and downloaded there only when ``HF_HUB_OFFLINE`` allows it.
"""
from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

EmbeddingKind = Literal["query", "document"]


@dataclass(frozen=True)
class OnnxModelSpec:
    repo: str
    model_file: str
    tokenizer_file: str
    dimensions: int
    pooling: Literal["mean", "sentence_embedding"]
    query_prefix: str
    document_prefix: str
    max_tokens: int = 512
    extra_files: tuple[str, ...] = ()
    # Matryoshka-trained models keep quality when the vector is cut to a prefix and renormalized.
    truncatable_to: tuple[int, ...] = ()
    license: str = ""


ONNX_MODELS: dict[str, OnnxModelSpec] = {
    "intfloat/multilingual-e5-small": OnnxModelSpec(
        "intfloat/multilingual-e5-small", "onnx/model.onnx", "onnx/tokenizer.json", 384, "mean",
        "query: ", "passage: ", license="MIT"),
    "intfloat/multilingual-e5-large": OnnxModelSpec(
        "qdrant/multilingual-e5-large-onnx", "model.onnx", "tokenizer.json", 1024, "mean",
        "query: ", "passage: ", extra_files=("model.onnx_data",), license="MIT"),
    "google/embeddinggemma-300m": OnnxModelSpec(
        "onnx-community/embeddinggemma-300m-ONNX", "onnx/model.onnx", "tokenizer.json", 768,
        "sentence_embedding", "task: search result | query: ", "title: none | text: ",
        max_tokens=1024, extra_files=("onnx/model.onnx_data",), truncatable_to=(512, 256, 128),
        license="Gemma Terms of Use"),
    "google/embeddinggemma-300m-q8": OnnxModelSpec(
        "onnx-community/embeddinggemma-300m-ONNX", "onnx/model_quantized.onnx", "tokenizer.json", 768,
        "sentence_embedding", "task: search result | query: ", "title: none | text: ",
        max_tokens=1024, extra_files=("onnx/model_quantized.onnx_data",), truncatable_to=(512, 256, 128),
        license="Gemma Terms of Use"),
}


def models_dir() -> Path:
    return Path(os.environ.get("ONNX_MODELS_DIR", "/opt/embedding-models"))


def model_path(spec: OnnxModelSpec) -> Path:
    return models_dir() / spec.repo.replace("/", "--")


def download(spec: OnnxModelSpec) -> Path:
    """Fetch the files a spec needs into a plain directory (no symlinks: onnxruntime refuses
    external weight files that resolve outside the model directory)."""
    from huggingface_hub import hf_hub_download

    target = model_path(spec)
    for name in (spec.model_file, spec.tokenizer_file, *spec.extra_files):
        if not (target / name).exists():
            hf_hub_download(spec.repo, name, local_dir=target)
    return target


class _LoadedModel:
    def __init__(self, spec: OnnxModelSpec, threads: int):
        import onnxruntime
        from tokenizers import Tokenizer

        directory = model_path(spec)
        if not (directory / spec.model_file).exists():
            if os.environ.get("HF_HUB_OFFLINE") == "1":
                raise FileNotFoundError(f"{spec.repo} is not installed in {models_dir()}")
            directory = download(spec)
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = onnxruntime.InferenceSession(str(directory / spec.model_file), options,
                                                    providers=["CPUExecutionProvider"])
        self.inputs = {item.name for item in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(directory / spec.tokenizer_file))
        self.tokenizer.enable_truncation(spec.max_tokens)
        pad_id = self.tokenizer.token_to_id("<pad>")
        self.tokenizer.enable_padding(pad_id=pad_id if pad_id is not None else 0, pad_token="<pad>")
        self.spec = spec
        self.lock = threading.Lock()

    def encode(self, texts: list[str]) -> Any:
        import numpy as np

        encodings = self.tokenizer.encode_batch(texts)
        feed = {"input_ids": np.array([item.ids for item in encodings], dtype=np.int64),
                "attention_mask": np.array([item.attention_mask for item in encodings], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        with self.lock:  # one inference at a time per session; it already uses every thread
            outputs = self.session.run(None, feed)
        if self.spec.pooling == "sentence_embedding":
            names = [item.name for item in self.session.get_outputs()]
            vectors = outputs[names.index("sentence_embedding")]
        else:
            mask = feed["attention_mask"][..., None].astype(np.float32)
            vectors = (outputs[0] * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        return vectors


class OnnxEmbeddingProvider:
    """``embedding.provider = onnx``: the model name selects an entry of ``ONNX_MODELS``."""
    name = "onnx"
    _models: dict[str, _LoadedModel] = {}
    _guard = threading.Lock()

    def __init__(self, settings: Any):
        self.threads = getattr(settings, "onnx_threads", None) or os.cpu_count() or 4
        self.batch_size = 16

    def _model(self, model: str) -> _LoadedModel:
        spec = ONNX_MODELS.get(model)
        if spec is None:
            raise ValueError(f"unknown ONNX embedding model {model!r}; known: {', '.join(ONNX_MODELS)}")
        with self._guard:
            if model not in self._models:
                self._models[model] = _LoadedModel(spec, self.threads)
            return self._models[model]

    async def embed(self, *, model: str, texts: list[str], dimensions: int,
                    kind: EmbeddingKind = "document") -> list[list[float]]:
        import numpy as np

        loaded = await asyncio.to_thread(self._model, model)
        spec = loaded.spec
        if dimensions != spec.dimensions and dimensions not in spec.truncatable_to:
            raise ValueError(f"{model} gives {spec.dimensions} dimensions"
                             + (f" (or {spec.truncatable_to})" if spec.truncatable_to else ""))
        prefix = spec.query_prefix if kind == "query" else spec.document_prefix
        prepared = [prefix + text for text in texts]
        batches = [prepared[index:index + self.batch_size]
                   for index in range(0, len(prepared), self.batch_size)]
        vectors = np.concatenate([await asyncio.to_thread(loaded.encode, batch) for batch in batches])
        vectors = vectors[:, :dimensions]
        vectors = vectors / np.clip(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12, None)
        return vectors.tolist()

    async def test_connection(self, model: str) -> dict[str, Any]:
        spec = ONNX_MODELS[model]
        vector = (await self.embed(model=model, texts=["연결 확인"], dimensions=spec.dimensions))[0]
        return {"ok": True, "provider": self.name, "model": model, "dimensions": len(vector)}
