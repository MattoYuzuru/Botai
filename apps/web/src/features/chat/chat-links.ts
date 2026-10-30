import type { CitationGroup, LinkResolver } from "@botai/design";
import { useRouter } from "@tanstack/react-router";
import { useCallback, useMemo } from "react";
import type { BlockContext } from "@/features/agent-blocks/context";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { useAttachmentViewer } from "./attachment-viewer-host";
import { type ConnectionRef, groupCitations, type ResolvedInternal, type TextSource } from "./citations";
import { internalHref, internalIcon, internalLabel, parseInternalLink } from "./internal-links";

export interface ChatLinks {
  resolveInternal: (url: string) => ResolvedInternal | null;
  resolveLink: LinkResolver;
  groupSources: (sources: readonly TextSource[]) => readonly CitationGroup[];
  openAttachment: NonNullable<BlockContext["openAttachment"]>;
}

/**
 * Ссылки ответа агента внутри приложения: `botai://threads/<id>` открывает тред, `botai://attachments/<id>` — вьюер,
 * документы и подключения ведут в «Учёбу». Цитаты группируются по источнику: название берётся из подключений вузов.
 */
export function useChatLinks(): ChatLinks {
  const router = useRouter();
  const directory = useSpaceDirectory();
  const openAttachment = useAttachmentViewer((state) => state.open);

  const resolveInternal = useCallback(
    (url: string): ResolvedInternal | null => {
      const link = parseInternalLink(url);
      if (!link) return null;
      const href = internalHref(link);
      return {
        href,
        label: internalLabel(link),
        icon: internalIcon(link),
        onOpen: link.kind === "attachment" ? () => openAttachment(link.id) : () => router.history.push(href),
      };
    },
    [router, openAttachment],
  );

  const resolveLink = useCallback<LinkResolver>(
    (url) => {
      const resolved = resolveInternal(url);
      return resolved ? { href: resolved.href, label: resolved.label, onOpen: resolved.onOpen } : null;
    },
    [resolveInternal],
  );

  const connections = useMemo<ConnectionRef[]>(
    () =>
      directory.spaces.flatMap((space) =>
        space.connections.map((connection) => ({
          id: connection.id,
          title: connection.title,
          origin: connection.origin,
          spaceTitle: space.shortTitle,
        })),
      ),
    [directory.spaces],
  );
  const groupSources = useCallback(
    (sources: readonly TextSource[]) => groupCitations(sources, connections, resolveInternal),
    [connections, resolveInternal],
  );

  return { resolveInternal, resolveLink, groupSources, openAttachment };
}
