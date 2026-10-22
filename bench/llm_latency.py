#!/usr/bin/env python3
"""
测 LLM 的 TTFT（首 token 延迟）与 TPOT（每 token 输出延迟）。

用法：
    python3 llm_latency.py --model MiniMax-M3.1-Flash
    python3 llm_latency.py --model MiniMax-M3 --repeat 3 --prompt "写一段 500 字的技术说明"
    MINIMAX_API_KEY=sk-xxx python3 llm_latency.py

环境变量：
    MINIMAX_API_KEY  或  OPENAI_API_KEY（取不到就回退读 --api-key）
    LLM_BASE_URL     默认 https://api.minimaxi.com/v1

指标定义：
    TTFT = 首个内容 token 到达时刻 - 请求发出时刻
    TPOT = (最后一个 token 到达时刻 - 首个 token 到达时刻) / (输出 token 数 - 1)
    吞吐 ≈ 1000 / TPOT  tok/s
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

from openai import AsyncOpenAI
import tiktoken


def get_client(api_key: str, base_url: str) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=120.0,
        max_retries=0,          # 重试会污染延迟测量
    )


def count_tokens(text: str, model: str) -> int:
    """优先用 API 返回的 usage，退化到 tiktoken 估算。"""
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # 粗略估算：中文按 1.5 字/token，英文按 4 字符/token
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other = len(text) - cjk
        return int(cjk / 1.5 + other / 4) or 1


async def measure_once(
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> dict:
    """跑一次流式请求，返回 TTFT / TPOT / 吞吐。"""
    start = time.perf_counter()

    first_ts = None          # 首个 token 到达时刻（含 reasoning）
    last_ts = None           # 最后一个 token 到达时刻
    chunk_times = []         # 每个 token chunk 的到达时刻
    pieces = []              # 正文文本
    reasoning_pieces = []    # 思考过程文本
    first_content_ts = None  # 首个「正文」token 到达时刻（思考阶段之后）
    api_usage = None

    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=max_tokens,
        temperature=temperature,
    )

    async for chunk in stream:
        now = time.perf_counter()
        if chunk.usage:
            api_usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        content = getattr(delta, "content", None)
        reasoning = getattr(delta, "reasoning_content", None)
        if not content and not reasoning:
            continue
        if first_ts is None:
            first_ts = now
        last_ts = now
        chunk_times.append(now)
        if content:
            if first_content_ts is None:
                first_content_ts = now
            pieces.append(content)
        if reasoning:
            reasoning_pieces.append(reasoning)

    end = time.perf_counter()
    text = "".join(pieces)

    if first_ts is None:
        return {"error": "没有收到任何内容 token", "model": model}

    # 输出 token 数：优先用 API 的 usage.completion_tokens（已含 reasoning token）
    if api_usage and api_usage.completion_tokens:
        output_token_cnt = api_usage.completion_tokens
        token_source = "api_usage"
    else:
        output_token_cnt = count_tokens(text + "".join(reasoning_pieces), model)
        token_source = "tiktoken_estimate"

    ttft = first_ts - start

    if output_token_cnt > 1 and len(chunk_times) > 1:
        # 用 token 数做分母，用首尾时间差做分子
        tpot = (last_ts - first_ts) / (output_token_cnt - 1)
        tpot_chunk_based = (last_ts - first_ts) / (len(chunk_times) - 1)
    else:
        tpot = None
        tpot_chunk_based = None

    total_time = end - start

    return {
        "model": model,
        "ttft_ms": round(ttft * 1000, 1),
        "ttft_to_content_ms": round((first_content_ts - start) * 1000, 1) if first_content_ts else None,
        "tpot_ms": round(tpot * 1000, 2) if tpot else None,
        "tpot_by_chunk_ms": round(tpot_chunk_based * 1000, 2) if tpot_chunk_based else None,
        "throughput_tok_s": round(1 / tpot, 1) if tpot else None,
        "output_tokens": output_token_cnt,
        "token_source": token_source,
        "chunks": len(chunk_times),
        "total_time_ms": round(total_time * 1000, 1),
        "prompt_tokens": api_usage.prompt_tokens if api_usage else None,
        "text_chars": len(text),
    }


async def main():
    p = argparse.ArgumentParser(description="测 LLM 的 TTFT 与 TPOT")
    p.add_argument("--model", default="MiniMax-M3.1-Flash")
    p.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"))
    p.add_argument("--api-key", default=None)
    p.add_argument("--prompt", default="用中文写一段 400 字左右的说明，介绍大语言模型的基本工作原理。")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--repeat", type=int, default=3, help="跑几次取中位数")
    args = p.parse_args()

    api_key = (
        args.api_key
        or os.getenv("MINIMAX_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )
    if not api_key:
        print("缺少 API Key：设置 MINIMAX_API_KEY 或传 --api-key", file=sys.stderr)
        sys.exit(1)

    client = get_client(api_key, args.base_url)
    results = []

    for i in range(args.repeat):
        try:
            r = await measure_once(
                client, args.model, args.prompt, args.max_tokens, args.temperature
            )
            results.append(r)
            if "error" in r:
                print(f"[{i + 1}/{args.repeat}] ERROR: {r['error']}")
            else:
                print(
                    f"[{i + 1}/{args.repeat}] TTFT={r['ttft_ms']}ms  "
                    f"TPOT={r['tpot_ms']}ms  吞吐={r['throughput_tok_s']}tok/s  "
                    f"输出token={r['output_tokens']}({r['token_source']})  "
                    f"chunks={r['chunks']}  总耗时={r['total_time_ms']}ms"
                )
        except Exception as e:
            print(f"[{i + 1}/{args.repeat}] 请求失败: {type(e).__name__}: {e}", file=sys.stderr)
        if i < args.repeat - 1:
            await asyncio.sleep(1)

    ok = [r for r in results if "error" not in r]
    if not ok:
        print("\n没有成功的请求")
        sys.exit(1)

    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "repeat": args.repeat,
        "ttft_ms_median": statistics.median(r["ttft_ms"] for r in ok),
        "ttft_ms_min": min(r["ttft_ms"] for r in ok),
        "ttft_ms_max": max(r["ttft_ms"] for r in ok),
        "tpot_ms_median": statistics.median(r["tpot_ms"] for r in ok if r["tpot_ms"]),
        "throughput_tok_s_median": statistics.median(
            r["throughput_tok_s"] for r in ok if r["throughput_tok_s"]
        ),
        "runs": ok,
    }
    print("\n=== 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
