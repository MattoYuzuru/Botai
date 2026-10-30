import { describe, expect, it } from "vitest";
import { connectionFixture, provenanceFixture } from "@/test/data-fixtures";
import { openUrlOf, siteUrlOf } from "./open-url";

const API = "https://lk.fakeuni.test/api/get-myschedule?begin_date=2026-10-26";

describe("openUrlOf: адрес для людей — страница интерфейса, не API", () => {
  it("openUrl провенанса важнее всего", () => {
    const provenance = provenanceFixture({
      sourceUrl: API,
      openUrl: "https://lk.fakeuni.test/schedule?begin_date=2026-10-26",
    });
    expect(openUrlOf(provenance, connectionFixture())).toBe(
      "https://lk.fakeuni.test/schedule?begin_date=2026-10-26",
    );
  });

  it("потом страница входа в данные источника и его origin", () => {
    const source = connectionFixture({ entryUrl: "https://lk.fakeuni.test/schedule" });
    expect(openUrlOf(provenanceFixture({ openUrl: null }), source)).toBe("https://lk.fakeuni.test/schedule");
    expect(openUrlOf(provenanceFixture({ openUrl: null }), { ...source, entryUrl: null })).toBe(
      "https://lk.fakeuni.test/",
    );
  });

  it("адрес API (sourceUrl) не возвращается никогда: нет ничего другого — нет ссылки", () => {
    expect(openUrlOf(provenanceFixture({ sourceUrl: API, openUrl: null }))).toBeNull();
    expect(openUrlOf(provenanceFixture({ sourceUrl: API, openUrl: null }), { origin: "" })).toBeNull();
  });

  it("небезопасные схемы отбрасываются и уступают следующему кандидату", () => {
    const provenance = provenanceFixture({ openUrl: "javascript:alert(1)" });
    expect(openUrlOf(provenance, { origin: "https://lk.fakeuni.test" })).toBe("https://lk.fakeuni.test/");
  });
});

describe("siteUrlOf: «Открыть сайт»", () => {
  it("страница входа в данные, а без неё origin; у документа без адреса сайта нет", () => {
    expect(
      siteUrlOf({ origin: "https://lk.fakeuni.test", entryUrl: "https://lk.fakeuni.test/schedule" }),
    ).toBe("https://lk.fakeuni.test/schedule");
    expect(siteUrlOf({ origin: "https://lk.fakeuni.test", entryUrl: null })).toBe("https://lk.fakeuni.test/");
    expect(siteUrlOf({ origin: "", entryUrl: null })).toBeNull();
  });
});
