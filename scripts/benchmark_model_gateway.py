"""Experiment-only mini gateway with a prospective, durable HTTP request ledger.

No model fallback, hidden retries, billing estimates, or access to the scorer.
OpenAI requests pass through; Claude's Messages wire format uses the pinned
LiteLLM translation code. Anthropic streaming is buffered and disclosed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import uuid
from fastapi import Request

MODEL = "gpt-5.4-mini"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    with path.open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def prepare_request(path, body, arm):
    """Return the actual provider request, preserving the requested body separately."""
    body = dict(body)
    if body.get("model") not in {MODEL, "openai/" + MODEL}:
        raise ValueError("This experiment permits only " + MODEL)
    body["model"] = MODEL
    if path == "/v1/messages":
        from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
            LiteLLMAnthropicToResponsesAPIAdapter,
        )
        body = LiteLLMAnthropicToResponsesAPIAdapter().translate_request(body)
        # Explicit protocol setting; never inherit a model-specific default.
        body["reasoning"] = {"effort": "none"}
        body.pop("temperature", None)
        body.pop("top_p", None)
        body.pop("litellm_metadata", None)
        body["stream"] = False
        body["store"] = False
        return "/responses", body
    if path == "/v1/responses":
        if arm != "sag":
            body["reasoning"] = {**body.get("reasoning", {}), "effort": "none"}
        body["store"] = False
        endpoint = "/responses"
    elif path == "/v1/chat/completions":
        if arm != "sag":
            body["reasoning_effort"] = "none"
        if body.get("stream"):
            body["stream_options"] = {**body.get("stream_options", {}), "include_usage": True}
        endpoint = "/chat/completions"
    else:
        raise ValueError("Unsupported inference endpoint")
    effort = body.get("reasoning_effort", body.get("reasoning", {}).get("effort"))
    if effort not in {None, "none", "medium", "high"}:
        raise ValueError("Reasoning setting differs from the frozen mini protocol")
    return endpoint, body


def usage_from_response(raw, *, streaming=False):
    candidates = []
    if streaming:
        for line in raw.decode("utf-8").splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                obj = json.loads(line[6:])
            except ValueError:
                continue
            if obj.get("type") == "response.completed":
                candidates.append(obj.get("response", {}))
            elif obj.get("usage"):
                candidates.append(obj)
    else:
        try:
            candidates = [json.loads(raw)]
        except ValueError:
            return None
    for obj in reversed(candidates):
        usage = obj.get("usage") or {}
        a, b = usage.get("input_tokens", usage.get("prompt_tokens")), usage.get("output_tokens", usage.get("completion_tokens"))
        if type(a) is not int or type(b) is not int or min(a, b) < 0:
            continue
        total = usage.get("total_tokens")
        if total is not None and total != a + b:
            continue
        return {"input_tokens": a, "output_tokens": b, "total_tokens": a + b,
                "response_id": obj.get("id"), "resolved_model": obj.get("model"),
                "provider_usage": usage}
    return None


def create_app(*, directory, run_id, arm, token, api_key, api_base):
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "start.json", {"run_id": run_id, "arm": arm, "model": MODEL,
         "started_at": now(), "gateway_version": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         "api_base": api_base, "provider_retries": 0,
         "anthropic_transport": "LiteLLM 1.83.7 Messages-to-Responses translation; buffered SSE",
         "reasoning_policy": "baseline actor none; native SAG body preserved (tool actor omits reasoning, advisor explicitly high)",
         "token_accounting": "provider input plus output, cached input included once; no estimates"})
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    active = set()

    @app.get("/health")
    async def health():
        return {"ready": True, "run_id": run_id}

    @app.post("/{path:path}")
    async def inference(path: str, request: Request):
        supplied = request.headers.get("x-api-key", "") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, token):
            return JSONResponse({"error": "Experiment token required"}, status_code=401)
        path = "/" + path
        try:
            original = await request.json()
            if path == "/v1/messages/count_tokens":
                if original.get("model") != MODEL:
                    raise ValueError("Unapproved model")
                import litellm
                estimate = litellm.token_counter(model=MODEL, text=json.dumps(original))
                save(directory / ("token-estimate-" + uuid.uuid4().hex + ".json"),
                     {"at": now(), "input_tokens": estimate, "method": "local tokenizer estimate; not a billed inference"})
                return {"input_tokens": estimate}
            endpoint, body = prepare_request(path, original, arm)
        except (ValueError, TypeError, KeyError) as exc:
            save(directory / ("rejected-" + uuid.uuid4().hex + ".json"), {"at": now(), "path": path, "reason": str(exc)})
            return JSONResponse({"error": str(exc)}, status_code=400)
        ident = uuid.uuid4().hex
        folder = directory / ident
        folder.mkdir()
        save(folder / "request.json", original)
        save(folder / "provider-request.json", body)
        effort = body.get('reasoning_effort', body.get('reasoning', {}).get('effort'))
        role = 'advisor' if arm == 'sag' and effort == 'high' else 'actor'
        save(folder / "started.json", {"id": ident, "run_id": run_id, "at": now(), "endpoint": endpoint,
                                      "role": role, "role_basis": "SAG explicit high identifies advisor under frozen configuration; other requests actor"})
        active.add(ident)
        client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30), follow_redirects=False)
        upstream = None
        try:
            req = client.build_request("POST", api_base.rstrip("/") + endpoint,
                                       headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, json=body)
            upstream = await client.send(req, stream=True)
            if body.get("stream") and upstream.status_code == 200:
                async def forward():
                    complete = False
                    try:
                        with (folder / "response.sse").open("xb") as stream:
                            async for data in upstream.aiter_bytes():
                                stream.write(data)
                                yield data
                            stream.flush()
                            os.fsync(stream.fileno())
                        complete = True
                    finally:
                        await upstream.aclose()
                        await client.aclose()
                        raw = (folder / "response.sse").read_bytes()
                        save(folder / "finished.json", {"at": now(), "status": "completed" if complete else "interrupted",
                             "http_status": 200, "usage": usage_from_response(raw, streaming=True) if complete else None})
                        active.discard(ident)
                return StreamingResponse(forward(), media_type="text/event-stream")
            raw = await upstream.aread()
            (folder / "response.json").write_bytes(raw)
            usage = usage_from_response(raw) if upstream.status_code == 200 else None
            save(folder / "finished.json", {"at": now(), "status": "completed" if upstream.status_code == 200 else "provider_error",
                 "http_status": upstream.status_code, "usage": usage})
            active.discard(ident)
            if path != "/v1/messages" or upstream.status_code != 200:
                return Response(raw, status_code=upstream.status_code, media_type="application/json")
            from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import LiteLLMAnthropicToResponsesAPIAdapter
            from litellm.types.llms.openai import ResponsesAPIResponse
            from litellm.llms.anthropic.experimental_pass_through.messages.fake_stream_iterator import FakeAnthropicMessagesStreamIterator
            response = LiteLLMAnthropicToResponsesAPIAdapter().translate_response(ResponsesAPIResponse(**json.loads(raw)))
            if original.get("stream"):
                return StreamingResponse(FakeAnthropicMessagesStreamIterator(response), media_type="text/event-stream")
            return JSONResponse(response)
        except Exception as exc:
            if not (folder / "finished.json").exists():
                save(folder / "finished.json", {"at": now(), "status": "transport_error", "error_type": type(exc).__name__, "usage": None})
            active.discard(ident)
            return JSONResponse({"error": "Gateway " + type(exc).__name__}, status_code=502)
        finally:
            if ident not in active:
                if upstream is not None:
                    await upstream.aclose()
                await client.aclose()

    @app.on_event("shutdown")
    async def shutdown():
        save(directory / "close.json", {"run_id": run_id, "closed_at": now(), "unfinished_requests": sorted(active)})

    return app


def main():
    import uvicorn
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--arm", choices=("sag", "claude", "opencode"), required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    app = create_app(directory=args.directory, run_id=args.run_id, arm=args.arm,
                     token=os.environ["BENCH_GATEWAY_TOKEN"], api_key=os.environ["OPENAI_API_KEY"],
                     api_base=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", timeout_graceful_shutdown=30)


if __name__ == "__main__":
    main()
