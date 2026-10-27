import { createFileRoute } from "@tanstack/react-router";
import { requireSession } from "@/app/session";
import { ConnectProgress } from "@/features/connect/connect-progress";

export const Route = createFileRoute("/connect/$jobId")({
  beforeLoad: ({ context }) => requireSession(context.queryClient),
  component: () => <ConnectProgress jobId={Route.useParams().jobId} />,
});
