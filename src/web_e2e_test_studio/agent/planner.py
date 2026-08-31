"""VLM planning adapter that only accepts registered structured actions."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from openai import DefaultHttpxClient, OpenAI

from web_e2e_test_studio.agent.actions import ActionSpec
from web_e2e_test_studio.agent.loop import AgentFeedback
from web_e2e_test_studio.agent.planning import (
    PlannedAction,
    PlanningResult,
    PlanningStatus,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionStatus,
    ElementFingerprint,
    ElementTarget,
    FrameRef,
    JsonValue,
    Locator,
    Observation,
    VerificationStatus,
)


class PlannerProtocolError(ValueError):
    """Raised when a model response is unsafe or structurally invalid."""


class VLMBackend(Protocol):
    """Small seam that keeps the agent loop independent from one provider."""

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        screenshot_path: str,
    ) -> dict[str, Any]: ...


class StructuredVLMPlanner:
    """Turns an observation into one validated, potentially multi-action plan."""

    def __init__(self, backend: VLMBackend) -> None:
        self._backend = backend
        self._memories: tuple[str, ...] = ()

    def set_memories(self, memories: Sequence[str]) -> None:
        """Supply task-scoped working memory without changing planner protocol."""
        self._memories = tuple(memory for memory in memories if memory.strip())

    def plan(
        self,
        request: str,
        observation: Observation,
        history: Sequence[AgentFeedback],
        actions: Sequence[ActionSpec],
    ) -> PlanningResult:
        system_prompt = _system_prompt(actions)
        user_prompt = _user_prompt(request, observation, history, self._memories)
        plan = self._request_valid_plan(
            system_prompt,
            user_prompt,
            observation.screenshot_path,
            actions,
            observation,
        )
        plan = _apply_remembered_link_policy(
            plan,
            request,
            self._memories,
            actions,
        )
        plan = _apply_managed_download_policy(plan, request, actions)
        plan = _apply_completion_policy(plan, request, history)
        known_download = _latest_verified_download_path(history)
        plan = _apply_spreadsheet_extraction_policy(
            plan,
            request,
            known_download,
            actions,
        )
        if (
            known_download
            and _asks_for_download_path(plan)
            and _requires_download_processing(request)
        ):
            correction = (
                "\n\nARTIFACT CORRECTION: The downloaded file path is already "
                f"known and verified: {json.dumps(known_download)}. Do not ask "
                "the user for this path. Use that exact path with a suitable "
                "registered external file-processing action. If no suitable "
                "tool is registered, ask only for registration of the missing "
                "tool capability."
            )
            plan = self._request_valid_plan(
                system_prompt,
                user_prompt + correction,
                observation.screenshot_path,
                actions,
                observation,
            )
            plan = _apply_managed_download_policy(plan, request, actions)
            plan = _apply_spreadsheet_extraction_policy(
                plan,
                request,
                known_download,
                actions,
            )
            if _asks_for_download_path(plan):
                return PlanningResult.ask(
                    "The downloaded file is available at "
                    f"{known_download}. A registered file-processing tool is "
                    "required to inspect it."
                )
        if _is_premature_file_completion(plan, request, history):
            correction = (
                "\n\nCORRECTION: The previous response completed too early. "
                "The original goal requires opening/reading/extracting data "
                "from the downloaded file, so download is only an intermediate "
                "step. Choose a suitable registered external action now. "
                "Use the downloaded path from previous_rounds. If no suitable "
                "file-processing tool is registered, return ask requesting "
                "registration of that specific capability."
            )
            plan = self._request_valid_plan(
                system_prompt,
                user_prompt + correction,
                observation.screenshot_path,
                actions,
                observation,
            )
            plan = _apply_managed_download_policy(plan, request, actions)
            plan = _apply_spreadsheet_extraction_policy(
                plan,
                request,
                known_download,
                actions,
            )
            if _is_premature_file_completion(plan, request, history):
                return PlanningResult.ask(
                    "A registered file-processing tool is required to read "
                    "the downloaded file and extract the requested information."
                )
        return _apply_completion_policy(plan, request, history)

    def _request_valid_plan(
        self,
        system_prompt: str,
        user_prompt: str,
        screenshot_path: str,
        actions: Sequence[ActionSpec],
        observation: Observation,
    ) -> PlanningResult:
        try:
            raw = self._backend.complete_json(
                system_prompt,
                user_prompt,
                screenshot_path,
            )
            return _parse_plan(raw, actions, observation.interactive_elements)
        except PlannerProtocolError as first_error:
            correction = (
                "\n\nPROTOCOL CORRECTION: Your previous response was rejected: "
                f"{first_error}. Return one short valid JSON object only, "
                "matching the registered planning schema. Do not use Markdown "
                "or explanatory text outside the JSON object."
            )
            raw = self._backend.complete_json(
                system_prompt,
                user_prompt + correction,
                screenshot_path,
            )
            return _parse_plan(raw, actions, observation.interactive_elements)


class OpenAICompatibleVLMBackend:
    """OpenAI-compatible multimodal JSON backend used only while authoring."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        ignore_proxy: bool = True,
        timeout: float | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("A VLM API key is required")
        client_arguments: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
        }
        if ignore_proxy:
            client_arguments["http_client"] = DefaultHttpxClient(trust_env=False)
        if timeout is not None:
            client_arguments["timeout"] = timeout
        self._client = OpenAI(**client_arguments)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    @classmethod
    def from_env(
        cls,
        *,
        timeout: float | None = None,
    ) -> OpenAICompatibleVLMBackend:
        api_key = os.getenv("VLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        model = os.getenv("VLM_MODEL", "gpt-4o")
        base_url = os.getenv("VLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        ignore_proxy = _environment_flag("VLM_IGNORE_PROXY")
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            ignore_proxy=ignore_proxy,
            timeout=timeout,
        )

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        screenshot_path: str,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        if screenshot_path:
            screenshot = Path(screenshot_path)
            if screenshot.is_file():
                mime = mimetypes.guess_type(screenshot.name)[0] or "image/png"
                encoded = base64.b64encode(screenshot.read_bytes()).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{encoded}",
                            "detail": "high",
                        },
                    }
                )
        client: Any = self._client
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            response_format={"type": "json_object"},
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        text = response.choices[0].message.content or ""
        parsed = _parse_json_object_response(text)
        if not isinstance(parsed, dict):
            raise PlannerProtocolError("VLM response must be a JSON object")
        return parsed


