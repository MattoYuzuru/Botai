import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { redirectIfSignedIn } from "@/app/session";
import { RegisterPage } from "@/features/auth/register-page";

export const Route = createFileRoute("/register")({
  validateSearch: (search: Record<string, unknown>): { invite?: string } =>
    typeof search.invite === "string" ? { invite: search.invite } : {},
  beforeLoad: ({ context }) => redirectIfSignedIn(context.queryClient),
  component: Register,
});

function Register() {
  const { invite } = Route.useSearch();
  const navigate = useNavigate();
  // Старые ссылки остаются рабочими; секрет приглашения больше не нужен.
  useEffect(() => {
    if (invite) void navigate({ to: "/register", search: {}, replace: true });
  }, [invite, navigate]);
  return <RegisterPage />;
}
