/**
 * Общие типы вложений чата: что показывает вьюер, что лежит в поле ввода до отправки и откуда пришли файлы.
 * Загрузку и адреса содержимого знает приложение, компоненты получают готовые данные.
 */
import type { IconName } from "./icon";

export type AttachmentKind = "image" | "document" | "audio" | "other";

/** Вложение в сообщении и во вьюере: `url` — адрес содержимого (тот же origin) для картинки, плеера, PDF и скачивания. */
export interface ViewerAttachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: AttachmentKind;
  url: string;
}

/** Вложение в поле ввода до отправки. `progress` — доля 0..1; не известна — `undefined`, процент не выдумывается. */
export interface ComposerAttachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: AttachmentKind;
  status: "uploading" | "ready" | "failed";
  progress?: number;
  /** Адрес локальной миниатюры (`URL.createObjectURL`) для картинки. */
  previewUrl?: string;
  /** Человеческий текст сбоя; кодов в нём нет. */
  error?: string;
}

/** Откуда пришли файлы: выбор, камера, вставка из буфера, перетаскивание или запись голоса. */
export type FileSource = "picker" | "camera" | "paste" | "drop" | "voice";

const ICONS: Record<AttachmentKind, IconName> = {
  image: "image",
  document: "document",
  audio: "audio",
  other: "file",
};

export const attachmentIcon = (kind: AttachmentKind): IconName => ICONS[kind];

const LABELS: Record<AttachmentKind, string> = {
  image: "Изображение",
  document: "Документ",
  audio: "Аудио",
  other: "Файл",
};

export const attachmentKindLabel = (kind: AttachmentKind): string => LABELS[kind];
