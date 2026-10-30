import { SourceStatusBar, useNow } from "@botai/design";
import { useNavigate, useRouter } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { Screen } from "@/app/screen";
import type { DataState } from "@/features/data/data-state";
import { ScreenSyncIndicator, SourceNotices, sourceStatusItems } from "@/features/data/source-notices";
import type { Scope, SpaceDirectory } from "@/features/data/use-space-directory";
import type { SectionDef } from "./registry";

export interface SectionContext {
  def: SectionDef;
  directory: SpaceDirectory;
  scope: Scope;
}

/** Подпись экрана раздела: вуз, чьи данные показаны. */
export const scopeSubtitle = (scope: Scope): string | undefined =>
  scope.isAll ? "Все вузы" : scope.spaces[0]?.title;

/**
 * Рамка экрана-раздела: шапка со стрелкой «Назад» (системный жест «назад» тоже работает) и чипами статусов
 * вузов, полосы источников над содержимым. Состояние данных — в `data-state`.
 */
export function SectionFrame({
  ctx,
  state,
  children,
  actions,
}: {
  ctx: SectionContext;
  state: DataState;
  children: ReactNode;
  /** Бизнес-действия раздела над списком («Добавить документ»). */
  actions?: ReactNode;
}) {
  const nowMs = useNow(30_000);
  const router = useRouter();
  const navigate = useNavigate();
  const back = () => {
    if (router.history.canGoBack()) router.history.back();
    else void navigate({ to: "/study" });
  };
  return (
    <Screen
      title={ctx.def.title}
      subtitle={scopeSubtitle(ctx.scope)}
      leading="back"
      onBack={back}
      meta={<SourceStatusBar items={sourceStatusItems(ctx.scope.spaces)} nowMs={nowMs} />}
      trailing={<ScreenSyncIndicator />}
    >
      <div data-screen={`study-${ctx.def.kind}`} data-state={state} className="flex flex-col gap-4">
        <SourceNotices spaces={ctx.scope.spaces} nowMs={nowMs} />
        {actions}
        {children}
      </div>
    </Screen>
  );
}
