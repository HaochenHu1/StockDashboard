"""Step 3-4: one Claude call with the web search server tool, JSON-only answer, one retry on bad JSON.

* Server side only. The key comes from the environment (ANTHROPIC_API_KEY, e.g. loaded from .env at
  startup); it never reaches the browser.
* Web search results carry citations, and citations cannot be combined with structured outputs
  (output_config.format), so the model is told to answer with JSON only and the server parses and
  checks it. If parsing fails, the same conversation gets one follow-up asking for the JSON again.
* Streaming (long output, many searches); pause_turn is resumed by re-sending the assistant content.
* Refusals: the server-side fallback (beta server-side-fallback-2026-07-01) is on by default.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from ..errors import UserError
from .rules import RETRY_MESSAGE, SYSTEM_PROMPT
from .validate import extract_json, structure_problems

log = logging.getLogger(__name__)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnalysisError(UserError):
    """The analysis could not be produced; the Chinese message is shown in the UI."""


def key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _urls(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v.startswith("http"):
                out.add(v)
            else:
                _urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _urls(v, out)


class AnalysisLLM:
    def __init__(self, cfg_get: Callable[[str, Any], Any]):
        self._cfg = cfg_get
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: the dashboard runs without the package when this tab is unused
            self._client = anthropic.AsyncAnthropic(max_retries=2, timeout=900.0)
        return self._client

    def _kwargs(self, messages: list[dict]) -> dict:
        kw: dict[str, Any] = {
            "model": self._cfg("model", "claude-opus-5-5"),
            "max_tokens": int(self._cfg("max_tokens", 64000)),
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": [{"type": "web_search_20260209", "name": "web_search",
                       "max_uses": int(self._cfg("web_search_max_uses", 20))}],
            "output_config": {"effort": self._cfg("effort", "high")},
            "cache_control": {"type": "ephemeral"},
        }
        fb = self._cfg("fallbacks", "default")
        if fb:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = fb
        return kw

    async def _stream(self, kwargs: dict, on_event: Callable[[str], None]):
        async with self._get_client().beta.messages.stream(**kwargs) as stream:
            async for ev in stream:
                if getattr(ev, "type", "") == "content_block_start":
                    t = getattr(ev.content_block, "type", "")
                    if t == "server_tool_use":
                        on_event("search")
                    elif t == "text":
                        on_event("text")
            return await stream.get_final_message()

    async def _turn(self, messages: list[dict], on_event: Callable[[str], None], acc: dict) -> list:
        """One assistant turn, resuming pause_turn. Returns the assistant content blocks of this turn."""
        import anthropic
        blocks: list = []
        for _ in range(int(self._cfg("max_continuations", 5)) + 1):
            msgs = messages + ([{"role": "assistant", "content": blocks}] if blocks else [])
            try:
                msg = await self._stream(self._kwargs(msgs), on_event)
            except anthropic.AuthenticationError:
                raise AnalysisError("Anthropic API 密钥无效（AuthenticationError），请检查 .env 中的 ANTHROPIC_API_KEY") from None
            except anthropic.PermissionDeniedError as e:
                raise AnalysisError(f"API 密钥没有权限：{e.message}") from None
            except anthropic.RateLimitError:
                raise AnalysisError("触发 Anthropic API 速率限制，请稍后重新生成") from None
            except anthropic.BadRequestError as e:
                raise AnalysisError(f"请求被 Anthropic API 拒绝：{e.message}") from None
            except anthropic.APIStatusError as e:
                raise AnalysisError(f"Anthropic API 错误 {e.status_code}，请稍后重新生成") from None
            except anthropic.APIConnectionError:
                raise AnalysisError("无法连接 Anthropic API，请检查网络") from None
            acc["model"] = getattr(msg, "model", acc.get("model"))
            u = getattr(msg, "usage", None)
            if u is not None:
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    acc["usage"][k] = acc["usage"].get(k, 0) + int(getattr(u, k, 0) or 0)
                stu = getattr(u, "server_tool_use", None)
                if stu is not None:
                    acc["usage"]["web_search_requests"] = (acc["usage"].get("web_search_requests", 0)
                                                           + int(getattr(stu, "web_search_requests", 0) or 0))
            blocks = blocks + list(msg.content)
            if msg.stop_reason == "pause_turn":
                continue
            if msg.stop_reason == "refusal":
                raise AnalysisError("模型拒绝生成这份分析（refusal），可以稍后重新生成")
            acc["stop_reason"] = msg.stop_reason
            return blocks
        raise AnalysisError("网络搜索轮次过多，分析未完成，请重新生成")

    async def run(self, payload: dict, on_event: Callable[[str], None]) -> dict:
        if not key_present():
            raise AnalysisError("未配置 Anthropic API 密钥：请在项目目录的 .env 中填写 ANTHROPIC_API_KEY，然后重启看板")
        acc: dict[str, Any] = {"usage": {}, "model": self._cfg("model", "claude-opus-5-5")}
        user = {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}
        blocks = await self._turn([user], on_event, acc)
        data, problems = self._parse(blocks)
        attempts = 1
        if problems:
            log.warning("stock analysis: model output not usable (%s); retrying once", "; ".join(problems))
            attempts = 2
            retry = {"role": "user", "content": RETRY_MESSAGE.format(problems="；".join(problems))}
            blocks2 = await self._turn([user, {"role": "assistant", "content": blocks}, retry], on_event, acc)
            blocks = blocks + blocks2
            data, problems = self._parse(blocks2)
            if problems:
                raise AnalysisError("模型两次返回的内容都无法解析为有效 JSON（" + "；".join(problems) + "），请重新生成")
        urls: set[str] = set()
        searches = 0
        for b in blocks:
            d = b.model_dump() if hasattr(b, "model_dump") else b
            if isinstance(d, dict) and d.get("type") == "server_tool_use":
                searches += 1
            _urls(d, urls)
        return {"data": data, "searched_urls": urls, "searches": searches, "usage": acc["usage"],
                "model": acc["model"], "attempts": attempts, "stop_reason": acc.get("stop_reason")}

    @staticmethod
    def _parse(blocks: list) -> tuple[dict | None, list[str]]:
        """Cited answers arrive as several text blocks that may split one JSON string, so the text blocks
        after the last search are joined with no separator; all text blocks are the fallback."""
        types = [getattr(b, "type", "") for b in blocks]
        if "text" not in types:
            return None, ["没有文字输出"]
        last_tool = max((i for i, t in enumerate(types) if t not in ("text", "thinking", "redacted_thinking", "fallback")),
                        default=-1)
        tail = "".join(getattr(b, "text", "") for b in blocks[last_tool + 1:] if getattr(b, "type", "") == "text")
        data = extract_json(tail)
        if data is None or structure_problems(data):
            whole = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")
            data = extract_json(whole) or data
        return data, structure_problems(data)
