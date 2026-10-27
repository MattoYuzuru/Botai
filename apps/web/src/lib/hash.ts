/** SHA-256 в hex через WebCrypto (доступен на https и localhost). */
export async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Каноническая форма полей предложения: JSON-объект с ключами по возрастанию, без пробелов, без экранирования
 * не-ASCII. Побайтово совпадает с `canonical_fields` сервера (`botai/agent/actions.py`):
 * `json.dumps(dict(sorted(fields.items())), ensure_ascii=False, separators=(",", ":"))`.
 */
export function canonicalFields(fields: Record<string, string>): string {
  const entries = Object.entries(fields).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([name, value]) => `${JSON.stringify(name)}:${JSON.stringify(value)}`).join(",")}}`;
}

/**
 * Хеш полей, которые человек видел: `POST /actions/{id}/approve` принимает итоговые поля и хеш показанных.
 * Если показанное изменилось на сервере, он ответит `action_changed`.
 */
export async function fieldsHash(fields: Record<string, string>): Promise<string> {
  return sha256Hex(canonicalFields(fields));
}
