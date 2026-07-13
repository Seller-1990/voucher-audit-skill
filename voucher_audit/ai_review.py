from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from .url_utils import normalize_openai_base_url
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class AIReviewResult:
    ok: bool
    message: str
    df: pd.DataFrame



def _df_head_records(df: pd.DataFrame, max_rows: int) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.head(max_rows).to_dict(orient="records")


def build_ai_payload(
    target_month: int,
    aux_rule_violations: pd.DataFrame,
    aux_suspect_wrong: pd.DataFrame,
    income_dim: pd.DataFrame,
    income_gm: pd.DataFrame,
    max_items: int,
) -> dict[str, Any]:
    return {
        "target_month": target_month,
        "sections": {
            "aux_rule_violations": _df_head_records(aux_rule_violations, max_items),
            "aux_suspect_wrong_account": _df_head_records(aux_suspect_wrong, max_items),
            "income_dim_anomalies": _df_head_records(income_dim, max_items),
            "income_gm_anomalies": _df_head_records(income_gm, max_items),
        },
        "notes": [
            "只基于异常明细做复核与建议；不要假设拥有全量数据。",
            "输出请给出：更像错误/更像口径变化/需确认 + 建议核对动作 + 追问清单。",
        ],
    }


def run_ai_review(
    model: str,
    max_output_tokens: int,
    payload: dict[str, Any],
    api_key_env: str = "OPENAI_API_KEY",
    base_url: str = "",
    api_key_override: str = "",
) -> AIReviewResult:
    api_key = (api_key_override or os.environ.get(api_key_env, "")).strip()
    if not api_key:
        return AIReviewResult(ok=False, message=f"未设置 {api_key_env}，跳过AI复核。", df=pd.DataFrame())

    try:
        from openai import OpenAI
    except Exception as e:
        return AIReviewResult(ok=False, message=f"OpenAI SDK 不可用：{type(e).__name__}: {e}", df=pd.DataFrame())

    base_url = normalize_openai_base_url((base_url or os.environ.get("OPENAI_BASE_URL") or "").strip())
    client = OpenAI(api_key=api_key, base_url=base_url or None)
    instructions = (
        "你是财务月结凭证审核助手。你会收到结构化的异常明细（来自收入成本表与辅助帐序时账）。\n"
        "请逐类输出复核意见：\n"
        "1) 判定：更像错误 / 更像口径变化 / 需业务确认\n"
        "2) 建议动作：具体到核对字段/找谁确认/需要什么补充材料\n"
        "3) 追问清单：给会计/业务/项目的确认问题\n"
        "输出格式必须是 JSON 数组，每个元素包含：category, severity, key, verdict, actions, questions。\n"
        "注意：不要编造不存在的数据。"
    )

    def _call_responses(input_text: str) -> str:
        # Some gateways are flaky; retry a few times on 5xx / gateway errors.
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = client.responses.create(
                    model=model,
                    input=input_text,
                    instructions=instructions,
                    max_output_tokens=max_output_tokens,
                )
                return getattr(resp, "output_text", "") or ""
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if any(x in msg for x in ["502", "bad gateway", "upstream_error", "internalservererror"]):
                    time.sleep(0.8 * (2**attempt))
                    continue
                raise
        assert last_err is not None
        raise last_err

    def _call_chat_completions(input_text: str) -> str:
        # 兼容部分代理网关：只支持 /v1/chat/completions，不支持 /v1/responses
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
            max_tokens=max_output_tokens,
            temperature=0,
        )
        try:
            return (resp.choices[0].message.content or "").strip()
        except Exception:
            return ""

    def _truncate(s: str, n: int = 800) -> str:
        s = "" if s is None else str(s)
        if len(s) <= n:
            return s
        return s[: n - 3] + "..."

    def _format_ai_call_error(e: Exception) -> str:
        raw = f"{type(e).__name__}: {e}"
        low = raw.lower()
        # 特征：代理/网关返回了 HTML（常见于 502/Cloudflare），OpenAI SDK 会把它当作 InternalServerError
        if "<!doctype html" in low or "<html" in low or "bad gateway" in low or "error code 502" in low:
            bu = base_url or "(空=直连官方)"
            return (
                "AI调用失败：网关返回了 HTML（疑似 502 Bad Gateway）。\n"
                f"- 当前 base_url：{bu}\n"
                "- 处理建议：\n"
                "  1) 若无需代理：把 base_url 留空或改为 https://api.openai.com/v1\n"
                "  2) 若使用代理：联系代理服务提供方，确认网关可用且路径包含 /v1\n"
                "  3) 若代理不支持 Responses API：改走 Chat Completions（本工具会自动尝试兼容模式）"
            )
        if "permissiondeniederror" in low or "blocked" in low:
            bu = base_url or "(空=直连官方)"
            return (
                "AI调用失败：PermissionDenied/blocked（权限或网关拦截）。\n"
                f"- 当前 base_url：{bu}\n"
                "- 处理建议：检查 API Key 权限/组织；如走代理请确认白名单与转发策略。"
            )
        if "upstream_error" in low:
            bu = base_url or "(空=直连官方)"
            return (
                "AI调用失败：upstream_error（上游/代理返回 400）。\n"
                f"- 当前 base_url：{bu}\n"
                "- 处理建议：优先排查代理网关；其次检查 model 是否存在、请求体大小是否过大。"
            )
        return f"AI调用失败：{_truncate(raw)}"

    try:
        input_text = json.dumps(payload, ensure_ascii=False)
        txt = _call_responses(input_text)
        used = "responses"
    except Exception as e1:
        # fallback: Chat Completions (only when the upstream explicitly requires it)
        m1 = str(e1)
        if any(x in m1 for x in ["chat/completions", "legacy protocol"]):
            try:
                input_text = json.dumps(payload, ensure_ascii=False)
                txt = _call_chat_completions(input_text)
                used = "chat_completions"
            except Exception as e2:
                msg1 = _format_ai_call_error(e1)
                msg2 = _format_ai_call_error(e2)
                return AIReviewResult(ok=False, message=f"{msg1}\n\n（兼容模式同样失败）\n{msg2}", df=pd.DataFrame())
        else:
            return AIReviewResult(ok=False, message=_format_ai_call_error(e1), df=pd.DataFrame())

    try:
        data = json.loads(txt)
        if isinstance(data, dict):
            data = [data]
        df = pd.DataFrame(data)
        msg = "AI复核完成。"
        if used == "chat_completions":
            msg = "AI复核完成（通过 Chat Completions 兼容模式）。"
        return AIReviewResult(ok=True, message=msg, df=df)
    except Exception as e:
        df = pd.DataFrame([{"raw": txt}])
        prefix = "AI输出不是可解析JSON"
        if used == "chat_completions":
            prefix = "AI输出不是可解析JSON（Chat Completions 兼容模式）"
        return AIReviewResult(ok=False, message=f"{prefix}：{type(e).__name__}: {e}", df=df)
