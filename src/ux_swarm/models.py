from typing import Literal
from pydantic import BaseModel


class UserType(BaseModel):
    label: str
    weight: float  # relative weight for random sampling, not a probability
    description: str  # injected verbatim into the LLM system prompt as the persona
    accessibility: str | None = None  # "screen_reader" → keyboard-only, no screenshot


class ScreenshotDecision(BaseModel):
    target_element: str
    reasoning: str
    comment: str  # first-person one-sentence summary; comes from this call, not a second one
    friction_observed: list[str]
    completed: bool
    abandoned: bool
    abandonment_reason: str | None


class BrowserStep(BaseModel):
    thinking: str
    action: str  # expected: click | type | scroll | hover | press_key | select_option | done | give_up
    element_index: int | None  # index into the extracted element list; None for scroll/press_key/done/give_up
    text: str  # type: text to type; press_key: key name; select_option: option; scroll: px (e.g. "500"); else empty
    friction_observed: list[str]
    success: bool | None  # True=done success, False=give_up; None for all other actions


class AgentResult(BaseModel):
    agent_index: int
    user_type: str
    status: Literal["completed", "abandoned", "timeout", "error"]
    abandonment_reason: str | None
    friction_points: list[str]
    comment: str
    target_element: str | None = None  # screenshot mode only
    reasoning: str | None = None  # screenshot mode only
    steps_taken: int  # always 1 in screenshot mode
    input_tokens: int
    output_tokens: int
    cost: float
    actions_taken: list[
        str] | None = None  # browser mode: ["click: text=Sign Up", ...]
    urls_visited: list[str] | None = None  # browser mode: pages navigated to
    step_screenshots: list[str | None] | None = None  # browser mode: public URLs, one per step
    step_frictions: list[list[str]] | None = None  # browser mode: friction observed at each step
    step_thinking: list[str] | None = None  # browser mode: LLM thinking at each step
    duration: float | None = None  # browser mode: wall-clock seconds


class SwarmResult(BaseModel):
    timestamp: str  # ISO 8601
    mode: Literal["screenshot", "browser"]
    target: str
    task: str
    model: str
    users: int
    completion_rate: float
    margin_of_error: float  # 1.96 * sqrt(p*(1-p)/n)
    user_breakdown: dict[str, float]  # label → completion rate
    friction_points: list[str]  # canonical phrases, deduplicated
    friction_counts: list[int] = []  # parallel to friction_points: agents that hit each issue
    total_cost: float
    individual_results: list[AgentResult]
    avg_steps_to_completion: float = 0.0  # browser mode; 0.0 in screenshot mode
