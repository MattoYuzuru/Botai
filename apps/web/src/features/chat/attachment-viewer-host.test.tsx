import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AttachmentViewerHost, useAttachmentViewer } from "./attachment-viewer-host";

describe("вьюер вложений", () => {
  it("уход с экрана чата закрывает его: при возвращении он не всплывает сам", () => {
    const { unmount } = render(
      <QueryClientProvider client={new QueryClient()}>
        <AttachmentViewerHost known={[]} />
      </QueryClientProvider>,
    );
    useAttachmentViewer.getState().open("A1");
    expect(useAttachmentViewer.getState().openId).toBe("A1");
    unmount();
    expect(useAttachmentViewer.getState().openId).toBeNull();
  });
});
