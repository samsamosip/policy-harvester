from __future__ import annotations

import base64
import json
import re
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import quote
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError
import httpx

from ..config import Settings, get_settings

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class CapabilityError(RuntimeError):
    pass


class TransientProviderError(RuntimeError):
    """The provider is temporarily unavailable; the job should wait and try again."""


REVIEW_MAX_OUTPUT_TOKENS = 32000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def is_transient(exc: BaseException) -> bool:
    """Rate limits, overload, 5xx, timeouts and dropped connections: worth retrying later.

    Validation failures and 4xx request errors are not; retrying the same request cannot help.
    """
    if isinstance(exc, TransientProviderError):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(status, int):
        # An error event inside a stream arrives as a bare APIError: OpenRouter's gateway
        # timeouts read "error code: 504" with no HTTP status attached.
        code = getattr(exc, "code", None)
        found = re.search(r"\berror code:?\s*(\d{3})\b", str(exc), flags=re.IGNORECASE)
        status = int(code) if isinstance(code, int) or (isinstance(code, str) and code.isdigit()) \
            else int(found.group(1)) if found else None
    if isinstance(status, int):
        return status in TRANSIENT_STATUS
    name = exc.__class__.__name__
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError",
                "ServiceUnavailableError", "OverloadedError"}:
        return True
    import httpx

    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


# Every request/response pair of the current task, when a caller wants them persisted.
EXCHANGE_LOG: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_exchange_log", default=None)


def start_exchange_log() -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    EXCHANGE_LOG.set(log)
    return log


async def _recorded(purpose: str, provider: str, call: Any, request: dict[str, Any]) -> Any:
    """Run one provider call and keep its raw request and response (or error) in the log."""
    log = EXCHANGE_LOG.get()
    started = time.monotonic()
    entry = {"purpose": purpose, "provider": provider, "model": request.get("model"),
             "request": request, "started_at": datetime.now(UTC).isoformat()}
    try:
        response = await call(**request)
    except Exception as exc:
        if log is not None:
            body = getattr(exc, "body", None)
            entry.update(status="error", error=f"{exc.__class__.__name__}: {exc}"[:4000],
                         response=body if isinstance(body, (dict, list, str)) else None,
                         latency_ms=int((time.monotonic() - started) * 1000))
            log.append(entry)
        raise
    if log is not None:
        entry.update(status="ok", latency_ms=int((time.monotonic() - started) * 1000),
                     response=response.model_dump(mode="json") if hasattr(response, "model_dump") else response)
        log.append(entry)
    return response


_JSON_SCHEMA_REJECTED: set[tuple[str, str]] = set()
_JSON_OBJECT_REJECTED: set[tuple[str, str]] = set()


class StructuredOutputError(ValueError):
    """Structured output stayed invalid; carries every raw response so nothing is discarded."""

    def __init__(self, message: str, raw_response: dict[str, Any]):
        super().__init__(message)
        self.raw_response = raw_response


def _strip_trailing_commas(content: str) -> str:
    output: list[str] = []
    in_string = escaped = False
    for index, char in enumerate(content):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "," and content[index + 1:].lstrip()[:1] in {"}", "]"}:
            continue
        output.append(char)
    return "".join(output)


def validate_lenient_json(schema: type[SchemaT], content: str) -> SchemaT:
    """Validate JSON after purely syntactic cleanup (fences, trailing commas) and the schema's
    own deterministic normalization, if it defines ``normalize_payload``."""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            data = json.loads(_strip_trailing_commas(cleaned))
        except json.JSONDecodeError:
            return schema.model_validate_json(content)  # raises the pydantic json_invalid error
    normalize = getattr(schema, "normalize_payload", None)
    if normalize is not None:
        data, _ = normalize(data)
    return schema.model_validate(data)


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    value: BaseModel
    raw_response: dict[str, Any]
    usage: Usage
    provider: str
    model: str


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    raw_response: dict[str, Any]
    usage: Usage
    model: str


