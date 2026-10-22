"""多模型统一调用模块。

能力：
1. 一套 OpenAI SDK 兼容所有 OpenAI 格式厂商（每个模型独立 base_url + api_key）
2. 多模型并行异步调用（asyncio.gather），大幅降低多结果对比的等待时间
3. 失败自动降级：单个模型超时/报错不影响其它模型；fallback 模式按顺序换到下一个
4. 统一返回 ModelResult（模型名 / 内容 / token 用量 / 错误信息）
5. 单模型流式输出（SSE 原样透传上游 chunk）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

from src.config_loader import AppConfig, ModelConfig

# 模型级 proxy 字段中代表"强制直连"的关键字
_DIRECT_KEYWORDS = {"direct", "off", "none"}


@dataclass
class ModelResult:
    """统一返回格式。"""

    model: str
    content: str = ""
    tokens: int = 0          # 总 token 用量（prompt + completion），失败为 0
    ok: bool = True
    error: str = ""
    raw: dict = field(default_factory=dict)  # 上游原始响应（调试用）


class MultiModelClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.timeout = config.call.timeout_seconds
        self.default_models = config.call.default_models
        self.fallback_order = config.call.fallback_order
        self.token_warning = config.limits.token_warning_threshold
        # 每个模型配置复用一个 AsyncOpenAI 客户端
        self._clients: dict[str, AsyncOpenAI] = {}
        # 为指定了独立代理的模型创建的 httpx 客户端（退出时需显式关闭）
        self._http_clients: list[httpx.AsyncClient] = []

    # ---------- 内部工具 ----------
    @staticmethod
    def _timeout_for(m: ModelConfig, default: float) -> float:
        """模型级超时优先；未配置时回退全局 call.timeout_seconds。"""
        return m.timeout_seconds if m.timeout_seconds is not None else default

    def _build_http_client(self, m: ModelConfig) -> httpx.AsyncClient | None:
        """模型级代理：返回定制 httpx 客户端；未指定代理则返回 None（跟随全局）。"""
        p = (m.proxy or "").strip()
        timeout = self._timeout_for(m, self.timeout)
        if not p:
            return None
        if p.lower() in _DIRECT_KEYWORDS:
            # 强制直连：绕过全局环境变量代理
            client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(timeout, connect=10.0),
            )
        else:
            # 仅该模型走指定代理
            client = httpx.AsyncClient(
                proxy=p,
                timeout=httpx.Timeout(timeout, connect=10.0),
            )
        self._http_clients.append(client)
        return client

    def _sdk_client(self, m: ModelConfig) -> AsyncOpenAI:
        if m.name not in self._clients:
            http_client = self._build_http_client(m)
            kwargs = {"http_client": http_client} if http_client else {}
            self._clients[m.name] = AsyncOpenAI(
                base_url=m.base_url,
                api_key=m.api_key,
                timeout=self._timeout_for(m, self.timeout),
                **kwargs,
            )
        return self._clients[m.name]

    def _resolve(self, models: list[str] | None) -> list[ModelConfig]:
        """把模型名/组名列表解析为启用的 ModelConfig；None 时用默认模型。

        名字命中 call.model_groups 时展开为该组模型（只展开一层）。
        未知或已禁用的模型名跳过（降级语义），全部无效时返回空列表。
        """
        names = models if models else self.default_models
        groups = self.config.call.model_groups
        expanded: list[str] = []
        for n in names:
            expanded.extend(groups.get(n, [n]))
        by_name = {m.name: m for m in self.config.models}
        resolved: list[ModelConfig] = []
        for n in expanded:
            m = by_name.get(n)
            if m is None:
                print(f"  [警告] 模型 '{n}' 未在 config.yaml 中配置，已跳过")
            elif not m.enabled:
                print(f"  [警告] 模型 '{n}' 已禁用，已跳过")
            else:
                resolved.append(m)
        return resolved

    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """粗估 token（1 个中文字约 1.5 token，英文单词约 1 token）。"""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return int(total_chars * 1.5)

    async def aclose(self) -> None:
        await asyncio.gather(
            *(c.close() for c in self._clients.values()), return_exceptions=True
        )
        # 关闭模型级代理创建的 httpx 客户端（重复关闭是安全的空操作）
        await asyncio.gather(
            *(h.aclose() for h in self._http_clients), return_exceptions=True
        )

    # ---------- 单模型调用 ----------
    async def _call_one(
        self, m: ModelConfig, messages: list[dict]
    ) -> ModelResult:
        try:
            resp = await self._sdk_client(m).chat.completions.create(
                model=m.model_name, messages=messages
            )
            usage = getattr(resp, "usage", None)
            return ModelResult(
                model=m.name,
                content=resp.choices[0].message.content or "",
                tokens=getattr(usage, "total_tokens", 0) if usage else 0,
                ok=True,
            )
        except Exception as exc:  # 单模型失败转为错误结果，由调用方决定降级
            return ModelResult(model=m.name, ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _call_one_timeout(
        self, m: ModelConfig, messages: list[dict]
    ) -> ModelResult:
        timeout = self._timeout_for(m, self.timeout)
        try:
            return await asyncio.wait_for(
                self._call_one(m, messages), timeout=timeout
            )
        except asyncio.TimeoutError:
            return ModelResult(
                model=m.name, ok=False, error=f"请求超时（>{timeout:.0f}s）"
            )

    # ---------- 对外主入口 ----------
    async def chat(
        self,
        messages: list[dict],
        models: list[str] | None = None,
        stream: bool = False,
        messages_by_model: dict[str, list[dict]] | None = None,
    ) -> list[ModelResult] | AsyncIterator[str]:
        """统一入口。

        - stream=False：并行调用所有指定（或默认）模型，返回 ModelResult 列表
        - stream=True：仅支持单模型，返回 SSE chunk 的异步迭代器
        - messages_by_model：可选，按模型名分发差异化上下文（如历史中只保留
          该模型自己的回复全文）；未提供的模型回退到统一 messages
        """
        if stream:
            targets = self._resolve(models)
            if not targets:
                raise ValueError("没有可用于流式调用的启用模型")
            return self.stream(messages, targets[0].name)

        targets = self._resolve(models)
        if not targets:
            raise ValueError("没有启用的模型可调用，请检查 config.yaml")

        # token 预警按各模型实际上下文的最大值估算
        est = max(
            self.estimate_tokens((messages_by_model or {}).get(m.name, messages))
            for m in targets
        )
        if est > self.token_warning:
            print(f"  [token 预警] 本次输入约 {est} tokens，超过阈值 {self.token_warning}")

        # 并行请求，保持与配置一致的返回顺序
        results = await asyncio.gather(
            *(
                self._call_one_timeout(
                    m, (messages_by_model or {}).get(m.name, messages)
                )
                for m in targets
            )
        )
        return list(results)

    # ---------- 失败降级 ----------
    async def chat_with_fallback(
        self,
        messages: list[dict],
        models: list[str] | None = None,
    ) -> ModelResult:
        """按 fallback_order 顺序尝试，返回第一个成功的结果；全失败时返回最后一个错误。"""
        names = models if models else self.fallback_order
        targets = self._resolve(names)
        last_error = ModelResult(model=names[0] if names else "?", ok=False, error="无可用模型")
        for m in targets:
            result = await self._call_one_timeout(m, messages)
            if result.ok:
                return result
            print(f"  [降级] {m.name} 失败：{result.error[:80]}，尝试下一个模型...")
            last_error = result
        return last_error

    async def chat_text(
        self, messages: list[dict], model_name: str | None = None
    ) -> str:
        """供摘要/画像等内部场景使用：返回纯文本，单模型失败自动降级。"""
        names = [model_name] + self.fallback_order if model_name else self.fallback_order
        # 去重保持顺序
        seen: set[str] = set()
        ordered = [n for n in names if not (n in seen or seen.add(n))]
        result = await self.chat_with_fallback(messages, ordered)
        if not result.ok:
            raise RuntimeError(f"所有模型均调用失败：{result.error}")
        return result.content

    # ---------- 流式 ----------
    async def stream(
        self, messages: list[dict], model_name: str | None = None
    ) -> AsyncIterator[str]:
        """单模型流式调用，逐块 yield OpenAI 格式的 SSE chunk JSON。"""
        targets = self._resolve([model_name] if model_name else None)
        if not targets:
            raise ValueError("没有可用于流式调用的启用模型")
        m = targets[0]
        client = self._sdk_client(m)
        stream_obj = await client.chat.completions.create(
            model=m.model_name, messages=messages, stream=True
        )
        try:
            async for chunk in stream_obj:
                yield chunk.model_dump_json()
        finally:
            await stream_obj.close()
