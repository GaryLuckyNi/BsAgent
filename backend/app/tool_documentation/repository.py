from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.storage.models import ToolDocumentationRecord as Record


def timestamp(now=None):
    return (now or datetime.now(UTC)).timestamp()


class DocumentationRepository:
    def __init__(self, session_factory):
        self.sessions = session_factory

    def ready(self, key, *, now=None):
        with self.sessions() as session:
            row = session.get(Record, key)
            return row.payload if row and row.status == "ready" and row.expires_at > timestamp(now) else None

    def reserve(self, key, manifest, domains, *, now=None):
        point = timestamp(now)
        with self.sessions() as session:
            session.add(Record(key=key, status="queued", manifest=manifest, domains=domains, expires_at=point + 300))
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
            changed = session.execute(update(Record).where(Record.key == key, Record.expires_at <= point).values(
                status="queued", expires_at=point + 300, lease=None, manifest=manifest, domains=domains))
            session.commit()
            return changed.rowcount == 1

    def claim(self, key, *, now=None):
        point, lease = timestamp(now), uuid4().hex
        with self.sessions() as session:
            changed = session.execute(update(Record).where(Record.key == key,
                or_(Record.status == "queued", (Record.status == "running") & (Record.expires_at <= point))).values(
                    status="running", lease=lease, expires_at=point + 300))
            if changed.rowcount != 1:
                session.rollback()
                return None
            row = session.scalar(select(Record).where(Record.key == key))
            job = {"lease": lease, "manifest": row.manifest, "domains": row.domains}
            session.commit()
            return job

    def finish(self, key, lease, payload, *, now=None):
        with self.sessions() as session:
            changed = session.execute(update(Record).where(Record.key == key, Record.status == "running",
                Record.lease == lease, Record.expires_at > timestamp(now)).values(status="ready", payload=payload,
                    lease=None, expires_at=timestamp(now) + 7 * 86400))
            session.commit()
            return changed.rowcount == 1

    def fail(self, key, lease=None, *, now=None):
        with self.sessions() as session:
            query = update(Record).where(Record.key == key)
            query = query.where(Record.lease == lease, Record.status == "running") if lease else query.where(Record.status == "queued")
            session.execute(query.values(status="failed", lease=None, expires_at=timestamp(now) + 3600))
            session.commit()