class LLMProvider(Protocol):
    name: str
    async def transcribe_image(self, *, model: str, image: bytes, mime: str, prompt: str,
                               parameters: dict[str, Any] | None = None) -> TranscriptionResult: ...
    async def structured(self, *, model: str, system: str, payload: dict[str, Any],
                         schema: type[SchemaT], parameters: dict[str, Any]) -> GenerationResult: ...
    async def test_connection(self, model: str) -> dict[str, Any]: ...


class EmbeddingProvider(Protocol):
    name: str
    async def embed(self, *, model: str, texts: list[str], dimensions: int,
                    kind: str = "document") -> list[list[float]]: ...
    async def test_connection(self, model: str) -> dict[str, Any]: ...


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings, *, compatible: bool = False,
                 capability: str = "llm"):
        from openai import AsyncOpenAI

        self.name = "openai_compatible" if compatible else "openai"
        api_key = (settings.embedding_api_key if capability == "embedding" else settings.llm_api_key)
        api_key = api_key or settings.llm_api_key or settings.embedding_api_key
        if api_key is None:
            raise CapabilityError(f"{self.name} API key is not configured")
        base_url = (settings.embedding_base_url if capability == "embedding"
                    else settings.llm_base_url)
        self.client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self.settings = settings
        self.compatible = compatible
        self.capability = capability

    async def _chat(self, purpose: str, request: dict[str, Any]) -> Any:
        """One chat completion, streamed when configured; the result is the same completion object."""
        if not self.settings.llm_stream:
            return await _recorded(purpose, self.name, self.client.chat.completions.create, request)
        request = {**request, "stream": True, "stream_options": {"include_usage": True}}
        return await _recorded(purpose, self.name, self._streamed_completion, request)

    async def _streamed_completion(self, **request: Any) -> Any:
        from openai.lib.streaming.chat import ChatCompletionStreamState

        state = ChatCompletionStreamState()
        async for chunk in await self.client.chat.completions.create(**request):
            state.handle_chunk(chunk)
        return state.get_final_completion()

    async def structured(self, *, model: str, system: str, payload: dict[str, Any],
                         schema: type[SchemaT], parameters: dict[str, Any]) -> GenerationResult:
        if self.compatible:
            from openai import BadRequestError

            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            endpoint = (str(self.client.base_url), model)
            # Native JSON Schema first; endpoints that refuse it get JSON object mode, and those
            # that refuse that too (Claude 5 behind LiteLLM: both become a forced tool call) get
            # the schema in the prompt only. Refusals are remembered for this process.
            output_mode = ("prompt" if endpoint in _JSON_OBJECT_REJECTED else
                           "json_object_fallback" if endpoint in _JSON_SCHEMA_REJECTED else "json_schema")
            schema_system = (system + "\nReturn one JSON object that validates against this schema "
                             "exactly:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False))
            while True:
                if output_mode == "json_schema":
                    request = dict(model=model, messages=messages, response_format={
                        "type": "json_schema",
                        "json_schema": {"name": schema.__name__, "strict": True,
                                        "schema": schema.model_json_schema()}})
                else:
                    request = dict(model=model, messages=[{"role": "system", "content": schema_system}, messages[1]],
                                   **({"response_format": {"type": "json_object"}}
                                      if output_mode == "json_object_fallback" else {}))
                try:
                    response = await self._chat("extraction", {**request, **parameters})
                    break
                except BadRequestError as exc:
                    if exc.status_code != 400 or output_mode == "prompt":
                        raise
                    if output_mode == "json_schema":
                        _JSON_SCHEMA_REJECTED.add(endpoint)
                        output_mode = "json_object_fallback"
                    else:
                        _JSON_OBJECT_REJECTED.add(endpoint)
                        output_mode = "prompt"
            if not response.choices:
                raise ValueError("provider returned no choices for structured output")
            content = response.choices[0].message.content
            if not content:
                raise ValueError("provider returned no structured content")
            first_response = response.model_dump(mode="json")
            responses = [first_response]
            try:
                value = validate_lenient_json(schema, content)
            except ValidationError as exc:
                output_mode = "json_object_repair"
                repair_payload = {
                    "task": (
                        "Repair the candidate JSON so it validates. Change only what the validation "
                        "errors require and copy every other part of the candidate unchanged, including "
                        "every evidence array, block_id and quote. Never drop evidence from a stated fact. "
                        "Return only the repaired JSON object."
                    ),
                    "validation_errors": exc.errors(
                        include_url=False, include_input=False, include_context=False
                    ),
                    "candidate": content,
                }
                response = await self._chat("repair", dict(
                    model=model,
                    messages=[
                        {"role": "system", "content": (
                            system + "\nValidate every fact state/value/evidence invariant. "
                            "The output must match this schema exactly:\n"
                            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
                        )},
                        {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)},
                    ],
                    **({} if endpoint in _JSON_OBJECT_REJECTED else {"response_format": {"type": "json_object"}}),
                    **parameters,
                ))
                responses.append(response.model_dump(mode="json"))
                content = response.choices[0].message.content if response.choices else None
                try:
                    if not content:
                        raise ValueError("provider returned no repaired structured content")
                    value = validate_lenient_json(schema, content)
                except (ValidationError, ValueError) as repair_exc:
                    raise StructuredOutputError(
                        str(repair_exc)[:2000],
                        {"responses": responses,
                         "_policy_harvester": {"structured_output_mode": output_mode}},
                    ) from repair_exc
            input_tokens = sum((getattr(item.usage, "prompt_tokens", 0) or 0)
                               for item in [response])
            output_tokens = sum((getattr(item.usage, "completion_tokens", 0) or 0)
                                for item in [response])
            if len(responses) > 1:
                first_usage = first_response.get("usage") or {}
                input_tokens += first_usage.get("prompt_tokens") or 0
                output_tokens += first_usage.get("completion_tokens") or 0
            usage = Usage(input_tokens or None, output_tokens or None)
            raw_response = {"responses": responses}
            raw_response["_policy_harvester"] = {"structured_output_mode": output_mode}
            return GenerationResult(value, raw_response, usage, self.name, model)
        response = await _recorded("extraction", self.name, self.client.responses.parse, dict(
            model=model,
            input=[{"role": "system", "content": system},
                   {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            text_format=schema,
            store=False,
            **parameters,
        ))
        if response.status != "completed" or response.output_parsed is None:
            raise ValueError(f"incomplete structured response: {response.status}")
        usage = Usage(getattr(response.usage, "input_tokens", None),
                      getattr(response.usage, "output_tokens", None))
        return GenerationResult(response.output_parsed, response.model_dump(mode="json"),
                                usage, self.name, model)

    async def transcribe_image(self, *, model: str, image: bytes, mime: str, prompt: str,
                               parameters: dict[str, Any] | None = None) -> TranscriptionResult:
        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        response = await self._chat("transcription", dict(
            model=model, messages=[{
                "role": "user", "content": [{"type": "text", "text": prompt},
                                            {"type": "image_url", "image_url": {"url": url}}]}],
            **(parameters or {})))
        usage = Usage(getattr(response.usage, "prompt_tokens", None),
                      getattr(response.usage, "completion_tokens", None))
        # Some endpoints return no choices when the model finds nothing to write (logos, photos).
        content = response.choices[0].message.content if response.choices else None
        return TranscriptionResult(content or "", response.model_dump(mode="json"), usage, model)

    async def embed(self, *, model: str, texts: list[str], dimensions: int,
                    kind: str = "document") -> list[list[float]]:
        response = await self.client.embeddings.create(model=model, input=texts, dimensions=dimensions)
        return [row.embedding for row in response.data]

    async def test_connection(self, model: str) -> dict[str, Any]:
        if self.capability == "embedding":
            response = await self.client.embeddings.create(
                model=model, input=["connection test"],
                dimensions=self.settings.embedding_dimensions,
            )
            dimensions = len(response.data[0].embedding)
            return {"ok": True, "provider": self.name, "model": model,
                    "dimensions": dimensions}
        if self.compatible:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with OK."}],
                max_tokens=256,
            )
            if not response.choices:
                raise ValueError("provider returned no chat choices")
        else:
            response = await self.client.responses.create(
                model=model, input="Reply with OK.", max_output_tokens=16, store=False)
            if response.status != "completed":
                raise ValueError(f"provider connection response was {response.status}")
        return {"ok": True, "provider": self.name, "model": model}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise CapabilityError("install the anthropic extra") from exc
        if settings.llm_api_key is None:
            raise CapabilityError("Anthropic API key is not configured")
        self.client = AsyncAnthropic(api_key=settings.llm_api_key.get_secret_value(),
                                     base_url=settings.llm_base_url,
                                     timeout=settings.llm_timeout_seconds,
                                     max_retries=settings.llm_max_retries)

    async def structured(self, *, model: str, system: str, payload: dict[str, Any],
                         schema: type[SchemaT], parameters: dict[str, Any]) -> GenerationResult:
        tool = {"name": "submit_extraction", "description": "Submit validated extraction",
                "input_schema": schema.model_json_schema()}
        response = await _recorded("extraction", self.name, self.client.messages.create, dict(
            model=model, system=system, max_tokens=parameters.pop("max_tokens", 8192),
            tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            **parameters,
        ))
        tool_blocks = [block for block in response.content if getattr(block, "type", None) == "tool_use"]
        if len(tool_blocks) != 1:
            raise ValueError("Anthropic did not return the extraction tool exactly once")
        value = schema.model_validate(tool_blocks[0].input)
        usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
        return GenerationResult(value, response.model_dump(mode="json"), usage, self.name, model)

    async def transcribe_image(self, *, model: str, image: bytes, mime: str, prompt: str,
                               parameters: dict[str, Any] | None = None) -> TranscriptionResult:
        response = await _recorded("transcription", self.name, self.client.messages.create, dict(
            model=model, max_tokens=8192, messages=[{
                "role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime,
                                                 "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": prompt}]}], **(parameters or {})))
        text_value = "".join(getattr(block, "text", "") for block in response.content)
        usage = Usage(getattr(response.usage, "input_tokens", None),
                      getattr(response.usage, "output_tokens", None))
        return TranscriptionResult(text_value, response.model_dump(mode="json"), usage, model)

    async def test_connection(self, model: str) -> dict[str, Any]:
        return {"ok": True, "provider": self.name, "model": model,
                "note": "credentials are configured; first generation performs the remote check"}


