#!/usr/bin/env python3
"""Shared easy Korean progress lines for Telegram engine bridges (T-260910-003).

T-260910-001 r3 is the display contract: hide tools/commands/paths by default,
group repeated stages, never infer pass/fail from tool/log text, and treat
footers as turn state (응답 종료 / 중단) rather than overall goal complete.
"""
from __future__ import annotations

import os
import re

FLOW_STAGE_READ = "관련 내용 확인 중"
FLOW_STAGE_EDIT = "수정 중"
FLOW_STAGE_TEST = "테스트로 동작 확인 중"
FLOW_STAGE_WORK = "작업 진행 중"
FLOW_STAGE_WRAP = "결과 정리 중"
FLOW_EASY_STAGES = (
    FLOW_STAGE_READ,
    FLOW_STAGE_EDIT,
    FLOW_STAGE_TEST,
    FLOW_STAGE_WORK,
    FLOW_STAGE_WRAP,
)
FLOW_DONE_LABELS = {
    "sent": "응답 종료",
    "answered": "응답 종료",
    "ambient_final": "응답 종료",
    "interrupt": "중단",
    "ambient_reset": "중단",
    "reset": "중단",
    "cancelled": "중단",
    "failed": "중단",
    "timeout": "시간초과",
}

_TEST_RUN_RE = re.compile(
    r"(?i)(?:^|[^\w.-])(?:fvm\s+)?flutter\s+test(?:\s|$)"
    r"|(?:^|[^\w.-])dart\s+test(?:\s|$)"
    r"|(?:^|[^\w.-])pytest(?:\s|$)"
    r"|python[0-9.]*\s+-m\s+(?:pytest|unittest)"
    r"|(?:^|[^\w.-])go\s+test(?:\s|$)"
)


def detail_enabled(env_name: str, flag_path: str) -> bool:
    configured = os.environ.get(env_name)
    if configured is not None and configured.strip():
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return os.path.exists(os.path.expanduser(flag_path))


def flow_done_label(status: str) -> str:
    return FLOW_DONE_LABELS.get(status) or f"종료 · {status or 'unknown'}"


def format_flow_elapsed(seconds: float) -> str:
    total = int(max(0.0, float(seconds)))
    if total < 60:
        return f"{total}초"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}분 {secs}초" if secs else f"{minutes}분"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"


def is_test_run_text(text: str) -> bool:
    return bool(_TEST_RUN_RE.search(text or ""))


def parse_certain_test_result(text: str) -> str | None:
    """Flow/tool/screen/commentary is not a trusted current test result."""
    return None


def flow_tool_short_name(name: str) -> str:
    key = (name or "tool").strip()
    if "__" in key:
        key = key.rsplit("__", 1)[-1]
    elif "." in key:
        key = key.rsplit(".", 1)[-1]
    return key or "tool"


def classify_flow_stage(name: str = "", detail: str = "", line: str = "") -> str:
    blob = " ".join(part for part in (name, detail, line) if part).strip().lower()
    if not blob:
        return FLOW_STAGE_WORK
    if any(stage in (line or "") for stage in FLOW_EASY_STAGES):
        for stage in FLOW_EASY_STAGES:
            if (line or "").startswith(stage) or (line or "").strip() == stage:
                return stage
    if is_test_run_text(blob):
        return FLOW_STAGE_TEST
    short = flow_tool_short_name(name).lower() if name else ""
    if not short and " · " in (line or ""):
        short = flow_tool_short_name((line or "").split(" · ", 1)[0]).lower()
    tokens = f"{short} {blob}"
    if any(
        token in tokens
        for token in ("edit", "patch", "write", "apply_patch", "notebook", "search_replace")
    ):
        return FLOW_STAGE_EDIT
    if any(
        token in tokens
        for token in (
            "read", "search", "glob", "explore", "list", "view", "grep", "web",
            "open_page", "browser_snapshot", "browser_navigate", "navigate",
            "web_search", "web_fetch", "open_page_with_find",
        )
    ):
        return FLOW_STAGE_READ
    if "⏳" in blob:
        return FLOW_STAGE_WORK
    if any(token in blob for token in ("📄 읽기", "🔎 검색", "🔎 탐색", "📁 파일 찾기", "🌐 ", "🖼 ", "🖱 ", "🔗 ")):
        return FLOW_STAGE_READ
    if any(token in blob for token in ("🖊 편집", "🖊 작성", "🗑 삭제")):
        return FLOW_STAGE_EDIT
    if "테스트로 동작 확인" in blob:
        return FLOW_STAGE_TEST
    if "결과 정리" in blob:
        return FLOW_STAGE_WRAP
    return FLOW_STAGE_WORK


def user_visible_flow_line(text: str, *, detail: bool = False) -> str:
    line = (text or "").strip()
    if line.startswith("• "):
        line = line[2:].strip()
    if not line:
        return ""
    if detail:
        return line
    return classify_flow_stage(line=line)


def collapse_flow_steps(
    text: str,
    *,
    detail: bool = False,
) -> tuple[list[str], str]:
    groups: list[dict[str, str | int]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("• "):
            line = line[2:].strip()
        if not line:
            continue
        if not detail:
            line = user_visible_flow_line(line, detail=False)
            if not line:
                continue
        label, separator, extra = line.partition(" · ")
        label = label.strip()
        extra = extra.strip() if separator and detail else ""
        if groups and groups[-1]["label"] == label:
            groups[-1]["count"] = int(groups[-1]["count"]) + 1
            if extra:
                groups[-1]["detail"] = extra
        else:
            groups.append({"label": label, "detail": extra, "count": 1})
    rendered: list[str] = []
    for group in groups:
        count = f" ×{group['count']}" if detail and int(group["count"]) > 1 else ""
        extra = f" · {group['detail']}" if detail and group["detail"] else ""
        rendered.append(f"{group['label']}{count}{extra}")
    current = str(groups[-1]["label"]) if groups else ""
    return rendered, current
