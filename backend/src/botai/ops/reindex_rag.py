"""Operator command: index source artifacts into RAG and backfill the pgvector column.

    python -m botai.ops.reindex_rag --email E [--source-id S] [--vectors-only|--artifacts-only]

Two independent, idempotent, bounded jobs behind one command:

* **artifacts** — `search.rag.reindex_source_artifacts`: reads the artifacts lane C's runner saved
  (`source_artifacts` + `FileArtifactStore` under `--artifact-root`/`BOTAI_RAG_ARTIFACT_ROOT`),
  chunks and embeds their text, and records provenance. The same text is one document
  (`content_hash` dedupe), binaries are skipped with a reason. Re-running only fills the gaps.
* **vectors** — `search.pgvector.backfill_vectors`: derives `embedding_vec` from the canonical JSONB
  value for rows embedded before the pgvector migration; `--max-rows` bounds one pass and the
  command prints how many rows are left so an operator reruns until zero.

Nothing secret is printed: counts, reasons and an estimated cost only. The provider comes from
`BOTAI_RAG_*` exactly like the runtime tools; the feature flag `BOTAI_RAG_ENABLED` gates tool
wiring, not this explicit operator action (the command says so when the flag is off).

Honest limitation: automatic indexing after a source run is **not wired** this round (the runner is
owned by another lane and the phase closes without the inline hook), so this command is the
supported way to index artifacts the runner already saved.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.auth import service
from botai.core.errors import BotaiError
from botai.db.models import ChunkEmbedding, User
from botai.search.embeddings import EmbeddingError, EmbeddingProvider
from botai.search.pgvector import backfill_vectors, state_for
from botai.search.rag import (
    MAX_ARTIFACTS_PER_CALL,
    RagSettings,
    rag_settings,
    reindex_source_artifacts,
)


class ReindexError(Exception):
    """A mistake of the operator, printed without a traceback."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="botai.ops.reindex_rag",
        description="BotAI: проиндексировать артефакты источников в RAG и заполнить pgvector",
    )
    parser.add_argument("--email", required=True, help="почта аккаунта")
    parser.add_argument(
        "--source-id",
        default=None,
        help="только один источник (по умолчанию все источники аккаунта)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_ARTIFACTS_PER_CALL,
        help=f"артефактов на источник за один проход (1..{MAX_ARTIFACTS_PER_CALL})",
    )
    parser.add_argument("--max-sources", type=int, default=50, help="источников за один проход")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=None,
        help="корень FileArtifactStore для storage_ref (по умолчанию BOTAI_RAG_ARTIFACT_ROOT)",
    )
    parser.add_argument("--batch-size", type=int, default=200, help="строк на батч backfill")
    parser.add_argument("--max-rows", type=int, default=5000, help="предел строк за один проход")
    parser.add_argument(
        "--vectors-only", action="store_true", help="только заполнить embedding_vec"
    )
    parser.add_argument(
        "--artifacts-only", action="store_true", help="только индексация артефактов"
    )
    return parser


async def _user_id(sessionmaker: async_sessionmaker[AsyncSession], email: str) -> str:
    async with sessionmaker() as session:
        user = await session.scalar(
            select(User).where(User.email == service.normalize_email(email))
        )
        if user is None:
            raise ReindexError("Пользователь не найден")
        return user.id


def _build_provider(settings: RagSettings) -> EmbeddingProvider:
    try:
        return settings.build_provider()
    except EmbeddingError as exc:
        raise ReindexError(f"Провайдер эмбеддингов не собран: {exc}") from None


async def _reindex_artifacts(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    args: argparse.Namespace,
    settings: RagSettings,
    out: Callable[[str], None],
) -> None:
    provider = _build_provider(settings)
    report = await reindex_source_artifacts(
        sessionmaker,
        user_id=user_id,
        source_id=args.source_id,
        limit=args.limit,
        max_sources=args.max_sources,
        artifact_root=args.artifact_root,
        settings=settings,
        provider=provider,
    )
    out(
        f"Артефакты: документов {report.documents}, чанков {report.chunks}, "
        f"новых векторов {report.embedded}, без изменений {report.deduplicated}, "
        f"пропущено {report.skipped}"
    )
    if report.reasons:
        out("Причины: " + ", ".join(sorted(set(report.reasons))))
    if report.usage.calls or report.usage.texts or report.usage.cost_usd:
        out(
            f"Эмбеддинги: вызовов {report.usage.calls}, текстов {report.usage.texts}, "
            f"стоимость ${report.usage.cost_usd:.4f}"
        )


async def _backfill(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    args: argparse.Namespace,
    out: Callable[[str], None],
) -> None:
    async with sessionmaker() as session:
        state = await state_for(session)
        if not state.available or state.dimension is None:
            out(
                f"pgvector недоступен ({state.reason or 'column_missing'}): "
                "векторы хранятся только в JSONB, заполнять нечего"
            )
            return
        report = await backfill_vectors(
            session,
            user_id=user_id,
            batch_size=max(1, args.batch_size),
            max_rows=max(1, args.max_rows),
        )
        await session.commit()
    out(f"pgvector: доступен, vector({state.dimension}); заполнено embedding_vec: {report.filled}")
    async with sessionmaker() as session:
        remaining = int(
            await session.scalar(
                select(func.count())
                .select_from(ChunkEmbedding)
                .where(
                    ChunkEmbedding.user_id == user_id,
                    ChunkEmbedding.embedding_vec.is_(None),
                )
            )
            or 0
        )
    if remaining:
        out(f"Осталось строк без embedding_vec: {remaining}; повторите с большим --max-rows")


async def run(
    argv: Sequence[str],
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: RagSettings | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = _parser().parse_args(argv)
    resolved = settings or rag_settings()
    user_id = ""
    try:
        user_id = await _user_id(sessionmaker, args.email)
        if not resolved.enabled:
            out(
                "Флаг BOTAI_RAG_ENABLED выключен: инструменты агента не подключают RAG, "
                "но явная команда работает."
            )
        if not args.vectors_only:
            await _reindex_artifacts(
                sessionmaker, user_id=user_id, args=args, settings=resolved, out=out
            )
        if not args.artifacts_only:
            await _backfill(sessionmaker, user_id=user_id, args=args, out=out)
    except (ReindexError, EmbeddingError, BotaiError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    out("Готово.")
    return 0


def main() -> None:
    from botai.db.session import get_sessionmaker

    raise SystemExit(asyncio.run(run(sys.argv[1:], sessionmaker=get_sessionmaker())))


if __name__ == "__main__":
    main()