class GoogleProvider:
    name = "google"

    def __init__(self, settings: Settings):
        try:
            from google import genai
        except ImportError as exc:
            raise CapabilityError("install the google extra") from exc
        if settings.llm_api_key is None:
            raise CapabilityError("Google API key is not configured")
        self.client = genai.Client(api_key=settings.llm_api_key.get_secret_value())

    async def structured(self, *, model: str, system: str, payload: dict[str, Any],
                         schema: type[SchemaT], parameters: dict[str, Any]) -> GenerationResult:
        from google.genai import types

        response = await _recorded("extraction", self.name, self.client.aio.models.generate_content, dict(
            model=model,
            contents=json.dumps(payload, ensure_ascii=False),
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                **parameters,
            ),
        ))
        value = schema.model_validate_json(response.text)
        metadata = response.usage_metadata
        usage = Usage(getattr(metadata, "prompt_token_count", None),
                      getattr(metadata, "candidates_token_count", None))
        return GenerationResult(value, response.model_dump(mode="json"), usage, self.name, model)

    async def transcribe_image(self, *, model: str, image: bytes, mime: str, prompt: str,
                               parameters: dict[str, Any] | None = None) -> TranscriptionResult:
        from google.genai import types

        response = await _recorded("transcription", self.name, self.client.aio.models.generate_content, dict(
            model=model, contents=[types.Part.from_bytes(data=image, mime_type=mime), prompt],
            config=types.GenerateContentConfig(**parameters) if parameters else None))
        metadata = response.usage_metadata
        usage = Usage(getattr(metadata, "prompt_token_count", None),
                      getattr(metadata, "candidates_token_count", None))
        return TranscriptionResult(response.text or "", response.model_dump(mode="json"), usage, model)

    async def test_connection(self, model: str) -> dict[str, Any]:
        await self.client.aio.models.get(model=model)
        return {"ok": True, "provider": self.name, "model": model}


