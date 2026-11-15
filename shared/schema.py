"""The Electron contract. Do not change without telling the team (group chat first).

Every seam between Person A, B and C passes one of these models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class Urgency(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class JobStatus(str, Enum):
    new = "new"
    triaged = "triaged"
    offered = "offered"
    accepted = "accepted"
    booked = "booked"
    failed = "failed"


# Legal forward moves. Only messaging/state.py:advance() may change status.
TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.new: {JobStatus.triaged, JobStatus.failed},
    JobStatus.triaged: {JobStatus.offered, JobStatus.failed},
    # offered -> offered is the "NO / timeout, try next tech" loop
    JobStatus.offered: {JobStatus.offered, JobStatus.accepted, JobStatus.failed},
    JobStatus.accepted: {JobStatus.booked, JobStatus.failed},
    JobStatus.booked: set(),
    JobStatus.failed: set(),
}


class Part(BaseModel):
    name: str
    confidence: float = Field(ge=0, le=1)
    # Filled in by find_parts(). Links only, never auto-buy.
    url: Optional[str] = None
    price: Optional[float] = None


class Location(BaseModel):
    lat: float
    lng: float


class Quote(BaseModel):
    low: float
    high: float


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    customer_phone: str
    photo_url: Optional[str] = None
    raw_text: str = ""
    trade: Optional[str] = None
    issue: Optional[str] = None
    urgency: Optional[Urgency] = None
    parts: list[Part] = Field(default_factory=list)
    location: Optional[Location] = None
    status: JobStatus = JobStatus.new
    assigned_tech_id: Optional[str] = None
    quote: Optional[Quote] = None
    # Set by intake() when the photo is unclear: ask this instead of guessing.
    # Not stored in the jobs table.
    follow_up_question: Optional[str] = None
    # Set by intake() when there's a photo: what the vision model sees, for the tech's offer text.
    photo_description: Optional[str] = None
    # Set by agents/job_prep.py after triage: causes, tools, parts plan, owner economics.
    prep: Optional[dict] = None


class Tech(BaseModel):
    id: str
    name: str
    phone: str
    skills: list[str]
    lat: float
    lng: float
    rating: float
    calendar_id: Optional[str] = None
    description: Optional[str] = None  # one line for the owner's view, e.g. "Plumber · water heaters"


class RankedTech(BaseModel):
    """rank_techs() returns a list of these, best first."""
    tech_id: str
    score: float
    reasons: list[str]


class Slot(BaseModel):
    start: datetime
    end: datetime


class OfferResponse(str, Enum):
    yes = "yes"
    no = "no"
    timeout = "timeout"


class Offer(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    tech_id: str
    sent_at: datetime
    response: Optional[OfferResponse] = None  # None = still waiting
