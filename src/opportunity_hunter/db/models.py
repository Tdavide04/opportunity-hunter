"""Opportunity Hunter persistence mappings. Table and column names match the PCH schema."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opportunity_hunter.db import Base
from opportunity_hunter.domain import CONDITIONS, PRICE_STATUSES, SHIPPING_STATUSES

PROFILE_STATUSES = ("active", "paused", "archived")
QUERY_STATUSES = ("active", "paused")
MARKETPLACES = ("fixture", "subito", "vinted")
MIN_SCAN_INTERVAL_MINUTES = 15
RUN_STATUSES = ("running", "succeeded", "partial", "failed")
ERROR_KINDS = ("transient", "permanent", "internal")
LISTING_ORIGINS = ("acquired", "legacy_seen")
AVAILABILITIES = ("active", "not_seen")
OPPORTUNITY_STATUSES = ("new", "seen", "dismissed", "pursued", "acquired")
NOTIFICATION_STATUSES = ("pending", "sent", "failed", "suppressed")
SUPPRESSION_REASONS = ("baseline", "paused", "dry_run", "legacy_seen", "shadow_mode")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


Money = Numeric(12, 2)


class SearchProfile(Base):
    __tablename__ = "search_profile"
    __table_args__ = (
        CheckConstraint("price_min IS NULL OR price_max IS NULL OR price_min <= price_max", name="price_range"),
        CheckConstraint("price_min >= 0 AND price_max >= 0 AND price_target >= 0", name="price_non_negative"),
        CheckConstraint("score_threshold BETWEEN 0 AND 100", name="score_threshold_range"),
        CheckConstraint("cardinality(keywords) > 0", name="has_keywords"),
        CheckConstraint(_in("status", PROFILE_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text))
    title_only: Mapped[bool] = mapped_column(default=False)
    category: Mapped[str | None] = mapped_column(Text)
    excluded_terms: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    price_min: Mapped[Decimal | None] = mapped_column(Money)
    price_max: Mapped[Decimal | None] = mapped_column(Money)
    price_target: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    listed_since: Mapped[date | None] = mapped_column(Date)
    score_threshold: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    marketplace_queries: Mapped[list["MarketplaceQuery"]] = relationship(
        back_populates="search_profile", order_by="MarketplaceQuery.id"
    )


class MarketplaceQuery(Base):
    __tablename__ = "marketplace_query"
    __table_args__ = (
        CheckConstraint(f"scan_interval_minutes >= {MIN_SCAN_INTERVAL_MINUTES}", name="scan_interval_min"),
        CheckConstraint("max_pages >= 1", name="max_pages_min"),
        CheckConstraint(_in("marketplace", MARKETPLACES), name="marketplace"),
        CheckConstraint(_in("status", QUERY_STATUSES), name="status"),
        Index("ix_marketplace_query_status_last_started_at", "status", "last_started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    search_profile_id: Mapped[int] = mapped_column(ForeignKey("search_profile.id", ondelete="RESTRICT"), index=True)
    marketplace: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    scan_interval_minutes: Mapped[int]
    max_pages: Mapped[int]
    status: Mapped[str] = mapped_column(Text, server_default="active")
    # scheduling bookkeeping, written by the worker (#7)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_run_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    search_profile: Mapped[SearchProfile] = relationship(back_populates="marketplace_queries")


class AcquisitionRun(Base):
    __tablename__ = "acquisition_run"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        CheckConstraint(_in("error_kind", ERROR_KINDS), name="error_kind"),
        # at most one running run per Marketplace Query: a second start fails here and is reported as skipped
        Index(
            "uq_acquisition_run_one_running_per_query",
            "marketplace_query_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_acquisition_run_query_started_at", "marketplace_query_id", text("started_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    marketplace_query_id: Mapped[int] = mapped_column(ForeignKey("marketplace_query.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages: Mapped[int] = mapped_column(default=0)
    listings_seen: Mapped[int] = mapped_column(default=0)
    listings_new: Mapped[int] = mapped_column(default=0)
    listings_updated: Mapped[int] = mapped_column(default=0)
    rejected: Mapped[int] = mapped_column(default=0)
    error_kind: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class Listing(Base):
    __tablename__ = "listing"
    __table_args__ = (
        UniqueConstraint("marketplace", "external_id", name="uq_listing_marketplace_external_id"),
        CheckConstraint(_in("marketplace", MARKETPLACES), name="marketplace"),
        CheckConstraint(_in("price_status", PRICE_STATUSES), name="price_status"),
        CheckConstraint("(price_status = 'priced') = (price_amount IS NOT NULL)", name="amount_iff_priced"),
        CheckConstraint("price_amount >= 0", name="amount_non_negative"),
        CheckConstraint("price_amount IS NULL OR currency IS NOT NULL", name="currency_with_amount"),
        CheckConstraint(_in("condition", CONDITIONS), name="condition"),
        CheckConstraint(_in("origin", LISTING_ORIGINS), name="origin"),
        CheckConstraint(_in("availability", AVAILABILITIES), name="availability"),
        # a Legacy Seen Listing may know only marketplace + external_id; acquired ones need the minimum data
        CheckConstraint(
            "origin = 'legacy_seen' OR (title IS NOT NULL AND url IS NOT NULL AND price_status IS NOT NULL)",
            name="acquired_has_minimum",
        ),
        CheckConstraint(_in("shipping_status", SHIPPING_STATUSES), name="shipping_status"),
        CheckConstraint("(shipping_status = 'known') = (shipping_amount IS NOT NULL)", name="shipping_amount_iff_known"),
        CheckConstraint("shipping_amount >= 0 AND fees_amount >= 0", name="shipping_fees_non_negative"),
        Index("ix_listing_marketplace_id", "marketplace", "id"),
        Index("ix_listing_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    marketplace: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_amount: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))
    price_status: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    images: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    location: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    seller_external_id: Mapped[str | None] = mapped_column(Text)
    seller_name: Mapped[str | None] = mapped_column(Text)
    condition: Mapped[str | None] = mapped_column(Text)
    condition_raw: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipping_status: Mapped[str] = mapped_column(Text, server_default="unknown")
    shipping_amount: Mapped[Decimal | None] = mapped_column(Money)
    fees_amount: Mapped[Decimal | None] = mapped_column(Money)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("acquisition_run.id", ondelete="SET NULL"))
    availability: Mapped[str] = mapped_column(Text, server_default="active")
    origin: Mapped[str] = mapped_column(Text, server_default="acquired")
    content_hash: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Match(Base):
    """The judgement on a Listing for a Search Profile; at most one per pair, re-evaluated when the Listing changes."""

    __tablename__ = "match"
    __table_args__ = (
        UniqueConstraint("search_profile_id", "listing_id", name="uq_match_profile_listing"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        CheckConstraint("matched OR score IS NULL", name="score_only_when_matched"),
        Index("ix_match_score", "score"),
        Index("ix_match_listing_id", "listing_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    search_profile_id: Mapped[int] = mapped_column(ForeignKey("search_profile.id", ondelete="RESTRICT"))
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"))
    matched: Mapped[bool]
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB)
    score: Mapped[int | None] = mapped_column(SmallInteger)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scoring_version: Mapped[int] = mapped_column(SmallInteger)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    search_profile: Mapped[SearchProfile] = relationship()
    listing: Mapped["Listing"] = relationship()
    opportunity: Mapped["Opportunity | None"] = relationship(back_populates="match")


class Opportunity(Base):
    """A Match whose Score reached the Score Threshold; the user decides on it."""

    __tablename__ = "opportunity"
    __table_args__ = (
        CheckConstraint(_in("status", OPPORTUNITY_STATUSES), name="status"),
        Index("ix_opportunity_status_created_at", "status", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(Text, server_default="new")
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    match: Mapped[Match] = relationship(back_populates="opportunity")


class Notification(Base):
    """Outbox row: written with the Opportunity, delivered after commit, never twice per (Opportunity, Channel, reason)."""

    __tablename__ = "notification"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "channel", "reason", name="uq_notification_opportunity_channel_reason"),
        CheckConstraint(_in("status", NOTIFICATION_STATUSES), name="status"),
        CheckConstraint(_in("suppressed_reason", SUPPRESSION_REASONS), name="suppressed_reason"),
        CheckConstraint("(status = 'suppressed') = (suppressed_reason IS NOT NULL)", name="reason_iff_suppressed"),
        Index("ix_notification_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    suppressed_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    opportunity: Mapped[Opportunity] = relationship()


class AppSetting(Base):
    """Runtime switches changed from the dashboard (e.g. notifications_paused, dry_run). Never secrets."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class ListingPriceHistory(Base):
    """One row per observed change of price or Price Status (and the first observation)."""

    __tablename__ = "listing_price_history"
    __table_args__ = (
        CheckConstraint(_in("price_status", PRICE_STATUSES), name="price_status"),
        CheckConstraint("(price_status = 'priced') = (price_amount IS NOT NULL)", name="amount_iff_priced"),
        Index("ix_listing_price_history_listing_observed", "listing_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id", ondelete="CASCADE"))
    price_amount: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))
    price_status: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    listing: Mapped["Listing"] = relationship()
