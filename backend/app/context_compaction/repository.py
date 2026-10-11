from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256
from itertools import pairwise
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.context_compaction.contracts import (
    ActivationResult,
    CompactionCandidate,
    CompactionSourceItem,
    ContextSummary,
    GenerationSnapshot,
    SourceMessage,
)
from app.storage.models import AgentContextCompaction, AgentMessage, AgentSession


class RepositoryNotFoundError(RuntimeError):
    """会话不存在或调用方无权访问时使用的统一错误。"""

    code = "context_compaction_repository_not_found"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_digest(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def compute_source_digest(
    previous: AgentContextCompaction | None,
    manifest: Sequence[CompactionSourceItem],
) -> str:
    previous_summary_digest = None
    previous_compaction_id = None
    if previous is not None:
        previous_compaction_id = previous.compaction_id
        previous_summary_digest = sha256(canonical_json(previous.summary_payload)).hexdigest()
    payload = {
        "previous_compaction_id": previous_compaction_id,
        "previous_summary_digest": previous_summary_digest,
        "source_manifest": [item.model_dump(mode="json") for item in manifest],
    }
    return sha256(canonical_json(payload)).hexdigest()


class ContextCompactionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active(
        self,
        tenant_id: str,
        user_id: str,
        session_key: str,
    ) -> AgentContextCompaction | None:
        return self._session.scalar(
            select(AgentContextCompaction)
            .join(
                AgentSession,
                and_(
                    AgentSession.tenant_id == AgentContextCompaction.tenant_id,
                    AgentSession.user_id == AgentContextCompaction.user_id,
                    AgentSession.session_key == AgentContextCompaction.session_key,
                    AgentSession.active_context_compaction_id
                    == AgentContextCompaction.compaction_id,
                ),
            )
            .where(
                AgentSession.tenant_id == tenant_id,
                AgentSession.user_id == user_id,
                AgentSession.session_key == session_key,
                AgentContextCompaction.tenant_id == tenant_id,
                AgentContextCompaction.user_id == user_id,
                AgentContextCompaction.session_key == session_key,
            )
        )

    def list_context_tail(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        after_sequence_no: int,
        exclude_request_id: str,
    ) -> tuple[AgentMessage, ...]:
        return tuple(
            self._session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.tenant_id == tenant_id,
                    AgentMessage.user_id == user_id,
                    AgentMessage.session_key == session_key,
                    AgentMessage.sequence_no > after_sequence_no,
                    AgentMessage.request_id != exclude_request_id,
                    AgentMessage.status == "completed",
                    AgentMessage.deleted_at.is_(None),
                    AgentMessage.role.in_(("user", "assistant")),
                )
                .order_by(AgentMessage.sequence_no.asc())
            )
        )

    def load_generation_snapshot(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        exclude_request_id: str,
    ) -> GenerationSnapshot | None:
        owner = self._session.scalar(
            select(AgentSession).where(
                AgentSession.tenant_id == tenant_id,
                AgentSession.user_id == user_id,
                AgentSession.session_key == session_key,
            )
        )
        if owner is None:
            return None

        active = self.get_active(tenant_id, user_id, session_key)
        if owner.active_context_compaction_id is not None and active is None:
            return None
        boundary = active.summary_through_sequence_no if active is not None else 0
        tail = self.list_context_tail(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            after_sequence_no=boundary,
            exclude_request_id=exclude_request_id,
        )
        complete = self._complete_turn_messages(tail)
        if not complete:
            return None

        previous_summary = None
        if active is not None:
            try:
                previous_summary = ContextSummary.model_validate(active.summary_payload)
            except ValidationError:
                return None
        observed = complete[-1]
        return GenerationSnapshot(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            expected_active_compaction_id=active.compaction_id if active else None,
            previous_summary=previous_summary,
            source_messages=tuple(self._to_source_message(message) for message in complete),
            observed_through_message_id=observed.message_id,
            observed_through_sequence_no=observed.sequence_no,
        )

    def activate_if_current(
        self,
        candidate: CompactionCandidate,
        expected_active_compaction_id: str | None,
    ) -> ActivationResult:
        owner = self._session.scalar(
            select(AgentSession)
            .where(
                AgentSession.tenant_id == candidate.tenant_id,
                AgentSession.user_id == candidate.user_id,
                AgentSession.session_key == candidate.session_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise RepositoryNotFoundError("session_not_found")
        if owner.active_context_compaction_id != expected_active_compaction_id:
            return self._discarded("active_changed")
        if candidate.previous_compaction_id != expected_active_compaction_id:
            return self._discarded("source_changed")

        previous = self._load_previous(candidate, expected_active_compaction_id)
        if expected_active_compaction_id is not None and previous is None:
            return self._discarded("source_changed")

        messages = self._reload_manifest_messages(candidate)
        if messages is None or not self._candidate_boundaries_are_valid(candidate, messages):
            return self._discarded("source_changed")
        if compute_source_digest(previous, candidate.source_manifest) != candidate.source_digest:
            return self._discarded("source_changed")

        version = previous.version + 1 if previous is not None else 1
        record = AgentContextCompaction(
            compaction_id=f"cmp_{uuid4().hex}",
            tenant_id=candidate.tenant_id,
            user_id=candidate.user_id,
            session_key=candidate.session_key,
            version=version,
            previous_compaction_id=candidate.previous_compaction_id,
            summary_through_message_id=candidate.summary_through_message_id,
            observed_through_message_id=candidate.observed_through_message_id,
            summary_through_sequence_no=candidate.summary_through_sequence_no,
            observed_through_sequence_no=candidate.observed_through_sequence_no,
            summary_payload=candidate.summary.model_dump(mode="json"),
            source_manifest=[item.model_dump(mode="json") for item in candidate.source_manifest],
            source_digest=candidate.source_digest,
            source_tokens=candidate.source_tokens,
            summary_tokens=candidate.summary_tokens,
            trigger=candidate.trigger,
            model=candidate.model,
            prompt_version=candidate.prompt_version,
            policy_version=candidate.policy_version,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError as error:
            if (
                self._is_source_identity_conflict(error)
                and self._find_exact_duplicate(candidate) is not None
            ):
                return self._discarded("duplicate")
            raise

        owner.active_context_compaction_id = record.compaction_id
        self._session.flush()
        return ActivationResult(
            activated=True,
            reason="activated",
            compaction_id=record.compaction_id,
            version=record.version,
        )

    def _load_previous(
        self,
        candidate: CompactionCandidate,
        expected_active_compaction_id: str | None,
    ) -> AgentContextCompaction | None:
        if expected_active_compaction_id is None:
            return None
        return self._session.scalar(
            select(AgentContextCompaction).where(
                AgentContextCompaction.compaction_id == expected_active_compaction_id,
                AgentContextCompaction.tenant_id == candidate.tenant_id,
                AgentContextCompaction.user_id == candidate.user_id,
                AgentContextCompaction.session_key == candidate.session_key,
            )
        )

    def _reload_manifest_messages(
        self,
        candidate: CompactionCandidate,
    ) -> tuple[AgentMessage, ...] | None:
        manifest = candidate.source_manifest
        if not manifest or not self._manifest_has_complete_turns(manifest):
            return None
        message_ids = [item.message_id for item in manifest]
        if len(message_ids) != len(set(message_ids)):
            return None
        records = tuple(
            self._session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.tenant_id == candidate.tenant_id,
                    AgentMessage.user_id == candidate.user_id,
                    AgentMessage.session_key == candidate.session_key,
                    AgentMessage.message_id.in_(message_ids),
                )
                .order_by(AgentMessage.sequence_no.asc())
            )
        )
        if len(records) != len(manifest):
            return None
        for item, message in zip(manifest, records, strict=True):
            if (
                item.message_id != message.message_id
                or item.role != message.role
                or item.sequence_no != message.sequence_no
                or message.status != "completed"
                or message.deleted_at is not None
                or content_digest(message.content) != item.content_digest
            ):
                return None
        if not self._messages_are_complete_turns(records):
            return None
        return records

    def _candidate_boundaries_are_valid(
        self,
        candidate: CompactionCandidate,
        messages: tuple[AgentMessage, ...],
    ) -> bool:
        summary = messages[-1]
        if (
            summary.role != "assistant"
            or candidate.summary_through_message_id != summary.message_id
            or candidate.summary_through_sequence_no != summary.sequence_no
            or candidate.observed_through_sequence_no < summary.sequence_no
        ):
            return False
        observed = self._session.scalar(
            select(AgentMessage).where(
                AgentMessage.tenant_id == candidate.tenant_id,
                AgentMessage.user_id == candidate.user_id,
                AgentMessage.session_key == candidate.session_key,
                AgentMessage.message_id == candidate.observed_through_message_id,
                AgentMessage.sequence_no == candidate.observed_through_sequence_no,
                AgentMessage.role == "assistant",
                AgentMessage.status == "completed",
                AgentMessage.deleted_at.is_(None),
            )
        )
        return observed is not None

    def _find_exact_duplicate(
        self,
        candidate: CompactionCandidate,
    ) -> AgentContextCompaction | None:
        previous_clause = (
            AgentContextCompaction.previous_compaction_id.is_(None)
            if candidate.previous_compaction_id is None
            else AgentContextCompaction.previous_compaction_id
            == candidate.previous_compaction_id
        )
        return self._session.scalar(
            select(AgentContextCompaction).where(
                AgentContextCompaction.tenant_id == candidate.tenant_id,
                AgentContextCompaction.user_id == candidate.user_id,
                AgentContextCompaction.session_key == candidate.session_key,
                previous_clause,
                AgentContextCompaction.summary_through_sequence_no
                == candidate.summary_through_sequence_no,
                AgentContextCompaction.source_digest == candidate.source_digest,
                AgentContextCompaction.prompt_version == candidate.prompt_version,
            )
        )

    @staticmethod
    def _is_source_identity_conflict(error: IntegrityError) -> bool:
        constraint_name = getattr(getattr(error, "orig", None), "diag", None)
        if constraint_name is not None:
            constraint_name = getattr(constraint_name, "constraint_name", None)
        return (
            constraint_name == "ux_agent_context_compactions_source_identity"
            or "ux_agent_context_compactions_source_identity" in str(error.orig)
        )

    @staticmethod
    def _manifest_has_complete_turns(
        manifest: Sequence[CompactionSourceItem],
    ) -> bool:
        if len(manifest) % 2:
            return False
        if any(
            current.sequence_no >= following.sequence_no
            for current, following in pairwise(manifest)
        ):
            return False
        return all(
            user.role == "user"
            and assistant.role == "assistant"
            and assistant.sequence_no == user.sequence_no + 1
            for user, assistant in zip(manifest[::2], manifest[1::2], strict=True)
        )

    @staticmethod
    def _messages_are_complete_turns(messages: Sequence[AgentMessage]) -> bool:
        return all(
            user.role == "user"
            and assistant.role == "assistant"
            and assistant.sequence_no == user.sequence_no + 1
            and user.request_id == assistant.request_id
            and user.run_id == assistant.run_id
            for user, assistant in zip(messages[::2], messages[1::2], strict=True)
        )

    @classmethod
    def _complete_turn_messages(
        cls,
        messages: Sequence[AgentMessage],
    ) -> tuple[AgentMessage, ...]:
        complete: list[AgentMessage] = []
        index = 0
        while index + 1 < len(messages):
            pair = messages[index : index + 2]
            if cls._messages_are_complete_turns(pair):
                complete.extend(pair)
                index += 2
            else:
                index += 1
        return tuple(complete)

    @staticmethod
    def _to_source_message(message: AgentMessage) -> SourceMessage:
        return SourceMessage(
            message_id=message.message_id,
            request_id=message.request_id,
            run_id=message.run_id,
            role=message.role,
            content=message.content,
            sequence_no=message.sequence_no,
        )

    @staticmethod
    def _discarded(reason: str) -> ActivationResult:
        return ActivationResult(
            activated=False,
            reason=reason,
            compaction_id=None,
            version=None,
        )