class HttpEmbeddingProvider:
    def __init__(self, settings: Settings, name: str):
        if settings.embedding_api_key is None:
            raise CapabilityError(f"{name} embedding API key is not configured")
        self.settings = settings
        self.name = name
        self.api_key = settings.embedding_api_key.get_secret_value()
        self.client = httpx.AsyncClient(timeout=settings.llm_timeout_seconds)

    async def embed(self, *, model: str, texts: list[str], dimensions: int,
                    kind: str = "document") -> list[list[float]]:
        if self.name == "cohere":
            base = self.settings.embedding_base_url or "https://api.cohere.com"
            response = await self.client.post(
                f"{base.rstrip('/')}/v2/embed",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": model, "texts": texts, "input_type": "search_document",
                      "embedding_types": ["float"]},
            )
            response.raise_for_status()
            return response.json()["embeddings"]["float"]
        if self.name == "voyage":
            base = self.settings.embedding_base_url or "https://api.voyageai.com"
            response = await self.client.post(
                f"{base.rstrip('/')}/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": model, "input": texts, "input_type": "document"},
            )
            response.raise_for_status()
            return [item["embedding"] for item in response.json()["data"]]
        if self.name == "google":
            base = self.settings.embedding_base_url or "https://generativelanguage.googleapis.com/v1beta"
            model_path = model if model.startswith("models/") else f"models/{model}"
            response = await self.client.post(
                f"{base.rstrip('/')}/{quote(model_path, safe='/')}:batchEmbedContents",
                params={"key": self.api_key},
                json={"requests": [{"model": model_path, "content": {"parts": [{"text": value}]},
                                     "outputDimensionality": dimensions} for value in texts]},
            )
            response.raise_for_status()
            return [item["values"] for item in response.json()["embeddings"]]
        raise CapabilityError(f"unsupported HTTP embedding provider: {self.name}")

    async def test_connection(self, model: str) -> dict[str, Any]:
        if self.name == "google":
            base = self.settings.embedding_base_url or "https://generativelanguage.googleapis.com/v1beta"
            model_path = model if model.startswith("models/") else f"models/{model}"
            response = await self.client.get(
                f"{base.rstrip('/')}/{quote(model_path, safe='/')}", params={"key": self.api_key})
            response.raise_for_status()
        else:
            await self.embed(model=model, texts=["connection test"], dimensions=1536)
        return {"ok": True, "provider": self.name, "model": model}