def _parse_json_object_response(text: str) -> dict[str, Any]:
    """Accept a JSON object with optional local-model prose or code fences."""
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        first_newline = candidate.find("\n")
        if first_newline >= 0:
            candidate = candidate[first_newline + 1 : -3].strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed = None
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            prefix = candidate[:index].strip()
            suffix = candidate[index + end :].strip()
            if isinstance(value, dict) and "{" not in prefix + suffix:
                parsed = value
                break
        if parsed is None:
            raise PlannerProtocolError("VLM did not return valid JSON") from None
    if not isinstance(parsed, dict):
        raise PlannerProtocolError("VLM response must be a JSON object")
    return parsed


def _system_prompt(actions: Sequence[ActionSpec]) -> str:
    schemas = [spec.to_dict() for spec in actions]
    return (
        "You assist with recording a deterministic Web E2E test. "
        "Return a short ordered list of registered actions, ask one blocking "
        "question, or declare the requested goal complete. Never emit Python, "
        "JavaScript, shell commands, hidden selectors, or an unregistered action. "
        "For browser actions whose registered specification has locator_required=true, "
        "select an element_id from observation.interactive_elements. Omit element_id or "
        "set it to null for actions that do not target a page element. Do not invent a "
        "locator when an indexed element is available. Return one JSON "
        "object matching one of:\n"
        '{"status":"actions","thought":"...","confidence":0.0,"actions":['
        '{"intent":"...","expected_result":"...","action":'
        '{"name":"...","channel":"browser|api|external",'
        '"arguments":{},"element_id":"e17","tool_ref":null,'
        '"timeout_seconds":30}}]}\n'
        '{"status":"ask","question":"...","memory":"optional durable fact"}\n'
        '{"status":"complete","summary":"...","memory":"optional durable fact"}\n'
        "The optional memory field is a short factual note from the current "
        "observation that may be needed in later rounds. Do not copy plans, "
        "reasoning, or information already present in working_memory. "
        "Credentials and secret values may be remembered when they are "
        "required to complete the user's local workflow. "
        "Ask only when required user-only information is missing and no safe "
        "registered action or completion is possible. A successfully verified "
        "download/export/save with a returned path completes a download-only "
        "goal: return complete with that path. Do not ask whether to open the "
        "file and do not invoke an external tool to inspect it unless the "
        "original request explicitly asks to open, read, extract, inspect, or "
        "analyze the downloaded content. When it does, downloading is only an "
        "intermediate step: continue with a suitable registered external tool "
        "and complete only after the requested information is returned.\n"
        "Registered action schemas:\n" + json.dumps(schemas, ensure_ascii=False, indent=2)
    )


