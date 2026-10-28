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
  meta,
  narrow = false,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  leading?: "back" | ReactNode;
  onBack?: () => void;
  trailing?: ReactNode;
  meta?: ReactNode;
  /** Одна читаемая колонка, в том числе на ноутбуке: подключение, формы и последовательные шаги. */
  narrow?: boolean;
  children: ReactNode;
}) {
  return (
    <>
      <ScreenHeader
        title={title}
        subtitle={subtitle}
        leading={leading}
        onBack={onBack}
        trailing={trailing}
        meta={meta}
        narrow={narrow}
      />
      <main
        className={`mx-auto flex w-full max-w-col flex-1 flex-col gap-6 px-safe-4 pt-4 pb-safe-24 xs:px-safe-5 lg:pb-safe-10 ${narrow ? "" : "lg:max-w-page"}`}
      >
        {children}
      </main>
    </>
  );
}
