"""
Pydantic models for the PMS Agent endpoint.

The agent takes a natural-language prompt and returns matched piping classes
plus a suggested action (e.g. open the generator pre-filled with the class).
"""
from typing import Optional, Literal
from pydantic import BaseModel, Field


class AgentHistoryTurn(BaseModel):
    """One prior turn of the conversation, sent back to give Claude multi-turn context."""
    role: Literal["user", "assistant"]
    content: str = Field(..., description="Message text (plain or markdown)")


class PMSAgentRequest(BaseModel):
    prompt: str = Field(..., description="User's natural-language query")
    history: list[AgentHistoryTurn] = Field(
        default_factory=list,
        description=(
            "Prior conversation turns in order (oldest first). The frontend "
            "should cap this to the last ~10 turns to keep latency low."
        ),
    )


class ClassMatch(BaseModel):
    """A piping class that matched the user's query."""
    piping_class: str
    rating: str
    material: str
    corrosion_allowance: str
    pt_preview: str = Field(
        ...,
        description="Short human-readable P-T summary, e.g. '19.6 barg @ 38°C · 10.2 barg @ 300°C'",
    )
    score: float = Field(
        default=1.0,
        description="Match confidence 0..1 — higher means a better fit",
    )


class ParsedQuery(BaseModel):
    """What the agent understood from the prompt."""
    piping_class: Optional[str] = None
    rating: Optional[str] = None
    material: Optional[str] = None
    corrosion_allowance: Optional[str] = None
    service: Optional[str] = None
    design_temp_c: Optional[float] = None
    design_pressure_barg: Optional[float] = None
    intent: Literal["generate", "list", "info", "unknown"] = "unknown"


class AgentAction(BaseModel):
    """Structured action the frontend can execute on behalf of the user."""
    type: Literal["open_generator", "list_only", "none"] = "none"
    piping_class: Optional[str] = None
    material: Optional[str] = None
    corrosion_allowance: Optional[str] = None
    service: Optional[str] = None
    design_pressure_barg: Optional[float] = None
    design_temp_c: Optional[float] = None


class PMSAgentResponse(BaseModel):
    reply: str = Field(..., description="Natural-language response shown to the user")
    interpreted: ParsedQuery
    matched_classes: list[ClassMatch] = Field(default_factory=list)
    suggested_action: AgentAction = Field(default_factory=AgentAction)