class LocalEmbeddingProvider:
    name = "huggingface_local"

    def __init__(self, _: Settings):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise CapabilityError("install the local-embeddings extra") from exc
        self.model_type = SentenceTransformer
        self.models: dict[str, Any] = {}

    async def embed(self, *, model: str, texts: list[str], dimensions: int,
                    kind: str = "document") -> list[list[float]]:
        import asyncio

        loaded = self.models.setdefault(model, self.model_type(model))
        vectors = await asyncio.to_thread(loaded.encode, texts, normalize_embeddings=True)
        result = [value.tolist() for value in vectors]
        if any(len(value) != dimensions for value in result):
            raise ValueError(f"local model dimension does not match profile dimension {dimensions}")
        return result

    async def test_connection(self, model: str) -> dict[str, Any]:
        loaded = self.models.setdefault(model, self.model_type(model))
        return {"ok": True, "provider": self.name, "model": model,
                "dimensions": loaded.get_sentence_embedding_dimension()}


@dataclass(frozen=True)
class ExtractionTarget:
    """A model ready for structured extraction, with the request parameters it needs."""
    provider: LLMProvider
    provider_name: str
    model: str
    parameters: dict[str, Any]


ADAPTIVE_THINKING = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-5", re.IGNORECASE)


def structured_parameters(provider_name: str, settings: Settings,
                          reasoning_effort: str | None, model: str | None = None) -> dict[str, Any]:
    """Output cap and reasoning effort in each provider's own request vocabulary."""
    name = provider_name.lower()
    limit = settings.llm_max_output_tokens
    if name in {"openai", "azure_openai"}:  # Responses API
        parameters: dict[str, Any] = {"max_output_tokens": limit} if limit else {}
        if reasoning_effort:
            parameters["reasoning"] = {"effort": reasoning_effort}
        return parameters
    if name in {"openai_compatible", "local"}:
        parameters = {"max_tokens": limit} if limit else {}
        extra_body: dict[str, Any] = {}
        if reasoning_effort and model and ADAPTIVE_THINKING.search(model):
            # Claude 5 refuses fixed thinking budgets, which is what gateways turn reasoning_effort into.
            extra_body.update(thinking={"type": "adaptive"}, output_config={"effort": reasoning_effort})
        elif reasoning_effort:
            parameters["reasoning_effort"] = reasoning_effort
        if "openrouter.ai" in (settings.llm_base_url or ""):
            extra_body["usage"] = {"include": True}  # per-call cost in the response
        if extra_body:
            parameters["extra_body"] = extra_body
        return parameters
    if name == "anthropic":
        return {"max_tokens": limit} if limit else {}
    if name in {"google", "gemini"}:
        return {"max_output_tokens": limit} if limit else {}
    return {}


