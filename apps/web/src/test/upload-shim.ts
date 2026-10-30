import { vi } from "vitest";

/**
 * Загрузка файлов в тестах. В jsdom `FormData` и `File` — классы jsdom, а `fetch` и MSW работают на классах Node:
 * тело multipart до обработчика не доходит. Эта подмена `XMLHttpRequest` собирает multipart вручную и отправляет его
 * через `fetch`, так что обработчик MSW получает настоящий файл. Только для Vitest: в браузере (Playwright) —
 * настоящий `XMLHttpRequest`.
 */
const read = (blob: Blob): Promise<Uint8Array> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });

class UploadShim {
  withCredentials = false;
  responseType = "";
  status = 0;
  responseText = "";
  upload: {
    onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null;
  } = {
    onprogress: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  private method = "GET";
  private url = "";
  private headers: Record<string, string> = {};
  private readonly controller = new AbortController();

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  abort() {
    this.controller.abort();
    this.onabort?.();
  }
  send(body: FormData) {
    void (async () => {
      const boundary = `botai${Math.random().toString(16).slice(2)}`;
      const encoder = new TextEncoder();
      const chunks: Uint8Array[] = [];
      for (const [name, value] of body.entries()) {
        if (typeof value === "string") {
          chunks.push(
            encoder.encode(
              `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`,
            ),
          );
        } else {
          chunks.push(
            encoder.encode(
              `--${boundary}\r\nContent-Disposition: form-data; name="${name}"; filename="${encodeURIComponent((value as File).name)}"\r\nContent-Type: ${value.type || "application/octet-stream"}\r\n\r\n`,
            ),
            await read(value),
            encoder.encode("\r\n"),
          );
        }
      }
      chunks.push(encoder.encode(`--${boundary}--\r\n`));
      const size = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      const payload = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        payload.set(chunk, offset);
        offset += chunk.length;
      }
      try {
        const response = await fetch(this.url, {
          method: this.method,
          headers: { ...this.headers, "content-type": `multipart/form-data; boundary=${boundary}` },
          body: Buffer.from(payload),
          signal: this.controller.signal,
        });
        this.upload.onprogress?.({ lengthComputable: true, loaded: size, total: size });
        this.status = response.status;
        this.responseText = await response.text();
        this.onload?.();
      } catch {
        if (!this.controller.signal.aborted) this.onerror?.();
      }
    })();
  }
}

/** Ставит подмену; `vi.unstubAllGlobals()` возвращает настоящий `XMLHttpRequest`. */
export function installUploadShim(): void {
  vi.stubGlobal("XMLHttpRequest", UploadShim);
}
