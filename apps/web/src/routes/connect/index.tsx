import { createFileRoute } from "@tanstack/react-router";
import { requireSession } from "@/app/session";
import { ConnectPage } from "@/features/connect/connect-page";

export const Route = createFileRoute("/connect/")({
  beforeLoad: ({ context }) => requireSession(context.queryClient),
  component: ConnectPage,
});