def _environment_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _user_prompt(
    request: str,
    observation: Observation,
    history: Sequence[AgentFeedback],
    memories: Sequence[str] = (),
) -> str:
    history_payload = [
        {
            "plan_status": item.plan.status.value,
            "records": [
                {
                    "action": record.task.action.to_dict(),
                    "intent": record.task.intent,
                    "execution_status": record.result.status.value,
                    "output": record.result.output,
                    "error": record.result.error,
                    "verification": record.verification.to_dict(),
                    "after_url": record.after.url,
                }
                for record in item.execution.records
            ],
        }
        for item in history[-8:]
    ]
    observation_payload = observation.to_dict()
    observation_payload["page_text"] = observation.page_text[:8000]
    observation_payload["interactive_elements"] = list(observation.interactive_elements[:100])
    return json.dumps(
        {
            "request": request,
            "observation": observation_payload,
            "working_memory": list(memories),
            "previous_rounds": history_payload,
        },
        ensure_ascii=False,
        indent=2,
    )


def _apply_completion_policy(
    plan: PlanningResult,
    request: str,
    history: Sequence[AgentFeedback],
) -> PlanningResult:
    if plan.status is not PlanningStatus.ASK or not _is_download_only_goal(request):
        return plan
    for feedback in reversed(history):
        for record in reversed(feedback.execution.records):
            if (
                record.task.action.name == "download"
                and record.result.status is ActionStatus.SUCCESS
                and record.verification.status is VerificationStatus.VERIFIED
            ):
                path = str(record.result.output.get("path", "")).strip()
                filename = str(record.result.output.get("filename", "")).strip()
                artifact = path or filename
                summary = (
                    f"Download completed: {artifact}"
                    if artifact
                    else "Download completed successfully"
                )
                return PlanningResult.complete(summary)
    return plan


_URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。；;）)]+", re.IGNORECASE)
_OPEN_LINK_TERMS = ("open", "navigate", "visit", "打开", "访问", "进入")
_LINK_TERMS = ("link", "url", "链接", "网址")
_LINK_STOP_WORDS = {
    "this",
    "that",
    "the",
    "link",
    "url",
    "open",
    "navigate",
    "visit",
}


def _apply_remembered_link_policy(
    plan: PlanningResult,
    request: str,
    memories: Sequence[str],
    actions: Sequence[ActionSpec],
) -> PlanningResult:
    """Prefer exact remembered URLs over visually guessed link-like controls."""
    folded_request = request.casefold()
    if not (
        any(term in folded_request for term in _OPEN_LINK_TERMS)
        and any(term in folded_request for term in _LINK_TERMS)
        and any(
            spec.name == "navigate" and spec.channel is ActionChannel.BROWSER for spec in actions
        )
    ):
        return plan

    request_tokens = {
        token
        for token in re.findall(r"[a-z0-9_-]{3,}", folded_request)
        if token not in _LINK_STOP_WORDS
    }
    candidates: list[tuple[str, int]] = []
    for memory in memories:
        memory_tokens = set(re.findall(r"[a-z0-9_-]{3,}", memory.casefold()))
        score = len(request_tokens & memory_tokens)
        candidates.extend((url.rstrip(".,:;"), score) for url in _URL_PATTERN.findall(memory))
    if not candidates:
        return plan

    best_score = max(score for _url, score in candidates)
    best_urls = list(dict.fromkeys(url for url, score in candidates if score == best_score))
    if len(best_urls) != 1:
        return PlanningResult.ask(
            "I found multiple matching links in working memory. "
            "Please specify which one to open:\n" + "\n".join(f"- {url}" for url in best_urls)
        )

    url = best_urls[0]
    return PlanningResult.actions(
        (
            PlannedAction(
                Action(
                    name="navigate",
                    channel=ActionChannel.BROWSER,
                    arguments={"url": url},
                ),
                intent=f"Open the exact link from working memory: {url}",
                expected_result=f"The browser navigates to {url}",
            ),
        ),
        thought=(
            "The requested URL is already present in working memory; "
            "use deterministic navigation instead of guessing a page control."
        ),
        confidence=max(plan.confidence, 0.99),
        memory=plan.memory,
    )


