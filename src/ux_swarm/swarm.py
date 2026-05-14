from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, cast

logger = logging.getLogger(__name__)

from litellm import ModelResponse, acompletion, completion_cost

from playwright.async_api import async_playwright

from ux_swarm.agents import run_browser_agent, run_screenshot_agent, _strip_code_fence
from ux_swarm.errors import CliError
from ux_swarm.models import AgentResult, SwarmResult, UserType
from ux_swarm.users import distribute_users


async def _consolidate_friction_points(
        raw: list[str],
        model: str) -> tuple[list[str], list[int], float]:
    if not raw:
        return [], [], 0.0

    system = (
        "You are a UX research analyst. You have friction point observations from multiple "
        "synthetic users who tested the same UI. Many observations describe the same root problem "
        "in different words.\n\n"
        "Produce a deduplicated list of distinct usability problems. Merge all observations that "
        "share the same root cause into a single clear statement. Be aggressive: if in doubt, "
        "merge rather than split.\n\n"
        "Rules:\n"
        "- Each item describes exactly one distinct root problem\n"
        "- Phrase it as a specific, actionable finding (≤12 words)\n"
        "- Order by how many agents encountered it (most common first)\n"
        "- The output list will be much shorter than the input — that is expected and correct\n"
        "- For each item, count how many of the input observations it accounts for\n\n"
        "Return a JSON object with a single key 'friction_points' whose value is an array of "
        "objects, each with 'phrase' (string) and 'count' (integer). "
        "Return ONLY valid JSON. No explanation.")
    try:
        response = cast(
            ModelResponse,
            await acompletion(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": system
                    },
                    {
                        "role": "user",
                        "content": json.dumps(raw)
                    },
                ],
                max_tokens=2048,
            ),
        )
        data = json.loads(_strip_code_fence(response.choices[0].message.content or "{}"))
        items = data.get("friction_points", [])
        if isinstance(items, list) and items:
            cost = 0.0
            try:
                cost = completion_cost(completion_response=response,
                                       model=model)
            except Exception:
                pass
            seen: set[str] = set()
            phrases: list[str] = []
            counts: list[int] = []
            for item in items:
                phrase = str(item.get("phrase", item) if isinstance(item, dict) else item)
                count = int(item.get("count", 1) if isinstance(item, dict) else 1)
                if phrase not in seen:
                    seen.add(phrase)
                    phrases.append(phrase)
                    counts.append(count)
            return phrases, counts, cost
    except Exception:
        pass
    deduped = list(dict.fromkeys(raw))
    return deduped, [1] * len(deduped), 0.0


def _aggregate(
    results: list[AgentResult],
    target: str,
    task: str,
    model: str,
    num_agents: int,
    mode: Literal["screenshot", "browser"],
    friction_points: list[str] | None = None,
    friction_counts: list[int] | None = None,
    extra_cost: float = 0.0,
) -> SwarmResult:
    n = len(results)

    if n == 0:
        hint = ("Check your API key, model, and whether Chromium is installed."
                if mode == "browser" else
                "Check your API key and model configuration.")
        raise CliError(f"All {num_agents} agents failed. {hint}")

    completion_rate = sum(1 for r in results if r.status == "completed") / n
    moe = (1.96 * math.sqrt(completion_rate *
                            (1 - completion_rate) / n) if n > 1 else 0.0)

    by_label: dict[str, list[bool]] = {}
    for r in results:
        by_label.setdefault(r.user_type, []).append(r.status == "completed")
    user_breakdown = {label: sum(v) / len(v) for label, v in by_label.items()}

    total_cost = sum(r.cost for r in results) + extra_cost
    model_id = model.split("/", 1)[-1] if "/" in model else model

    steps = [r.steps_taken for r in results if r.status == "completed"]
    avg_steps = sum(steps) / len(steps) if mode == "browser" and steps else 0.0

    return SwarmResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        mode=mode,
        target=target,
        task=task,
        model=model_id,
        users=n,
        completion_rate=completion_rate,
        margin_of_error=moe,
        user_breakdown=user_breakdown,
        friction_points=friction_points or [],
        friction_counts=friction_counts or [],
        total_cost=total_cost,
        individual_results=results,
        avg_steps_to_completion=avg_steps,
    )


