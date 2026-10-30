/**
 * Громкость голоса по полосам для эквалайзера записи. Чистые функции: источником служит спектр
 * `AnalyserNode.getByteFrequencyData` (0…255 на бин), потребителем — столбики, которым нужны числа 0…1.
 */

/** Откуда читаем речь: ниже — гул и ветер, выше — шипение, которое столбикам не интересно. */
export const VOICE_LOW_HZ = 80;
export const VOICE_HIGH_HZ = 7000;
/** Ниже этого уровня (доля шкалы анализатора) — шум комнаты: столбик остаётся точкой. */
const NOISE_FLOOR = 0.08;
/** Речь редко доходит до края шкалы: растягиваем остаток, чтобы обычный голос поднимал столбики высоко. */
const GAIN = 1 / 0.7;
/** Верхние частоты речи тише нижних; наклон выравнивает полосы, чтобы правая половина не лежала плашмя. */
const TILT = 0.8;
const SOFTNESS = 1.8;

/** Полуинтервал `[from, to)` номеров бинов спектра, из которых складывается один столбик. */
export type BandRange = readonly [from: number, to: number];

/**
 * Разбивает диапазон голоса на `bars` полос равной «музыкальной» ширины (логарифмическая шкала частот), слева
 * направо от низких к высоким. Каждая полоса получает хотя бы один бин, полосы не пересекаются.
 */
export function bandRanges(sampleRate: number, binCount: number, bars: number): BandRange[] {
  const nyquist = sampleRate / 2;
  const high = Math.min(VOICE_HIGH_HZ, nyquist);
  const bin = (hz: number) => Math.round((hz / nyquist) * binCount);
  const ranges: BandRange[] = [];
  let from = Math.min(binCount - 1, bin(VOICE_LOW_HZ));
  for (let index = 0; index < bars; index += 1) {
    const edge = bin(VOICE_LOW_HZ * (high / VOICE_LOW_HZ) ** ((index + 1) / bars));
    const to = Math.min(binCount, Math.max(from + 1, edge));
    ranges.push([from, to]);
    from = Math.min(to, binCount - 1);
  }
  return ranges;
}

/** Среднее по бинам каждой полосы → уровень 0…1 с отсечкой шума, усилением и наклоном. */
export function bandLevels(
  spectrum: ArrayLike<number>,
  ranges: readonly BandRange[],
  out: Float32Array,
): void {
  const last = Math.max(1, ranges.length - 1);
  for (let index = 0; index < ranges.length; index += 1) {
    const range = ranges[index];
    if (!range) continue;
    const [from, to] = range;
    let sum = 0;
    for (let bin = from; bin < to; bin += 1) sum += spectrum[bin] ?? 0;
    const mean = sum / Math.max(1, to - from) / 255;
    const raw = Math.max(0, mean - NOISE_FLOOR) * GAIN * (1 + (TILT * index) / last);
    // Мягкое насыщение вместо среза: громче вход — всегда чуть выше столбик, но до потолка не упирается.
    out[index] = 1 - Math.exp(-SOFTNESS * raw);
  }
}

/** Общий уровень 0…1 (самая громкая полоса): для статичной полосы при сокращённом движении. */
export function peakLevel(levels: ArrayLike<number>): number {
  let peak = 0;
  for (let index = 0; index < levels.length; index += 1) peak = Math.max(peak, levels[index] ?? 0);
  return peak;
}

/** Постоянные времени сглаживания: столбик взлетает быстро и опадает медленнее, как стрелка прибора. */
export const ATTACK_MS = 45;
export const RELEASE_MS = 180;

/** Шаг сглаживания за `dtMs`, не зависящий от частоты кадров. */
export function smoothLevel(previous: number, target: number, dtMs: number): number {
  const tau = target > previous ? ATTACK_MS : RELEASE_MS;
  return previous + (target - previous) * (1 - Math.exp(-Math.max(0, dtMs) / tau));
}

/** Источник уровней: заполняет `out` (по числу столбиков) значениями 0…1 на текущий момент. */
export interface LevelMeter {
  read: (out: Float32Array) => void;
}
