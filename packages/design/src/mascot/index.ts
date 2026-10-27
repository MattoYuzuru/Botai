/** Маскот Варя: чистое состояние, реплики, сезонные образы, тихие режимы (docs/design/mascot.md). */
export {
  activeEvents,
  activeSeasonalEvent,
  inWindow,
  MASCOT_EVENTS,
  type MascotEvent,
  type MascotSlot,
} from "./events";
export {
  availableLines,
  FLAG_FIELDS,
  formatSnapshotTime,
  LINES,
  type LineField,
  type LineFields,
  type LineKey,
  type LineVariant,
  MAX_LINE_LENGTH,
  pickLine,
  placeholdersOf,
  renderTemplate,
} from "./lines";
export {
  isLiteEnvironment,
  isMascotStatic,
  type MascotQuietFlags,
  useLite,
  useMascotQuiet,
} from "./quiet";
export {
  CLASS_SOON_MS,
  detectDoneEvent,
  eventKey,
  isNight,
  LINE_ROTATION_MS,
  RECENT_EVENT_MS,
  resolveMascot,
} from "./resolve";
export {
  STAGE_ACTION_MS,
  STAGE_ACTIONS,
  STAGE_PAUSE_MS,
  type StageAction,
  type StageStep,
  stagePlan,
  stageStep,
} from "./stage-plan";
export * from "./types";
export { getVarya, seededRandom, type VaryaApi, type VaryaHandle } from "./varya";
