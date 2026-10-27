import { describe, expect, it } from "vitest";
import { fieldsHash, sha256Hex } from "./hash";

describe("sha256Hex", () => {
  it("известные значения", async () => {
    expect(await sha256Hex("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    expect(await sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });
});

describe("хеш полей предложения действия", () => {
  it("не зависит от порядка полей", async () => {
    expect(await fieldsHash({ b: "2", a: "1" })).toBe(await fieldsHash({ a: "1", b: "2" }));
  });

  it("меняется при изменении значения, имени и числа полей", async () => {
    const base = await fieldsHash({ a: "1" });
    expect(await fieldsHash({ a: "2" })).not.toBe(base);
    expect(await fieldsHash({ b: "1" })).not.toBe(base);
    expect(await fieldsHash({ a: "1", b: "" })).not.toBe(base);
  });

  it("каноническая форма — JSON пар [имя, значение] по имени, без пробелов", async () => {
    expect(await fieldsHash({ copies: "2", type: "Об обучении" })).toBe(
      await sha256Hex('[["copies","2"],["type","Об обучении"]]'),
    );
  });
});
