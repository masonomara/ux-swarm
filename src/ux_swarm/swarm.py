from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from datetime import datetime, timezone

import click

from ux_swarm.agent import run_screenshot_agent
from ux_swarm.models import AgentResult, SwarmResult, UserType
from ux_swarm.personas import distribute_users


async def run_screenshot_swarm(
    target: str,
    task: str,
    users: list[UserType],
    num_agents: int,
    model: str,
    max_concurrent: int,
    on_agent_done: Callable[[int, int], None] | None = None,
) -> SwarmResult:
    """Run N concurrent screenshot agents and return aggregated results."""
    assigned = distribute_users(users, num_agents)
    semaphore = asyncio.Semaphore(max_concurrent)

    results: list[AgentResult] = []
    completed_count = 0

    async def _run_agent(idx: int, user_type: UserType) -> None:
        nonlocal completed_count
        try:
            async with semaphore:
                decision, in_tok, out_tok, cost = await run_screenshot_agent(
                    target=target,
                    task=task,
                    user_type=user_type,
                    model=model,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            completed_count += 1
            if on_agent_done:
                on_agent_done(completed_count, num_agents)

        results.append(AgentResult(
            agent_index=idx,
            user_type=user_type.label,
            completed=decision.completed,
            abandoned=decision.abandoned,
            abandonment_reason=decision.abandonment_reason,
            friction_points=decision.friction_observed,
            comment=decision.comment,
            target_element=decision.target_element,
            reasoning=decision.reasoning,
            steps_taken=1,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost=cost,
        ))

    async with asyncio.TaskGroup() as tg:
        for idx, user_type in enumerate(assigned):
            tg.create_task(_run_agent(idx, user_type))
            if idx < max_concurrent:
                await asyncio.sleep(0.3)

    return _aggregate(results, target, task, model, num_agents)


def _aggregate(
    results: list[AgentResult],
    target: str,
    task: str,
    model: str,
    num_agents: int,
) -> SwarmResult:
    """Aggregate individual agent results into a SwarmResult."""
    n = len(results)

    if n == 0:
        raise click.ClickException(
            f"All {num_agents} agents failed. Check your API key and model configuration."
        )

    completion_rate = sum(1 for r in results if r.completed) / n
    moe = 1.96 * math.sqrt(completion_rate *
                           (1 - completion_rate) / n) if n > 1 else 0.0

    by_label: dict[str, list[bool]] = {}
    for r in results:
        by_label.setdefault(r.user_type, []).append(r.completed)
    user_breakdown = {
        label: sum(outcomes) / len(outcomes)
        for label, outcomes in by_label.items()
    }

    friction_points = [fp for r in results for fp in r.friction_points]
    total_cost = sum(r.cost for r in results)
    model_id = model.split("/", 1)[-1] if "/" in model else model

    return SwarmResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        mode="screenshot",
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
    )
