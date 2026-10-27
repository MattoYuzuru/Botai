import { createFileRoute } from "@tanstack/react-router";
import { redirectIfSignedIn } from "@/app/session";
import { LoginPage } from "@/features/auth/login-page";

export const Route = createFileRoute("/login")({
  beforeLoad: ({ context }) => redirectIfSignedIn(context.queryClient),
  component: LoginPage,
});
