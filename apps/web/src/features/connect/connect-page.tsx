import { useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { clientId } from "@/lib/client-id";

/** Старый адрес открывает чат; каталог вузов доступен в контексте самого разговора. */
export function ConnectPage() {
  const navigate = useNavigate();
  const { space, university } = useSearch({ from: "/connect/" });
  const [requestId] = useState(clientId);
  useEffect(() => {
    void navigate({ to: "/chat", search: { connect: requestId, space, university }, replace: true });
  }, [navigate, requestId, space, university]);
  return (
    <p role="status" className="px-safe-4 py-6 type-body text-fg-muted">
      Открываю чат…
    </p>
  );
}

/** Адрес кабинета необязателен; если он есть, это ссылка http(s). */
export function validateCabinetUrl(value: string): string | null {
  const text = value.trim();
  if (!text) return null;
  try {
    const url = new URL(text);
    return url.protocol === "https:" || url.protocol === "http:"
      ? null
      : "Нужна ссылка вида https://lk.example.ru";
  } catch {
    return "Нужна ссылка вида https://lk.example.ru";
  }
}
