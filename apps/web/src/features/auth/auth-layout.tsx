import { Wordmark } from "@botai/design";
import type { ReactNode } from "react";

/** Рамка экранов входа и регистрации: по центру, в одной колонке, с safe area и `100dvh`. */
export function AuthLayout({
  title,
  lead,
  children,
  footer,
}: {
  title: string;
  lead: string;
  children: ReactNode;
  footer: ReactNode;
}) {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-sm flex-col justify-center gap-8 px-safe-4 py-safe-8 xs:px-safe-5">
      <div className="flex flex-col gap-3">
        <Wordmark size="lg" />
        <h1 className="type-title text-fg">{title}</h1>
        <p className="type-ui text-fg-muted">{lead}</p>
      </div>
      {children}
      <div className="type-ui text-fg-muted">{footer}</div>
    </main>
  );
}
