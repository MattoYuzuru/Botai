import { createFileRoute } from "@tanstack/react-router";
import { requireSession } from "@/app/session";
import { ConnectPage } from "@/features/connect/connect-page";

export const Route = createFileRoute("/connect/")({
  beforeLoad: ({ context }) => requireSession(context.queryClient),
  validateSearch: (search: Record<string, unknown>): { space?: string; university?: string } => ({
    space: typeof search.space === "string" ? search.space : undefined,
    university: typeof search.university === "string" ? search.university : undefined,
  }),
  component: ConnectPage,
});
