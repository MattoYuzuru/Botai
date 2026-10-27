import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
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
  // Код приглашения нужен один раз: берём его из адреса и убираем из истории браузера.
  const [initialInvite] = useState(invite ?? "");
  useEffect(() => {
    if (invite) void navigate({ to: "/register", search: {}, replace: true });
  }, [invite, navigate]);
  return <RegisterPage initialInvite={initialInvite} />;
}
