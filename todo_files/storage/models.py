"""SQLAlchemy ORM models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    last_parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tickets: Mapped[list[DBTicket]] = relationship("DBTicket", back_populates="file")


class DBTicket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # local UUID
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_key: Mapped[str | None] = mapped_column(String, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_synced_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    sync_status: Mapped[str] = mapped_column(String, default="clean")

    file: Mapped[File] = relationship("File", back_populates="tickets")
    parent_links: Mapped[list[SubtaskLink]] = relationship(
        "SubtaskLink", foreign_keys="SubtaskLink.child_id", back_populates="child"
    )
    child_links: Mapped[list[SubtaskLink]] = relationship(
        "SubtaskLink", foreign_keys="SubtaskLink.parent_id", back_populates="parent"
    )


class SubtaskLink(Base):
    __tablename__ = "subtasks"

    parent_id: Mapped[str] = mapped_column(
        String, ForeignKey("tickets.id"), primary_key=True
    )
    child_id: Mapped[str] = mapped_column(
        String, ForeignKey("tickets.id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    parent: Mapped[DBTicket] = relationship(
        "DBTicket", foreign_keys=[parent_id], back_populates="child_links"
    )
    child: Mapped[DBTicket] = relationship(
        "DBTicket", foreign_keys=[child_id], back_populates="parent_links"
    )
