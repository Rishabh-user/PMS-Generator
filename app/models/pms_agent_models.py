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


# ── Chat session persistence models ────────────────────────────────
# These are used by the GET/PUT/PATCH/DELETE /api/pms-agent/sessions
# endpoints, backed by the pms_agent_sessions PostgreSQL table.

class AgentSessionSummary(BaseModel):
    id: str
    title: str
    message_count: int = 0
    last_message_preview: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentSessionDetail(AgentSessionSummary):
    blocks: list[dict] = Field(
        default_factory=list,
        description="The full MessageBlock array as saved by the frontend; "
                    "opaque server-side JSON (render logic lives on the client).",
    )


class UpsertAgentSessionRequest(BaseModel):
    title: str = Field(..., description="Session title (auto-derived client-side)")
    blocks: list[dict] = Field(..., description="Full MessageBlock array")
    message_count: int = 0
    last_message_preview: str = ""


class RenameAgentSessionRequest(BaseModel):
    title: str = Field(..., description="New title (trimmed, non-empty)")


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


class FieldSuggestion(BaseModel):
    """When a user-provided value doesn't match any valid option, the agent
    returns this so the frontend can render 'did you mean …?' chips."""
    field: Literal["rating", "material", "corrosion_allowance", "service"]
    provided: str = Field(..., description="What the user actually typed")
    suggestions: list[str] = Field(
        default_factory=list,
        description="Valid values the user might have meant (closest first)",
    )


class SlotState(BaseModel):
    """Snapshot of the four slot-filling fields for generating a PMS.
    The frontend uses this to render progress pills and to decide
    whether a Download flow can start.

    Required: rating, material, corrosion_allowance, service.
    All four must be filled before matches are released — previously
    service was optional and silently defaulted to "General" which was
    confusing in the generated PMS. Making it explicit forces the
    agent to ask and the user to pick.
    """
    rating: Optional[str] = None
    material: Optional[str] = None
    corrosion_allowance: Optional[str] = None
    service: Optional[str] = None
    missing: list[str] = Field(
        default_factory=list,
        description="Required fields the user hasn't supplied yet (subset of "
                    "['rating','material','corrosion_allowance','service']).",
    )
    complete: bool = Field(
        default=False,
        description="True when all four required fields are filled.",
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
    slots: SlotState = Field(
        default_factory=SlotState,
        description="Slot-filling state — which of Rating / Material / CA have "
                    "been supplied, which are missing, and whether the trio is complete.",
    )
    field_suggestions: list[FieldSuggestion] = Field(
        default_factory=list,
        description="For any user-supplied value that didn't match valid options, "
                    "the closest valid values. Frontend renders these as 'did you "
                    "mean …?' chips.",
    )
    available_values: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Canonical valid values per field (for dropdown / autocomplete). "
                    "Keys: 'rating', 'material', 'corrosion_allowance'.",
    )
    allow_bulk_download: bool = Field(
        default=False,
        description="True when matched_classes contains concrete class rows the user "
                    "can multi-select and ZIP-download. False for empty results or "
                    "pure info queries.",
    )