def _apply_managed_download_policy(
    plan: PlanningResult,
    request: str,
    actions: Sequence[ActionSpec],
) -> PlanningResult:
    if (
        plan.status is not PlanningStatus.ACTIONS
        or not _request_mentions_download(request)
        or not any(
            spec.name == "download" and spec.channel is ActionChannel.BROWSER for spec in actions
        )
    ):
        return plan
    upgraded: list[PlannedAction] = []
    changed = False
    for item in plan.planned_actions:
        action = item.action
        locator = action.locator
        locator_text = " ".join(
            value
            for value in (
                locator.value if locator else "",
                locator.name if locator and locator.name else "",
                item.intent,
                item.expected_result,
            )
            if value
        ).casefold()
        if (
            action.channel is ActionChannel.BROWSER
            and action.name == "click"
            and any(
                term in locator_text
                for term in (
                    "download",
                    "export",
                    "下载",
                    "导出",
                )
            )
        ):
            action = replace(action, name="download")
            changed = True
        upgraded.append(PlannedAction(action, item.intent, item.expected_result))
    if not changed:
        return plan
    return PlanningResult.actions(
        tuple(upgraded),
        thought=plan.thought,
        confidence=plan.confidence,
    )


def _request_mentions_download(request: str) -> bool:
    normalized = request.casefold()
    if any(term in normalized for term in ("\u4e0b\u8f7d", "\u5bfc\u51fa")):
        return True
    return any(term in normalized for term in ("download", "export", "下载", "导出"))


def _latest_verified_download_path(
    history: Sequence[AgentFeedback],
) -> str:
    for feedback in reversed(history):
        for record in reversed(feedback.execution.records):
            if (
                record.task.action.name == "download"
                and record.result.status is ActionStatus.SUCCESS
                and record.verification.status is VerificationStatus.VERIFIED
            ):
                path = record.result.output.get("path")
                if isinstance(path, str) and path.strip():
                    return path.strip()
    return ""


def _apply_spreadsheet_extraction_policy(
    plan: PlanningResult,
    request: str,
    known_download: str,
    actions: Sequence[ActionSpec],
) -> PlanningResult:
    if (
        plan.status is not PlanningStatus.ACTIONS
        or not known_download.lower().endswith((".xlsx", ".xlsm"))
        or not _requires_download_processing(request)
        or not any(
            spec.name == "extract_spreadsheet" and spec.channel is ActionChannel.EXTERNAL
            for spec in actions
        )
    ):
        return plan
    upgraded: list[PlannedAction] = []
    changed = False
    for item in plan.planned_actions:
        action = item.action
        if (
            action.name == "run_external_tool"
            and action.channel is ActionChannel.EXTERNAL
            and not action.arguments
            and _looks_like_file_extraction(item)
        ):
            query = item.expected_result or item.intent or request
            action = Action(
                name="extract_spreadsheet",
                channel=ActionChannel.EXTERNAL,
                arguments={
                    "file_path": known_download,
                    "query": query,
                },
                timeout_seconds=action.timeout_seconds,
            )
            changed = True
        upgraded.append(PlannedAction(action, item.intent, item.expected_result))
    if not changed:
        return plan
    return PlanningResult.actions(
        tuple(upgraded),
        thought=plan.thought,
        confidence=plan.confidence,
    )