class ProviderRegistry:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _target(self, provider: str | None, base_url: str | None, api_key: Any, model: str,
                reasoning_effort: str | None, output_limit: int | None = None) -> ExtractionTarget:
        """A model on its own endpoint; each unset endpoint field falls back to the llm_* one."""
        base = self.settings
        name = provider or base.llm_provider
        updates: dict[str, Any] = {"llm_provider": name}
        if base_url:
            updates["llm_base_url"] = base_url
        if api_key is not None and api_key.get_secret_value():
            updates["llm_api_key"] = api_key
        if output_limit is not None:
            updates["llm_max_output_tokens"] = output_limit
        settings = base.model_copy(update=updates)
        return ExtractionTarget(ProviderRegistry(settings).llm(name), name, model,
                                structured_parameters(name, settings, reasoning_effort, model))

    def extraction(self) -> ExtractionTarget:
        """The extraction model: extraction_* settings where set, otherwise the llm_* ones."""
        base = self.settings
        return self._target(base.extraction_llm_provider, base.extraction_llm_base_url,
                            base.extraction_llm_api_key, base.extraction_llm_model or base.llm_model,
                            base.extraction_reasoning_effort)

    def crosscheck(self) -> ExtractionTarget | None:
        """The second-opinion model (crosscheck_* endpoint, else llm_*), if one is configured."""
        base = self.settings
        if not base.extraction_crosscheck_model:
            return None
        return self._target(base.crosscheck_llm_provider, base.crosscheck_llm_base_url,
                            base.crosscheck_llm_api_key, base.extraction_crosscheck_model, None)

    def reviewer(self) -> ExtractionTarget | None:
        """The AI second-review model (review_* endpoint, else llm_*), if one is configured."""
        base = self.settings
        if not base.review_llm_model:
            return None
        # A verdict is short; the cap leaves room for thinking without inviting runaway output.
        return self._target(base.review_llm_provider, base.review_llm_base_url, base.review_llm_api_key,
                            base.review_llm_model, base.review_reasoning_effort, REVIEW_MAX_OUTPUT_TOKENS)

    def llm(self, provider: str | None = None) -> LLMProvider:
        name = (provider or self.settings.llm_provider).lower()
        if name in {"openai", "azure_openai"}:
            return OpenAIProvider(self.settings)
        if name in {"openai_compatible", "local"}:
            return OpenAIProvider(self.settings, compatible=True)
        if name == "anthropic":
            return AnthropicProvider(self.settings)
        if name in {"google", "gemini"}:
            return GoogleProvider(self.settings)
        raise CapabilityError(f"unsupported LLM provider: {name}")

    def embedding(self, provider: str | None = None) -> EmbeddingProvider:
        name = (provider or self.settings.embedding_provider).lower()
        if name in {"openai", "azure_openai"}:
            return OpenAIProvider(self.settings, capability="embedding")
        if name in {"openai_compatible", "local"}:
            return OpenAIProvider(self.settings, compatible=True, capability="embedding")
        if name in {"cohere", "voyage", "google"}:
            return HttpEmbeddingProvider(self.settings, name)
        if name in {"huggingface", "huggingface_local", "sentence_transformers"}:
            return LocalEmbeddingProvider(self.settings)
        if name == "onnx":  # internal: ONNX model on this machine's CPU, no external call
            from .onnx_embeddings import OnnxEmbeddingProvider

            return OnnxEmbeddingProvider(self.settings)
        raise CapabilityError(f"embedding capability is unavailable for provider: {name}")
