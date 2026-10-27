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
    <main
      data-auth-layout=""
      className="mx-auto flex min-h-dvh w-full max-w-form flex-col justify-center gap-8 px-safe-4 py-safe-8 xs:px-safe-5"
    >
      <header className="flex flex-col gap-6">
        <Wordmark size="md" />
        <div className="flex flex-col gap-2">
          <h1 className="break-words type-display text-fg">{title}</h1>
          <p className="type-ui text-fg-muted">{lead}</p>
        </div>
      </header>
      {children}
      <div className="type-caption text-fg-muted">{footer}</div>
    </main>
  );
}
