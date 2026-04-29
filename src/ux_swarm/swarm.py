from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, cast

from litellm import ModelResponse, acompletion, completion_cost

from playwright.async_api import async_playwright

from ux_swarm.agent import run_screenshot_agent
from ux_swarm.browser_agent import run_browser_agent
from ux_swarm.cli import CliError
from ux_swarm.models import AgentResult, SwarmResult, UserType
from ux_swarm.personas import distribute_users

_LLM_CONCURRENCY = 3


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
    """Run N concurrent screenshot agents and return aggregated results."""
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
            agent_result = AgentResult(
                agent_index=idx,
                user_type=user_type.label,
                status=("completed" if decision.completed
                        else "abandoned" if decision.abandoned
                        else "timeout"),
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
        except Exception:
            pass
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
    consolidated_friction, consolidation_cost = await _consolidate_friction_points(
        raw_friction, model)
    return _aggregate(results, target, task, model, num_agents, "screenshot",
                      consolidated_friction, consolidation_cost)


async def _consolidate_friction_points(raw: list[str],
                                       model: str) -> tuple[list[str], float]:
    """Cluster semantically similar friction points into canonical phrases via a single LLM call."""
    if not raw:
        return raw, 0.0

    system = (
        "You are a UX research analyst consolidating friction point observations from multiple "
        "synthetic users who tested the same UI.\n\n"
        "Your goal is aggressive deduplication. Cluster observations that describe the same root "
        "usability problem into one canonical phrase — even if the wording, punctuation, or emphasis "
        "differs. When in doubt, merge rather than split. Ignore punctuation differences (dashes, "
        "apostrophes, capitalization) entirely.\n\n"
        "The canonical phrase should be concise (≤10 words), specific, and written in the same tone "
        "as the inputs. Choose the clearest phrasing from among the cluster.\n\n"
        "Return a JSON object with a single key 'canonical' whose value is an array of exactly the "
        "same length as the input array. Each element is the canonical phrase for the corresponding "
        "input. Observations that describe the same problem MUST have byte-for-byte identical "
        "canonical strings.\n\n"
        "Return ONLY valid JSON. No explanation.")
    messages = [
        {
            "role": "system",
            "content": system
        },
        {
            "role": "user",
            "content": json.dumps(raw)
        },
    ]
    try:
        response = cast(
            ModelResponse, await acompletion(
                model=model,
                messages=messages,
                max_tokens=2048,
                response_format={"type": "json_object"},
            ))
        data = json.loads(response.choices[0].message.content or "")
        canonical = data.get("canonical", [])
        if isinstance(canonical, list) and len(canonical) == len(raw):
            try:
                cost = completion_cost(completion_response=response,
                                       model=model)
            except Exception:
                cost = 0.0
            return [str(c) for c in canonical], cost
    except Exception:
        pass
    return raw, 0.0


def _aggregate(
    results: list[AgentResult],
    target: str,
    task: str,
    model: str,
    num_agents: int,
    mode: Literal["screenshot", "browser"],
    friction_points: list[str] | None = None,
    extra_cost: float = 0.0,
) -> SwarmResult:
    n = len(results)

    if n == 0:
        hint = ("Check your API key, model, and whether Chromium is installed."
                if mode == "browser" else
                "Check your API key and model configuration.")
        raise CliError(f"All {num_agents} agents failed. {hint}")

    completion_rate = sum(1 for r in results if r.status == "completed") / n
    moe = 1.96 * math.sqrt(completion_rate *
                           (1 - completion_rate) / n) if n > 1 else 0.0

    by_label: dict[str, list[bool]] = {}
    for r in results:
        by_label.setdefault(r.user_type, []).append(r.status == "completed")
    user_breakdown = {
        label: sum(outcomes) / len(outcomes)
        for label, outcomes in by_label.items()
    }

    if friction_points is None:
        friction_points = [fp for r in results for fp in r.friction_points]
    total_cost = sum(r.cost for r in results) + extra_cost
    model_id = model.split("/", 1)[-1] if "/" in model else model

    avg_steps = 0.0
    if mode == "browser":
        successful_steps = [r.steps_taken for r in results if r.status == "completed"]
        avg_steps = sum(successful_steps) / len(
            successful_steps) if successful_steps else 0.0

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
        friction_points=friction_points,
        total_cost=total_cost,
        individual_results=results,
        avg_steps_to_completion=avg_steps,
    )


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
) -> SwarmResult:
    """Run N concurrent browser agents and return aggregated results."""
    assigned = distribute_users(users, num_agents)
    browser_sem = asyncio.Semaphore(max_concurrent)
    llm_sem = asyncio.Semaphore(_LLM_CONCURRENCY)

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
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            completed_count += 1
            if on_agent_done:
                on_agent_done(completed_count, num_agents, agent_result)

        if agent_result is not None:
            results.append(agent_result)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)
        async with asyncio.TaskGroup() as tg:
            for idx, user_type in enumerate(assigned):
                tg.create_task(_run_agent(idx, user_type))
                if idx < max_concurrent:
                    await asyncio.sleep(0.3)
        await browser.close()

    raw_friction = [fp for r in results for fp in r.friction_points]
    consolidated_friction, consolidation_cost = await _consolidate_friction_points(
        raw_friction, model)
    return _aggregate(results, url, task, model, num_agents, "browser",
                      consolidated_friction, consolidation_cost)
