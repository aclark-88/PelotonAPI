"""SQLAlchemy schema for the Coremont Signal Engine.

Manager-centred design: many fund vehicles and filing events roll up to a single
advisory platform so signals and scores are computed at the manager level.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Manager(Base):
    """An advisory firm / platform — the unit we score and rank."""

    __tablename__ = "managers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(512))
    normalized_name: Mapped[str] = mapped_column(String(512), index=True, unique=True)
    sec_iard_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    cik: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    website: Mapped[Optional[str]] = mapped_column(String(512))
    hq_city: Mapped[Optional[str]] = mapped_column(String(128))
    hq_state: Mapped[Optional[str]] = mapped_column(String(64))
    country: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")
    # Stored as JSON list of strategy tag strings.
    strategy_tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Cached scoring outputs for fast ranking (refreshed by the signal job).
    total_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    tier: Mapped[int] = mapped_column(Integer, default=4, index=True)
    last_signal_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    vehicles: Mapped[list["FundVehicle"]] = relationship(
        back_populates="manager", cascade="all, delete-orphan"
    )
    filings: Mapped[list["Filing"]] = relationship(
        back_populates="manager", cascade="all, delete-orphan"
    )
    signals: Mapped[list["Signal"]] = relationship(
        back_populates="manager", cascade="all, delete-orphan"
    )
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="manager", cascade="all, delete-orphan"
    )
    research_notes: Mapped[list["ResearchNote"]] = relationship(
        back_populates="manager", cascade="all, delete-orphan"
    )


class FundVehicle(Base):
    """A private fund / offering vehicle under a manager."""

    __tablename__ = "fund_vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("managers.id"), index=True)
    legal_name: Mapped[str] = mapped_column(String(512))
    normalized_name: Mapped[str] = mapped_column(String(512), index=True)
    vehicle_type: Mapped[Optional[str]] = mapped_column(String(64))
    domicile: Mapped[Optional[str]] = mapped_column(String(64))
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)
    is_feeder: Mapped[bool] = mapped_column(Boolean, default=False)
    is_offshore: Mapped[bool] = mapped_column(Boolean, default=False)
    launch_date_est: Mapped[Optional[dt.date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="active")

    manager: Mapped[Manager] = relationship(back_populates="vehicles")
    filings: Mapped[list["Filing"]] = relationship(back_populates="vehicle")

    __table_args__ = (
        UniqueConstraint("manager_id", "normalized_name", name="uq_vehicle_per_manager"),
    )


class Filing(Base):
    """A filing-level event (Form D / D/A today, adviser forms later)."""

    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("managers.id"), index=True)
    fund_vehicle_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("fund_vehicles.id"), index=True
    )
    filing_type: Mapped[str] = mapped_column(String(32))  # e.g. "D"
    filing_subtype: Mapped[Optional[str]] = mapped_column(String(32))  # "new" | "amendment"
    sec_accession_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    filing_date: Mapped[Optional[dt.date]] = mapped_column(Date, index=True)
    first_sale_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    offering_amount: Mapped[Optional[float]] = mapped_column(Float)
    amount_sold: Mapped[Optional[float]] = mapped_column(Float)
    remaining_amount: Mapped[Optional[float]] = mapped_column(Float)
    exemption_claimed: Mapped[Optional[str]] = mapped_column(String(128))
    raw_payload_json: Mapped[dict] = mapped_column(JSON, default=dict)

    manager: Mapped[Manager] = relationship(back_populates="filings")
    vehicle: Mapped[Optional[FundVehicle]] = relationship(back_populates="filings")


class Signal(Base):
    """A prospecting event derived from filings, with a scored breakdown."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("managers.id"), index=True)
    fund_vehicle_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("fund_vehicles.id"), index=True
    )
    signal_type: Mapped[str] = mapped_column(String(48), index=True)
    signal_date: Mapped[Optional[dt.date]] = mapped_column(Date, index=True)
    strength: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1 confidence
    reason: Mapped[str] = mapped_column(Text)
    # Component subscores mirror the four scoring buckets (manager-level aggregate).
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0)   # event strength (0-30)
    strategy_fit_score: Mapped[float] = mapped_column(Float, default=0.0)  # (0-30)
    complexity_score: Mapped[float] = mapped_column(Float, default=0.0)    # (0-25)
    reachability_score: Mapped[float] = mapped_column(Float, default=0.0)  # (0-15)
    total_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    manager: Mapped[Manager] = relationship(back_populates="signals")


class Contact(Base):
    """A person tied to a manager — typically a likely Clarion buyer persona."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("managers.id"), index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(256))
    title: Mapped[Optional[str]] = mapped_column(String(256))
    persona: Mapped[Optional[str]] = mapped_column(String(64))  # coo / ops / risk / finance / treasury
    email: Mapped[Optional[str]] = mapped_column(String(256))
    source_url: Mapped[Optional[str]] = mapped_column(String(512))

    manager: Mapped[Manager] = relationship(back_populates="contacts")


class ResearchNote(Base):
    """Structured enrichment summaries + source URLs for account planning."""

    __tablename__ = "research_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("managers.id"), index=True)
    summary: Mapped[str] = mapped_column(Text)
    source_url: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    manager: Mapped[Manager] = relationship(back_populates="research_notes")
