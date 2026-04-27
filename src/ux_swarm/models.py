from typing import Literal
from pydantic import BaseModel


class UserType(BaseModel):
    """A simulated user persona. Weights are relative, not probabilities."""
    label: str
    weight: float  # relative weight for random sampling, not a probability
    description: str  # injected verbatim into the LLM system prompt as the persona


class ScreenshotDecision(BaseModel):
    """LLM response for a single screenshot agent."""
    target_element: str
    reasoning: str
    comment: str  # first-person one-sentence summary; comes from this call, not a second one
    friction_observed: list[str]
    completed: bool
    abandoned: bool
    abandonment_reason: str | None


class BrowserAction(BaseModel):
    """A single browser interaction step returned by the LLM."""
    action: str
    x: int | None  # None for scroll
    y: int | None  # None for scroll
    value: str | None  # text for type; None otherwise
    reasoning: str


class BrowserDecision(BaseModel):
    """LLM response after each browser screenshot."""
    next_action: BrowserAction | None
    completed: bool
    abandoned: bool
    abandonment_reason: str | None
    friction_observed: list[str]
    comment: str  # first-person one-sentence summary of this step


class AgentResult(BaseModel):
    """Output of one agent, shared by both modes."""
    agent_index: int
    user_type: str
    completed: bool
    abandoned: bool
    abandonment_reason: str | None
    friction_points: list[str]
    comment: str
    target_element: str | None = None  # None in browser mode (multi-step)
    reasoning: str | None = None  # None in browser mode (multi-step)
    steps_taken: int  # always 1 in screenshot mode
    input_tokens: int
    output_tokens: int
    cost: float


class SwarmResult(BaseModel):
    """Full output written to results.json."""
    timestamp: str  # ISO 8601
    mode: Literal["screenshot", "browser"]
    target: str
    task: str
    model: str
    users: int
    completion_rate: float
    margin_of_error: float  # 1.96 * sqrt(p*(1-p)/n)
    user_breakdown: dict[str, float]  # label → completion rate
    friction_points: list[str]  # canonical phrases; semantically equivalent points share the same string
    total_cost: float
    individual_results: list[AgentResult]
