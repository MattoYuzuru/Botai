import { describe, expect, it } from "vitest";
import {
  ATTACK_MS,
  bandLevels,
  bandRanges,
  peakLevel,
  RELEASE_MS,
  smoothLevel,
  VOICE_HIGH_HZ,
  VOICE_LOW_HZ,
} from "./audio-levels";

const SAMPLE_RATE = 48_000;
const BINS = 512; // fftSize 1024

describe("bandRanges", () => {
  it("делит голосовой диапазон на заданное число полос слева направо без пересечений", () => {
    for (const bars of [12, 24, 32]) {
      const ranges = bandRanges(SAMPLE_RATE, BINS, bars);
      expect(ranges).toHaveLength(bars);
      for (const [index, range] of ranges.entries()) {
        const [from, to] = range;
        expect(to).toBeGreaterThan(from);
        expect(to).toBeLessThanOrEqual(BINS);
        const previous = ranges[index - 1];
        if (previous) expect(from).toBe(previous[1]);
      }
    }
  });

  it("первая полоса начинается у нижней границы голоса, последняя заканчивается у верхней", () => {
    const ranges = bandRanges(SAMPLE_RATE, BINS, 32);
    const hzPerBin = SAMPLE_RATE / 2 / BINS;
    expect((ranges[0]?.[0] ?? 0) * hzPerBin).toBeGreaterThanOrEqual(VOICE_LOW_HZ - hzPerBin);
    expect((ranges.at(-1)?.[1] ?? 0) * hzPerBin).toBeLessThanOrEqual(VOICE_HIGH_HZ + hzPerBin);
  });

  it("логарифмическая шкала: нижние полосы уже верхних", () => {
    const ranges = bandRanges(SAMPLE_RATE, BINS, 16);
    const width = (range: readonly [number, number] | undefined) => (range ? range[1] - range[0] : 0);
    expect(width(ranges.at(-1))).toBeGreaterThan(width(ranges[0]));
  });

  it("при малой частоте дискретизации и малом числе бинов у каждой полосы есть хотя бы бин", () => {
    for (const [from, to] of bandRanges(8000, 64, 32)) expect(to - from).toBeGreaterThanOrEqual(1);
  });
});

describe("bandLevels", () => {
  const ranges = bandRanges(SAMPLE_RATE, BINS, 24);
  const levelsOf = (fill: (bin: number) => number) => {
    const spectrum = new Uint8Array(BINS).map((_, bin) => fill(bin));
    const out = new Float32Array(ranges.length);
    bandLevels(spectrum, ranges, out);
    return out;
  };

  it("тишина и шум комнаты дают нули", () => {
    expect(peakLevel(levelsOf(() => 0))).toBe(0);
    expect(peakLevel(levelsOf(() => 15))).toBe(0);
  });

  it("громкий голос поднимает столбики, а уровни не выходят за 0…1", () => {
    const speech = levelsOf(() => 180);
    expect(peakLevel(speech)).toBeGreaterThan(0.7);
    const loud = levelsOf(() => 255);
    for (const value of loud) {
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThanOrEqual(1);
    }
  });

  it("реагирует на громкость: громче вход — выше столбики", () => {
    const quiet = levelsOf(() => 60);
    const medium = levelsOf(() => 120);
    const loud = levelsOf(() => 200);
    expect(peakLevel(medium)).toBeGreaterThan(peakLevel(quiet));
    expect(peakLevel(loud)).toBeGreaterThan(peakLevel(medium));
  });

  it("шумит только одна часть спектра — поднимаются только её столбики", () => {
    const [from, to] = ranges[2] ?? [0, 1];
    const out = levelsOf((bin) => (bin >= from && bin < to ? 220 : 0));
    expect(out[2]).toBeGreaterThan(0.5);
    expect(out[10]).toBe(0);
    expect(out[20]).toBe(0);
  });
});

describe("smoothLevel", () => {
  it("взлетает быстрее, чем опадает", () => {
    const up = smoothLevel(0, 1, ATTACK_MS);
    const down = smoothLevel(1, 0, ATTACK_MS);
    expect(up).toBeGreaterThan(0.6);
    expect(1 - down).toBeLessThan(0.3);
    expect(RELEASE_MS).toBeGreaterThan(ATTACK_MS);
  });

  it("не зависит от частоты кадров: два шага по 8 мс = один шаг в 16 мс", () => {
    const once = smoothLevel(0, 1, 16);
    const twice = smoothLevel(smoothLevel(0, 1, 8), 1, 8);
    expect(twice).toBeCloseTo(once, 5);
  });

  it("нулевой интервал не меняет уровень, а цель достигается", () => {
    expect(smoothLevel(0.3, 0.9, 0)).toBe(0.3);
    expect(smoothLevel(0.3, 0.9, 5000)).toBeCloseTo(0.9, 3);
  });
});