def _looks_like_file_extraction(item: PlannedAction) -> bool:
    description = f"{item.intent} {item.expected_result}".casefold()
    return any(
        term in description
        for term in (
            "read",
            "open",
            "inspect",
            "extract",
            "workbook",
            "spreadsheet",
            "excel",
            "\u8bfb\u53d6",
            "\u6253\u5f00",
            "\u63d0\u53d6",
            "\u83b7\u53d6",
        )
    )


def _asks_for_download_path(plan: PlanningResult) -> bool:
    if plan.status is not PlanningStatus.ASK:
        return False
    normalized = plan.question.casefold()
    unicode_path_terms = (
        "\u5b8c\u6574\u8def\u5f84",
        "\u6587\u4ef6\u8def\u5f84",
        "\u4e0b\u8f7d\u8def\u5f84",
        "\u6587\u4ef6\u4f4d\u7f6e",
        "\u4fdd\u5b58\u4f4d\u7f6e",
        "\u5728\u54ea\u91cc",
    )
    unicode_file_terms = ("\u6587\u4ef6", "\u4e0b\u8f7d")
    if any(term in normalized for term in unicode_path_terms) and any(
        term in normalized for term in unicode_file_terms
    ):
        return True
    path_terms = (
        "path",
        "location",
        "where",
        "完整路径",
        "文件路径",
        "下载路径",
        "文件位置",
        "保存位置",
        "在哪里",
    )
    file_terms = ("file", "download", "文件", "下载")
    return any(term in normalized for term in path_terms) and any(
        term in normalized for term in file_terms
    )


def _is_premature_file_completion(
    plan: PlanningResult,
    request: str,
    history: Sequence[AgentFeedback],
) -> bool:
    if plan.status is not PlanningStatus.COMPLETE or not _requires_download_processing(request):
        return False
    downloaded = False
    processed = False
    for feedback in history:
        for record in feedback.execution.records:
            succeeded = (
                record.result.status is ActionStatus.SUCCESS
                and record.verification.status is VerificationStatus.VERIFIED
            )
            if record.task.action.name == "download" and succeeded:
                downloaded = True
                processed = False
            elif downloaded and record.task.action.channel is ActionChannel.EXTERNAL and succeeded:
                processed = True
    return downloaded and not processed


def _is_download_only_goal(request: str) -> bool:
    normalized = request.casefold()
    download_terms = (
        "download",
        "export",
        "save",
        "下载",
        "导出",
        "保存",
    )
    followup_terms = (
        "analyze",
        "analyse",
        "inspect",
        "open",
        "process",
        "read",
        "extract",
        "verify content",
        "分析",
        "打开",
        "读取",
        "提取",
        "获取其中",
        "检查内容",
        "处理文件",
    )
    return any(term in normalized for term in download_terms) and not any(
        term in normalized for term in followup_terms
    )


def _requires_download_processing(request: str) -> bool:
    normalized = request.casefold()
    if any(
        term in normalized
        for term in (
            "\u6253\u5f00",
            "\u8bfb\u53d6",
            "\u63d0\u53d6",
            "\u83b7\u53d6",
            "\u89e3\u6790",
            "\u68c0\u67e5",
            "\u5904\u7406",
        )
    ):
        return True
    terms = (
        "analyze",
        "analyse",
        "extract",
        "inspect",
        "open",
        "parse",
        "process",
        "read",
        "分析",
        "打开",
        "读取",
        "提取",
        "解析",
        "检查内容",
        "处理文件",
        "获取其中",
    )
    return any(term in normalized for term in terms)


def _parse_plan(
    raw: dict[str, Any],
    actions: Sequence[ActionSpec],
    elements: Sequence[dict[str, JsonValue]] = (),
) -> PlanningResult:
    raw_kind = str(raw.get("status", "")).strip().lower()
    try:
        status = PlanningStatus(raw_kind)
    except ValueError as exc:
        raise PlannerProtocolError(f"Unknown VLM plan status: {raw_kind!r}") from exc

    if status is PlanningStatus.ASK:
        question = str(raw.get("question", "")).strip()
        if not question:
            raise PlannerProtocolError("Ask plan is missing a question")
        return PlanningResult.ask(question, memory=str(raw.get("memory", "")).strip())

    if status is PlanningStatus.COMPLETE:
        return PlanningResult.complete(
            str(raw.get("summary", "")).strip(),
            memory=str(raw.get("memory", "")).strip(),
        )

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise PlannerProtocolError("Actions plan requires a non-empty actions array")
    planned: list[PlannedAction] = []
    for raw_planned in raw_actions:
        if not isinstance(raw_planned, dict):
            raise PlannerProtocolError("Each planned action must be an object")
        raw_action = raw_planned.get("action")
        action = _parse_action(raw_action, actions, elements)
        planned.append(
            PlannedAction(
                action,
                str(raw_planned.get("intent", "")).strip(),
                str(raw_planned.get("expected_result", "")).strip(),
            )
        )
    confidence = raw.get("confidence", 0.0)
    return PlanningResult.actions(
        tuple(planned),
        thought=str(raw.get("thought", "")).strip(),
        memory=str(raw.get("memory", "")).strip(),
        confidence=(
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else 0.0
        ),
    )