async def run_screenshot_swarm(
    target: str,
    task: str,
    users: list[UserType],
    num_agents: int,
    model: str,
    max_concurrent: int,
    on_agent_done: Callable[[int, int, AgentResult | None], None]
    | None = None,
) -> SwarmResult:
    assigned = distribute_users(users, num_agents)
    semaphore = asyncio.Semaphore(max_concurrent)
    results: list[AgentResult] = []
    completed_count = 0

    async def _run_agent(idx: int, user_type: UserType) -> None:
        nonlocal completed_count
        agent_result: AgentResult | None = None
        try:
            async with semaphore:
                decision, in_tok, out_tok, cost = await run_screenshot_agent(
                    target=target,
                    task=task,
                    user_type=user_type,
                    model=model,
                )
            status = ("completed" if decision.completed else
                      "abandoned" if decision.abandoned else "timeout")
            agent_result = AgentResult(
                agent_index=idx,
                user_type=user_type.label,
                status=status,
                abandonment_reason=decision.abandonment_reason,
                friction_points=decision.friction_observed,
                comment=decision.comment,
                target_element=decision.target_element,
                reasoning=decision.reasoning,
                steps_taken=1,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost=cost,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Agent %d failed: %s", idx, exc)
        finally:
            completed_count += 1
            if on_agent_done:
                on_agent_done(completed_count, num_agents, agent_result)

        if agent_result is not None:
            results.append(agent_result)

    async with asyncio.TaskGroup() as tg:
        for idx, user_type in enumerate(assigned):
            tg.create_task(_run_agent(idx, user_type))
            if idx < max_concurrent:
                await asyncio.sleep(0.3)

    raw_friction = [fp for r in results for fp in r.friction_points]
    consolidated_friction, friction_counts, consolidation_cost = await _consolidate_friction_points(
        raw_friction, model)
    return _aggregate(results, target, task, model, num_agents, "screenshot",
                      consolidated_friction, friction_counts, consolidation_cost)


async def run_browser_swarm(
    url: str,
    task: str,
    users: list[UserType],
    num_agents: int,
    model: str,
    max_concurrent: int,
    max_steps: int,
    viewport: int = 1280,
    headed: bool = False,
    on_agent_done: Callable[[int, int, AgentResult | None], None]
    | None = None,
    on_agent_step: Callable[[int, str, str, int], None] | None = None,
    upload_screenshot_factory: Callable[[int], Callable] | None = None,
) -> SwarmResult:
    assigned = distribute_users(users, num_agents)
    browser_sem = asyncio.Semaphore(max_concurrent)
    llm_sem = asyncio.Semaphore(max_concurrent)
    results: list[AgentResult] = []
    completed_count = 0

    async def _run_agent(idx: int, user_type: UserType) -> None:
        nonlocal completed_count
        agent_result: AgentResult | None = None

        def on_step(status: str, detail: str, step: int) -> None:
            if on_agent_step:
                on_agent_step(idx, status, detail, step)

        try:
            async with browser_sem:
                upload_fn = upload_screenshot_factory(idx) if upload_screenshot_factory else None
                agent_result, _, _, _ = await run_browser_agent(
                    browser=browser,
                    url=url,
                    task=task,
                    user_type=user_type,
                    model=model,
                    llm_semaphore=llm_sem,
                    max_steps=max_steps,
                    agent_index=idx,
                    viewport=viewport,
                    on_step=on_step,
                    upload_screenshot=upload_fn,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Agent %d failed: %s", idx, exc)
        finally:
            completed_count += 1
            if on_agent_done:
                on_agent_done(completed_count, num_agents, agent_result)

        if agent_result is not None:
            results.append(agent_result)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        async with asyncio.TaskGroup() as tg:
            for idx, user_type in enumerate(assigned):
                tg.create_task(_run_agent(idx, user_type))
                if idx < max_concurrent:
                    await asyncio.sleep(0.3)
        await browser.close()

    raw_friction = [fp for r in results for fp in r.friction_points]
    consolidated_friction, friction_counts, consolidation_cost = await _consolidate_friction_points(
        raw_friction, model)
    return _aggregate(results, url, task, model, num_agents, "browser",
                      consolidated_friction, friction_counts, consolidation_cost)
