import { createFileRoute } from "@tanstack/react-router";
import { requireSession } from "@/app/session";
import { WelcomePage } from "@/features/onboarding/welcome-page";

export const Route = createFileRoute("/welcome")({
  beforeLoad: ({ context }) => requireSession(context.queryClient),
  component: WelcomePage,
});
