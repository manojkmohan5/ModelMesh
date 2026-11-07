"""Durable goal intent and per-turn provenance; agent execution remains native."""

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class Goal(Base):
    __tablename__ = "modelmesh_goals"
    __table_args__ = (Index("modelmesh_goal_dispatch", "status", "checked_at"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("modelmesh_sessions.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("modelmesh_users.id"))
    auth_version: Mapped[int] = mapped_column(Integer)
    source_turn_id: Mapped[str | None] = mapped_column(ForeignKey("modelmesh_turns.id"), nullable=True)
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="active")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    model: Mapped[str] = mapped_column(String(200))
    # Unused since connected accounts were removed; existing databases still require values.
    connections: Mapped[str] = mapped_column(Text, default="[]")
    grants_key: Mapped[str] = mapped_column(String(64))
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, default=0)
    blocker: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    checked_at: Mapped[str] = mapped_column(String(40), default=now)


class GoalTurn(Base):
    __tablename__ = "modelmesh_goal_turns"
    turn_id: Mapped[str] = mapped_column(ForeignKey("modelmesh_turns.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("modelmesh_goals.session_id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    generated: Mapped[bool] = mapped_column(Boolean, default=False)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence: Mapped[str] = mapped_column(Text, default="")


class GoalReceipt(Base):
    __tablename__ = "modelmesh_goal_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("modelmesh_sessions.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(Text)
