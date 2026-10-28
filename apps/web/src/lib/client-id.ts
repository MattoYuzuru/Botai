const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/** ULID для идемпотентного действия клиента; не содержит пользовательских данных. */
export function clientId(): string {
  let time = Date.now();
  let prefix = "";
  for (let i = 0; i < 10; i++) {
    prefix = CROCKFORD[time % 32] + prefix;
    time = Math.floor(time / 32);
  }
  const random = crypto.getRandomValues(new Uint8Array(16));
  return prefix + Array.from(random, (value) => CROCKFORD[value & 31]).join("");
}
