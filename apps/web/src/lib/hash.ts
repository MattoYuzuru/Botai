/** SHA-256 в hex через WebCrypto (доступен на https и localhost). */
export async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Хеш полей предложения действия: `POST /actions/{id}/approve` принимает поля, которые человек видел,
 * и их хеш; при расхождении сервер отказывает (`action_changed`). Каноническая форма: JSON-массив пар
 * `[имя, значение]`, отсортированных по имени, без пробелов. Контракт API называет хеш («sha256 канонических
 * полей»), но форму не фиксирует — договорённость с W5 записана в PR.
 */
export async function fieldsHash(fields: Record<string, string>): Promise<string> {
  const pairs = Object.entries(fields).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return sha256Hex(JSON.stringify(pairs));
}
