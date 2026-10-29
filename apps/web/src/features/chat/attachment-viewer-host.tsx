import { AttachmentViewer, type ViewerAttachment } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { create } from "zustand";
import { type AttachmentOut, v3 } from "@/api/v3";
import { viewerFromOut } from "@/features/agent-blocks/attachments";

/** Какое вложение открыто во вьюере. Открыть его можно из сообщения, из чипа поля и по ссылке `botai://attachments/…`. */
export const useAttachmentViewer = create<{
  openId: string | null;
  open: (id: string) => void;
  close: () => void;
}>((set) => ({
  openId: null,
  open: (id) => set({ openId: id }),
  close: () => set({ openId: null }),
}));

/**
 * Вьюер вложений экрана чата. Вложения ленты и поля известны заранее; вложение из внутренней ссылки,
 * которого в ленте нет, подтягивается по `GET /attachments/{id}`.
 */
export function AttachmentViewerHost({ known }: { known: readonly ViewerAttachment[] }) {
  const openId = useAttachmentViewer((state) => state.openId);
  const close = useAttachmentViewer((state) => state.close);
  const isKnown = openId !== null && known.some((item) => item.id === openId);
  const fetched = useQuery({
    queryKey: ["attachments", openId],
    queryFn: () => v3<AttachmentOut>("GET", `/api/v1/attachments/${encodeURIComponent(openId ?? "")}`),
    enabled: openId !== null && !isKnown,
    staleTime: 60_000,
  });
  const items = useMemo(
    () => (fetched.data ? [...known, viewerFromOut(fetched.data)] : known),
    [known, fetched.data],
  );
  return (
    <AttachmentViewer
      items={items}
      openId={openId !== null && items.some((item) => item.id === openId) ? openId : null}
      onOpenChange={(id) => (id === null ? close() : useAttachmentViewer.getState().open(id))}
    />
  );
}
