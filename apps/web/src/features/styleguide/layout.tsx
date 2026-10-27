import type { ReactNode } from "react";

/** Раздел витрины: якорь, заголовок и пояснение со ссылкой на правило стайлгайда. */
export function Section({
  id,
  title,
  note,
  children,
}: {
  id: string;
  title: string;
  note?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="flex scroll-mt-20 flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h2 id={`${id}-title`} className="type-heading text-fg">
          {title}
        </h2>
        {note && <p className="max-w-reading type-caption text-fg-muted">{note}</p>}
      </header>
      <div className="flex flex-col gap-6">{children}</div>
    </section>
  );
}

/** Одно состояние компонента с подписью. */
export function Demo({
  label,
  children,
  wide = false,
}: {
  label: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <p className="type-label text-fg-muted">{label}</p>
      <div className={wide ? "w-full" : "flex flex-wrap items-center gap-3"}>{children}</div>
    </div>
  );
}

/** Сетка образцов или состояний: одна колонка на телефоне, больше — на широких экранах. */
export function Grid({ children, cols = 2 }: { children: ReactNode; cols?: 2 | 3 | 4 }) {
  const columns = { 2: "md:grid-cols-2", 3: "md:grid-cols-3", 4: "md:grid-cols-4" }[cols];
  return <div className={`grid grid-cols-1 gap-6 ${columns}`}>{children}</div>;
}
