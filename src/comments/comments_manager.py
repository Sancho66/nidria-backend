import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.step_comment import StepComment
from src.comments.comments_repository import CommentsRepository
from src.comments.comments_schema import CommentResponse
from src.core.config import get_settings
from src.core.email import send_email, space_link
from src.core.email_templates import new_comment_to_agent, new_comment_to_client
from src.core.enums import ActorType
from src.core.exceptions import ForbiddenError, NotFoundError
from src.core.i18n import (
    resolve_notification_lang_agent,
    resolve_notification_lang_client,
    resolve_step_name_for_notif,
)
from src.external.scoping import get_case_for_external
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

# Anti-burst window: a recipient already notified for THIS thread less
# than this ago is not re-mailed (messages are grouped).
NOTIFY_WINDOW = timedelta(minutes=15)


class CommentsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CommentsRepository(db)

    # --- response shaping ----------------------------------------------------------

    async def _build_responses(
        self,
        comments: list[StepComment],
        viewer_type: ActorType,
        viewer_id: uuid.UUID,
    ) -> list[CommentResponse]:
        agent_ids = [c.author_id for c in comments if c.author_type == ActorType.AGENT.value]
        expat_ids = [c.author_id for c in comments if c.author_type == ActorType.EXPAT.value]
        agent_names = await self.repo.agent_first_names(agent_ids)
        expat_names = await self.repo.expat_labels(expat_ids)
        out: list[CommentResponse] = []
        for c in comments:
            if c.author_type == ActorType.AGENT.value:
                label = agent_names.get(c.author_id, "")
            else:
                label = expat_names.get(c.author_id, "")
            deleted = c.deleted_at is not None
            out.append(
                CommentResponse(
                    id=c.id,
                    author_type=c.author_type,
                    author_label=label,
                    is_mine=(c.author_type == viewer_type.value and c.author_id == viewer_id),
                    body=None if deleted else c.body,
                    edited=c.edited_at is not None,
                    deleted=deleted,
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                )
            )
        return out

    # --- agent face ----------------------------------------------------------------

    async def _resolve_agent(
        self, agent: Agent, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> tuple[ClientCase, uuid.UUID]:
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)  # border 1
        if case is None:
            raise NotFoundError("Case not found.")
        progress = await self.repo.get_progress_in_case(case.id, progress_id)  # thread scoped
        if progress is None:
            raise NotFoundError("Case step not found.")
        return case, progress.template_step_id

    async def list_as_agent(
        self, agent: Agent, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> list[CommentResponse]:
        await self._resolve_agent(agent, case_id, progress_id)
        comments = await self.repo.list_comments(progress_id)
        return await self._build_responses(comments, ActorType.AGENT, agent.id)

    async def create_as_agent(
        self, agent: Agent, case_id: uuid.UUID, progress_id: uuid.UUID, body: str
    ) -> CommentResponse:
        case, template_step_id = await self._resolve_agent(agent, case_id, progress_id)
        comment = self.repo.add_comment(
            case_step_progress_id=progress_id,
            author_type=ActorType.AGENT.value,
            author_id=agent.id,
            body=body,
        )
        await UsageManager(self.db).emit_for_case(
            case, "message.sent", actor_type=ActorType.AGENT, actor_id=agent.id
        )
        await self.db.commit()
        await self.db.refresh(comment)
        await self._notify_after_commit(
            case, progress_id, template_step_id, ActorType.AGENT, agent.first_name
        )
        return (await self._build_responses([comment], ActorType.AGENT, agent.id))[0]

    async def update_as_agent(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        comment_id: uuid.UUID,
        body: str,
    ) -> CommentResponse:
        await self._resolve_agent(agent, case_id, progress_id)
        comment = await self._own_comment(progress_id, comment_id, ActorType.AGENT, agent.id)
        comment.body = body
        comment.edited_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(comment)
        return (await self._build_responses([comment], ActorType.AGENT, agent.id))[0]

    async def delete_as_agent(
        self, agent: Agent, case_id: uuid.UUID, progress_id: uuid.UUID, comment_id: uuid.UUID
    ) -> None:
        await self._resolve_agent(agent, case_id, progress_id)
        comment = await self._own_comment(progress_id, comment_id, ActorType.AGENT, agent.id)
        comment.deleted_at = datetime.now(UTC)
        await self.db.commit()

    # --- external provider face (wave B) -------------------------------------------
    #
    # An external is an AGENT (author_type=AGENT, viewer identity = its id)
    # — so it shares the same thread as the agency, and is_mine works the
    # same. The ONLY difference vs the agent face: the case is resolved by
    # ASSIGNMENT (get_case_for_external → 404), never by agency. No
    # auto-notification (the AGENT→client mail would mis-attribute a
    # provider's message as "your conseiller").

    async def _resolve_external(
        self, external: Agent, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> None:
        case = await get_case_for_external(self.db, external, case_id)  # border: assignment
        if case is None:
            raise NotFoundError("Case not found.")
        progress = await self.repo.get_progress_in_case(case.id, progress_id)  # thread scoped
        if progress is None:
            raise NotFoundError("Case step not found.")

    async def list_as_external(
        self, external: Agent, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> list[CommentResponse]:
        await self._resolve_external(external, case_id, progress_id)
        comments = await self.repo.list_comments(progress_id)
        return await self._build_responses(comments, ActorType.AGENT, external.id)

    async def create_as_external(
        self, external: Agent, case_id: uuid.UUID, progress_id: uuid.UUID, body: str
    ) -> CommentResponse:
        await self._resolve_external(external, case_id, progress_id)
        # Usage tracker needs the case (demo exclusion); the border above
        # already guaranteed assignment-scoped access.
        case = await get_case_for_external(self.db, external, case_id)
        assert case is not None  # _resolve_external just resolved it
        comment = self.repo.add_comment(
            case_step_progress_id=progress_id,
            author_type=ActorType.AGENT.value,
            author_id=external.id,
            body=body,
        )
        await UsageManager(self.db).emit_for_case(
            case, "message.sent", actor_type=ActorType.AGENT, actor_id=external.id
        )
        await self.db.commit()
        await self.db.refresh(comment)
        return (await self._build_responses([comment], ActorType.AGENT, external.id))[0]

    async def update_as_external(
        self,
        external: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        comment_id: uuid.UUID,
        body: str,
    ) -> CommentResponse:
        await self._resolve_external(external, case_id, progress_id)
        comment = await self._own_comment(progress_id, comment_id, ActorType.AGENT, external.id)
        comment.body = body
        comment.edited_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(comment)
        return (await self._build_responses([comment], ActorType.AGENT, external.id))[0]

    async def delete_as_external(
        self, external: Agent, case_id: uuid.UUID, progress_id: uuid.UUID, comment_id: uuid.UUID
    ) -> None:
        await self._resolve_external(external, case_id, progress_id)
        comment = await self._own_comment(progress_id, comment_id, ActorType.AGENT, external.id)
        comment.deleted_at = datetime.now(UTC)
        await self.db.commit()

    # --- expat face ----------------------------------------------------------------

    async def _resolve_expat(
        self, expat: ExpatUser, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> tuple[ClientCase, uuid.UUID]:
        case = await self.repo.get_case_for_expat(expat.id, case_id)  # border: ownership 404
        if case is None:
            raise NotFoundError("Case not found.")
        progress = await self.repo.get_progress_in_case(case.id, progress_id)  # thread scoped
        if progress is None:
            raise NotFoundError("Case step not found.")
        return case, progress.template_step_id

    async def list_as_expat(
        self, expat: ExpatUser, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> list[CommentResponse]:
        await self._resolve_expat(expat, case_id, progress_id)
        comments = await self.repo.list_comments(progress_id)
        return await self._build_responses(comments, ActorType.EXPAT, expat.id)

    async def create_as_expat(
        self, expat: ExpatUser, case_id: uuid.UUID, progress_id: uuid.UUID, body: str
    ) -> CommentResponse:
        case, template_step_id = await self._resolve_expat(expat, case_id, progress_id)
        comment = self.repo.add_comment(
            case_step_progress_id=progress_id,
            author_type=ActorType.EXPAT.value,
            author_id=expat.id,
            body=body,
        )
        await UsageManager(self.db).emit_for_case(
            case, "message.sent", actor_type=ActorType.EXPAT, actor_id=expat.id
        )
        await self.db.commit()
        await self.db.refresh(comment)
        await self._notify_after_commit(case, progress_id, template_step_id, ActorType.EXPAT, None)
        return (await self._build_responses([comment], ActorType.EXPAT, expat.id))[0]

    async def update_as_expat(
        self,
        expat: ExpatUser,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        comment_id: uuid.UUID,
        body: str,
    ) -> CommentResponse:
        await self._resolve_expat(expat, case_id, progress_id)
        comment = await self._own_comment(progress_id, comment_id, ActorType.EXPAT, expat.id)
        comment.body = body
        comment.edited_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(comment)
        return (await self._build_responses([comment], ActorType.EXPAT, expat.id))[0]

    async def delete_as_expat(
        self, expat: ExpatUser, case_id: uuid.UUID, progress_id: uuid.UUID, comment_id: uuid.UUID
    ) -> None:
        await self._resolve_expat(expat, case_id, progress_id)
        comment = await self._own_comment(progress_id, comment_id, ActorType.EXPAT, expat.id)
        comment.deleted_at = datetime.now(UTC)
        await self.db.commit()

    # --- own-comment border (edit/delete only your own) ----------------------------

    async def _own_comment(
        self,
        progress_id: uuid.UUID,
        comment_id: uuid.UUID,
        viewer_type: ActorType,
        viewer_id: uuid.UUID,
    ) -> StepComment:
        comment = await self.repo.get_comment(progress_id, comment_id)
        if comment is None or comment.deleted_at is not None:
            # A deleted comment is gone from the actionable thread.
            raise NotFoundError("Comment not found.")
        if comment.author_type != viewer_type.value or comment.author_id != viewer_id:
            # Each party touches ONLY its own messages — checked against the
            # JWT identity, never the payload.
            raise ForbiddenError("Only the author can modify this comment.")
        return comment

    # --- notification (anti-burst, best-effort, AFTER commit) ----------------------

    async def _notifications_enabled(self, case: ClientCase) -> bool:
        agency = await self.repo.get_agency(case.agency_id)
        settings = (agency.settings if agency else None) or {}
        return bool(settings.get("step_notifications_enabled", True))

    async def _notify_after_commit(
        self,
        case: ClientCase,
        progress_id: uuid.UUID,
        template_step_id: uuid.UUID,
        author_type: ActorType,
        author_first_name: str | None,
    ) -> None:
        """Notify the OTHER party, grouped and best-effort. The comment is
        already committed — a failure here never rolls it back. The
        anti-burst window is keyed on the EFFECTIVE send (last_notified_at),
        posted only after send_email succeeds: a failed mail does not
        suppress the next one."""
        if not await self._notifications_enabled(case):
            return
        recipient_type = ActorType.EXPAT if author_type is ActorType.AGENT else ActorType.AGENT
        step_scalar, step_i18n = await self.repo.get_step_name_and_i18n(template_step_id)
        step_scalar = step_scalar or ""
        settings = get_settings()

        if recipient_type is ActorType.EXPAT:
            _, email, preferred_lang = await self.repo.get_principal_name_email(case)
            if not email:
                return
            agency = await self.repo.get_agency(case.agency_id)
            agency_name = agency.name if agency else "Votre agence"
            # Recipient = CLIENT → preferred_lang, else EN.
            lang = resolve_notification_lang_client(preferred_lang)
            step_name = resolve_step_name_for_notif(step_i18n, step_scalar, lang)
            content = new_comment_to_client(
                agency_name,
                author_first_name or "",
                step_name,
                space_link(settings.frontend_url, "/space", agency.slug if agency else None),
                lang,
            )
        else:
            if case.owner_agent_id is None:
                return
            email = await self.repo.get_agent_email(case.owner_agent_id)
            if not email:
                return
            client_name, _, _ = await self.repo.get_principal_name_email(case)
            # Recipient = AGENT → agency default language, else fr.
            agency = await self.repo.get_agency(case.agency_id)
            lang = resolve_notification_lang_agent(agency.default_language if agency else None)
            step_name = resolve_step_name_for_notif(step_i18n, step_scalar, lang)
            content = new_comment_to_agent(
                client_name or "Votre client",
                step_name,
                f"{settings.frontend_url}/app/cases/{case.id}",
                lang,
            )

        now = datetime.now(UTC)
        existing = await self.repo.get_notification(progress_id, recipient_type.value)
        if existing is not None and (now - existing.last_notified_at) < NOTIFY_WINDOW:
            return  # grouped — recipient already notified recently for this thread

        try:
            await asyncio.to_thread(send_email, email, content.subject, content.text, content.html)
        except Exception:  # noqa: BLE001 — best-effort boundary
            logger.exception("comment notification email failed (best-effort) to=%s", email)
            return  # do NOT record a send → the next message will retry
        await self.repo.upsert_notification(progress_id, recipient_type.value, now)
        await self.db.commit()
