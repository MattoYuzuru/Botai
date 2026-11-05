import { createFileRoute } from "@tanstack/react-router";
import { redirectIfSignedIn, returnPath } from "@/app/session";
import { LoginPage } from "@/features/auth/login-page";

export const Route = createFileRoute("/login")({
  validateSearch: (search: Record<string, unknown>): { next?: string } => {
    const next = returnPath(typeof search.next === "string" ? search.next : undefined);
    return next ? { next } : {};
  },
  beforeLoad: ({ context }) => redirectIfSignedIn(context.queryClient),
  component: function LoginRoute() {
    const { next } = Route.useSearch();
    return <LoginPage next={next} />;
  },
});
