import { ScreenHeader } from "@botai/design";
import type { ReactNode } from "react";

/**
 * Рамка экрана вкладки: липкая шапка, колонка `max-w-col` (768–1023 px) или `max-w-page` (от 1024 px),
 * нижний отступ под нижнюю панель с safe area.
 */
export function Screen({
  title,
  subtitle,
  leading,
  onBack,
  trailing,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  leading?: "back" | ReactNode;
  onBack?: () => void;
  trailing?: ReactNode;
  children: ReactNode;
}) {
  return (
    <>
      <ScreenHeader title={title} subtitle={subtitle} leading={leading} onBack={onBack} trailing={trailing} />
      <main className="mx-auto flex w-full max-w-col flex-col gap-6 px-safe-4 pt-4 pb-safe-24 xs:px-safe-5 lg:max-w-page lg:pb-safe-10">
        {children}
      </main>
    </>
  );
}
