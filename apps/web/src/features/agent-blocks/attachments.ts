import type { ViewerAttachment } from "@botai/design";
import { type AttachmentBlock, type AttachmentOut, attachmentContentUrl } from "@/api/v3";

export const viewerFromBlock = (block: AttachmentBlock): ViewerAttachment => ({
  id: block.attachmentId,
  name: block.name,
  mime: block.mime,
  size: block.size,
  kind: block.kind,
  url: attachmentContentUrl(block.attachmentId),
});

export const viewerFromOut = (item: AttachmentOut): ViewerAttachment => ({
  id: item.id,
  name: item.name,
  mime: item.mime,
  size: item.size,
  kind: item.kind,
  url: attachmentContentUrl(item.id),
});
