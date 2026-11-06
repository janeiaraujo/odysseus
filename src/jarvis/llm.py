"""Model calls for Jarvis background work, chosen by the router, with cost-gated fallback."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from src.jarvis import router
from src.jarvis.redact import redact_text


class NoModelAvailable(RuntimeError):
    pass


async def complete(owner: Optional[str], messages: List[Dict[str, Any]], *, profile: Optional[str] = None, requires: Optional[List[str]] = None, purpose: str = "jarvis") -> str:
    from src.endpoint_resolver import resolve_endpoint_by_id
    from src.llm_core import llm_call_async

    ranked = router.rank(owner=owner or "", profile=profile, requires=requires)
    if not ranked:
        raise NoModelAvailable("nenhum modelo disponivel dentro das regras de custo")
    last_error: Optional[Exception] = None
    for choice in ranked[:4]:
        target = resolve_endpoint_by_id(choice.endpoint_id, choice.model, owner=owner or None)
        if not target:
            continue
        url, model, headers = target
        started = time.monotonic()
        try:
            answer = await llm_call_async(url, model, messages, headers=headers)
        except Exception as exc:
            last_error = exc
            router.record_usage(owner=owner or "", url=url, model=model, ok=False, error=exc, latency_ms=int((time.monotonic() - started) * 1000), purpose=purpose)
            continue
        router.record_usage(owner=owner or "", url=url, model=model, latency_ms=int((time.monotonic() - started) * 1000), purpose=purpose,
                            input_tokens=sum(len(str(m.get("content", ""))) for m in messages) // 4, output_tokens=len(answer or "") // 4)
        return answer
    raise NoModelAvailable(redact_text(last_error) if last_error else "nenhum modelo respondeu")
