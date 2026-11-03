"""LLM 延迟 A/B 压测：火山方舟 vs MiniMax，用后端真实业务提示词。

测的是后端最慢的那一步——「方案设计」阶段：8.5KB 的 report_plan.md 系统提示词
+ 一轮用户需求，输出结构化 JSON。指标：

    TTFT      首字延迟（用户从发出请求到看到第一个字）
    总耗时    整段完成（后端目前是非流式，用户实际等的就是它）
    JSON 合规 输出能否解析成合法 plan_candidate（结构化修复重试的触发条件）

用法：
    cd backend
    # 1) MiniMax 走开放平台（OpenAI 兼容，推荐）：
    MINIMAX_API_KEY=sk-api-xxx python3 scripts/bench_llm_latency.py --rounds 3
    # 2) 换模型 / 换端点：
    MINIMAX_MODEL=MiniMax-M3.1-Flash MINIMAX_BASE_URL=https://api.minimax.cn/v1 \
        python3 scripts/bench_llm_latency.py --rounds 3
    # 3) 只跑一家：
    python3 scripts/bench_llm_latency.py --rounds 3 --only volc

依赖：只用标准库。凭据从 backend/.env 与环境变量读取，不打印。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_PROMPT_PATH = _BACKEND_DIR / "app" / "agents" / "prompts" / "report_plan.md"

# 真实用户请求（覆盖指标卡 / 对比图 / 表格三种板块 + 排除项 + 粒度）
_USER_MESSAGE = (
    "帮我做一份 PUE 专题报表，时间范围最近 7 天，粒度按天，"
    "要一个 PUE 总览指标卡、一个各机房 PUE 对比柱状图，再加一个当前告警列表表格，"
    "表格要有设备名称、告警级别、发生时间三列。不要设备耗电排名。"
)


def _load_env() -> dict[str, str]:
    """读取 backend/.env（不覆盖已存在的环境变量）。"""
    env: dict[str, str] = {}
    env_file = _BACKEND_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


_ENV = _load_env()


def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name) or _ENV.get(name, default)


# --------------------------------------------------------------------------
# 两个供应商的调用封装
# --------------------------------------------------------------------------


def _stream_openai(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    timeout: float,
    extra: dict | None = None,
) -> tuple[int, int, str]:
    """OpenAI 兼容协议（方舟 / MiniMax 开放平台）。返回 (ttft_ms, total_ms, text)。"""
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 8000,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if extra:
        body.update(extra)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    t0 = time.perf_counter()
    ttft = -1
    chunks: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices") or []
            if not choices:
                continue
            text = (choices[0].get("delta") or {}).get("content")
            if text:
                if ttft < 0:
                    ttft = round((time.perf_counter() - t0) * 1000)
                chunks.append(text)
    return ttft, round((time.perf_counter() - t0) * 1000), "".join(chunks)


def _stream_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    timeout: float,
    extra: dict | None = None,
) -> tuple[int, int, str]:
    """Anthropic Messages 协议。返回 (ttft_ms, total_ms, text)。"""
    url = f"{base_url.rstrip('/')}/messages"
    body = {
        "model": model,
        "max_tokens": 8000,
        "stream": True,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if extra:
        body.update(extra)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    t0 = time.perf_counter()
    ttft = -1
    chunks: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if data.get("type") != "content_block_delta":
                continue
            delta = data.get("delta") or {}
            if delta.get("type") != "text_delta":
                continue
            text = delta.get("text")
            if text:
                if ttft < 0:
                    ttft = round((time.perf_counter() - t0) * 1000)
                chunks.append(text)
    return ttft, round((time.perf_counter() - t0) * 1000), "".join(chunks)


def _run_minimax(base_url: str, api_key: str, model: str, system: str, user: str, timeout: float,
                 extra: dict | None = None):
    if "/messages" in base_url:
        return _stream_anthropic(base_url, api_key, model, system, user, timeout, extra)
    return _stream_openai(base_url, api_key, model, system, user, timeout, extra)


# --------------------------------------------------------------------------
# 方案契约检查（复刻 app/schemas/report_plan.py 的关键硬规则）
# --------------------------------------------------------------------------

_COMPONENT_TYPES = {"metric_card", "line_chart", "bar_chart", "pie_chart", "table", "text"}
_DISPLAY_INTENTS = {"total", "trend", "comparison", "composition", "records"}


def _strip_thinking(text: str) -> tuple[str, bool]:
    """剥掉内联的 <think>...</think>。返回 (干净文本, 是否含思考)。

    MiniMax 默认把思考过程内联在 content 里，直接 json.loads 会炸；
    迁移时后端必须做同样的剥离（或改用 reasoning_split / thinking disabled）。
    """
    had = "<think>" in text or "</think>" in text
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip(), had


def _check_contract(text: str) -> list[str]:
    """返回契约问题列表；空列表表示通过。"""
    issues: list[str] = []
    body, _ = _strip_thinking(text)
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.M).strip()
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return ["输出不是合法 JSON"]
    if not isinstance(obj, dict):
        return ["顶层不是对象"]
    if obj.get("kind") != "plan_candidate":
        issues.append(f"kind={obj.get('kind')!r}（期望 plan_candidate）")
    candidate = obj.get("candidate")
    if not isinstance(candidate, dict):
        issues.append("candidate 缺失或不是对象")
        return issues
    sections = candidate.get("sections")
    if not isinstance(sections, list) or not sections:
        issues.append("sections 缺失或为空")
        sections = []
    for i, section in enumerate(sections):
        component = section.get("componentType")
        if component not in _COMPONENT_TYPES:
            issues.append(f"sections[{i}].componentType 非法={component!r}")
        if component == "text":
            if section.get("displayIntent"):
                issues.append(f"sections[{i}] 是 text 却带 displayIntent")
        elif section.get("displayIntent") not in _DISPLAY_INTENTS:
            issues.append(f"sections[{i}].displayIntent 非法={section.get('displayIntent')!r}")
        if component == "table":
            columns = section.get("columns")
            if not isinstance(columns, list) or not columns:
                issues.append(f"sections[{i}] 是 table 但没有 columns")
            elif not all(isinstance(c, dict) and c.get("label") for c in columns):
                issues.append(f"sections[{i}] 的 columns 缺 label")
        if not section.get("title"):
            issues.append(f"sections[{i}] 缺 title")
        if not section.get("metricIntents"):
            issues.append(f"sections[{i}] 缺 metricIntents")
    requirements = candidate.get("requirements")
    if not isinstance(requirements, dict) or not isinstance(requirements.get("metrics"), list):
        issues.append("requirements.metrics 缺失或不是数组")
    for field in ("assumptions", "suggestions", "referenceTemplates"):
        if not isinstance(candidate.get(field), list):
            issues.append(f"{field} 缺失或不是数组")
    return issues


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _bench(label: str, call, rounds: int, timeout: float) -> None:
    ttfts: list[int] = []
    totals: list[int] = []
    lengths: list[int] = []
    ok_count = 0
    think_count = 0
    for _ in range(rounds):
        try:
            ttft, total, text = call()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            print(f"{label:<26} HTTP {exc.code}: {detail}")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"{label:<26} {type(exc).__name__}: {exc}")
            return
        ttfts.append(ttft)
        totals.append(total)
        lengths.append(len(text))
        if "<think>" in text:
            think_count += 1
        issues = _check_contract(text)
        if not issues:
            ok_count += 1
        else:
            print(f"    契约问题：{issues[:3]}")
    rng = f"{min(totals) / 1000:.1f}-{max(totals) / 1000:.1f}s"
    print(
        f"{label:<26} 首字 {statistics.median(ttfts) / 1000:>6.1f}s | "
        f"总耗时 {statistics.median(totals) / 1000:>6.1f}s | "
        f"区间 {rng:>13} | 字符 {statistics.median(lengths):>5.0f} | "
        f"契约合规 {ok_count}/{len(totals)}"
        + (f" | 含内联思考 {think_count}/{len(totals)}" if think_count else "")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 延迟 A/B 压测")
    parser.add_argument("--rounds", type=int, default=3, help="每家跑几轮（默认 3）")
    parser.add_argument("--timeout", type=float, default=180.0, help="单次请求超时秒数")
    parser.add_argument("--only", choices=["volc", "minimax"], help="只跑其中一家")
    args = parser.parse_args()

    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user = _USER_MESSAGE
    print(f"提示词：{_PROMPT_PATH.name}（{len(system)} 字符），每家 {args.rounds} 轮\n")

    volc_key = _cfg("VOLC_API_KEY")
    volc_base = _cfg("VOLC_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    volc_model = _cfg("VOLC_MODEL", "doubao-seed-2-1-lite-260915")
    mm_key = _cfg("MINIMAX_API_KEY")
    mm_base = _cfg("MINIMAX_BASE_URL", "https://api.minimax.cn/v1")
    mm_model = _cfg("MINIMAX_MODEL", "MiniMax-M3")
    # 额外请求参数，例如 {"thinking": {"type": "disabled"}} 或 {"reasoning_split": true}
    mm_extra_raw = _cfg("MINIMAX_EXTRA_BODY", "")
    mm_extra: dict = {}
    if mm_extra_raw:
        try:
            mm_extra = json.loads(mm_extra_raw)
        except json.JSONDecodeError:
            print(f"MINIMAX_EXTRA_BODY 不是合法 JSON，已忽略：{mm_extra_raw!r}")
    mm_thinking = mm_extra.get("thinking", {}).get("type") if isinstance(mm_extra.get("thinking"), dict) else None

    if args.only != "minimax":
        if not volc_key:
            print("跳过方舟：VOLC_API_KEY 未配置")
        else:
            print(f"-> 方舟 {volc_model} @ {volc_base}")
            _bench(
                f"方舟 {volc_model}",
                lambda: _stream_openai(volc_base, volc_key, volc_model, system, user, args.timeout),
                args.rounds,
                args.timeout,
            )

    if args.only != "volc":
        if not mm_key:
            print("\n跳过 MiniMax：MINIMAX_API_KEY 未配置")
            print("  用法：MINIMAX_API_KEY=sk-api-xxx python3 scripts/bench_llm_latency.py")
        else:
            print(f"\n-> MiniMax {mm_model} @ {mm_base}"
                  f"{'（thinking=' + str(mm_thinking) + '）' if mm_thinking else ''}")
            _bench(
                f"MiniMax {mm_model}",
                lambda: _run_minimax(mm_base, mm_key, mm_model, system, user, args.timeout, mm_extra),
                args.rounds,
                args.timeout,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
