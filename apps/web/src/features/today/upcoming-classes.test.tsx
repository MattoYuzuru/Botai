import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { eventFixture } from "@/test/data-fixtures";
import { UpcomingClasses, upcomingDay, upcomingFreshness } from "./upcoming-classes";

const MSK = "Europe/Moscow";
const NOW = Date.UTC(2026, 10, 27, 12, 0);
const at = (iso: string, id: string) => ({
  ...eventFixture(),
  id,
  startsAt: iso,
  endsAt: new Date(Date.parse(iso) + 80 * 60_000).toISOString(),
});

describe("ближайший день с парами", () => {
  it("первый день, где пары есть; все пары этого дня и только он", () => {
    const events = [
      at("2026-11-29T08:00:00.000Z", "c"),
      at("2026-11-28T08:00:00.000Z", "a"),
      at("2026-11-28T11:00:00.000Z", "b"),
    ];
    const found = upcomingDay(events, MSK);
    expect(found?.events.map((event) => event.id)).toEqual(["a", "b"]);
    expect(upcomingDay([], MSK)).toBeNull();
  });
});

describe("подпись свежести ближайших пар", () => {
  const base = { events: [], snapshotAt: null, refreshFailedAt: null, nowMs: NOW, timeZone: MSK };

  it("свежие данные — без подписи", () => {
    expect(upcomingFreshness({ ...base, events: [at("2026-11-28T08:00:00.000Z", "a")] })).toBeNull();
  });

  it("снимок из офлайн-кэша, неудавшееся обновление и устаревшие пары называются каждый своими словами", () => {
    expect(upcomingFreshness({ ...base, snapshotAt: Date.UTC(2026, 10, 27, 9, 0) })).toBe(
      "Нет сети. Показан снимок от 27 ноября, 12:00",
    );
    expect(upcomingFreshness({ ...base, refreshFailedAt: Date.UTC(2026, 10, 27, 10, 30) })).toMatch(
      /^Не удалось обновить, обновление идёт само\. Показаны данные от 27 ноября, 13:30$/,
    );
    const stale = {
      ...at("2026-11-28T08:00:00.000Z", "a"),
      provenance: { ...eventFixture().provenance, stale: true, fetchedAt: "2026-11-24T09:00:00.000Z" },
    };
    expect(upcomingFreshness({ ...base, events: [stale] })).toBe("Данные от 24 ноября");
  });
});

describe("UpcomingClasses: состояния", () => {
  const props = { day: null, events: [], spaces: new Map(), nowMs: NOW, timeZone: MSK, onOpen: () => {} };

  it("идёт поиск — скелетон; нет сети — «Нет сети» без скелетона; сбой — словами и без кнопки повтора", () => {
    const { rerender } = render(<UpcomingClasses {...props} status="loading" />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    rerender(<UpcomingClasses {...props} status="offline" />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("Нет сети")).toBeInTheDocument();
    expect(screen.getByText(/появятся, когда вернётся сеть/)).toBeInTheDocument();
    rerender(<UpcomingClasses {...props} status="failed" />);
    expect(screen.getByText("Ближайшие пары не загрузились")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