def _parse_action(
    raw_action: Any,
    actions: Sequence[ActionSpec],
    elements: Sequence[dict[str, JsonValue]] = (),
) -> Action:
    if not isinstance(raw_action, dict):
        raise PlannerProtocolError("Planned item is missing an action object")
    name = str(raw_action.get("name", ""))
    channel = str(raw_action.get("channel", ""))
    matching = [spec for spec in actions if spec.name == name and spec.channel.value == channel]
    if not matching:
        raise PlannerProtocolError(f"Model action {name!r} on {channel!r} is not registered")

    raw_locator = raw_action.get("locator")
    locator = Locator.from_dict(raw_locator) if isinstance(raw_locator, dict) else None
    raw_element_id = raw_action.get("element_id")
    element_id = str(raw_element_id).strip() if raw_element_id is not None else ""
    target = _target_from_element_id(element_id, elements) if element_id else None
    raw_arguments = raw_action.get("arguments", {})
    if not isinstance(raw_arguments, dict):
        raise PlannerProtocolError("Action arguments must be a JSON object")
    action = Action(
        name=name,
        channel=matching[0].channel,
        arguments=dict(raw_arguments),
        locator=locator,
        target=target,
        tool_ref=(str(raw_action["tool_ref"]) if raw_action.get("tool_ref") is not None else None),
        timeout_seconds=(
            float(raw_action["timeout_seconds"])
            if isinstance(raw_action.get("timeout_seconds"), (int, float))
            and not isinstance(raw_action.get("timeout_seconds"), bool)
            else None
        ),
    )
    try:
        matching[0].validate(action)
    except ValueError as exc:
        raise PlannerProtocolError(str(exc)) from exc

    return action


def _target_from_element_id(
    element_id: str,
    elements: Sequence[dict[str, JsonValue]],
) -> ElementTarget:
    element = next(
        (item for item in elements if str(item.get("element_id", "")) == element_id),
        None,
    )
    if element is None:
        raise PlannerProtocolError(f"Unknown observation element_id: {element_id}")
    candidates: list[Locator] = []
    test_id = str(element.get("test_id", "")).strip()
    role = str(element.get("role", "")).strip()
    name = str(element.get("name", "")).strip()
    label = str(element.get("label", "")).strip()
    text = str(element.get("text", "")).strip()
    if test_id:
        candidates.append(Locator("test_id", test_id))
    if role and name:
        candidates.append(Locator("role", role, name=name))
    if label:
        candidates.append(Locator("label", label))
    if text:
        candidates.append(Locator("text", text))
    if not candidates:
        raise PlannerProtocolError(f"Element {element_id} has no replayable locator")
    raw_frame_path = element.get("frame_path", [])
    frame_path = tuple(
        FrameRef.from_dict(item)
        for item in raw_frame_path
        if isinstance(item, dict)
    ) if isinstance(raw_frame_path, list) else ()
    return ElementTarget(
        frame_path=frame_path,
        candidates=tuple(candidates),
        fingerprint=ElementFingerprint(
            tag=str(element.get("tag", "")),
            role=role,
            accessible_name=name,
            text=text,
            attributes={"data-testid": test_id} if test_id else {},
        ),
        element_id=element_id,
    )


__all__ = [
    "OpenAICompatibleVLMBackend",
    "PlannerProtocolError",
    "StructuredVLMPlanner",
    "VLMBackend",
]
