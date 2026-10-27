import { describe, expect, it } from "vitest";
import { canonicalFields, fieldsHash, sha256Hex } from "./hash";

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

  it("каноническая форма — JSON-объект с ключами по возрастанию, без пробелов и без экранирования не-ASCII", () => {
    expect(canonicalFields({ type: "Об обучении", copies: "2" })).toBe('{"copies":"2","type":"Об обучении"}');
    expect(canonicalFields({})).toBe("{}");
  });

  // Эталоны посчитаны кодом сервера: `fields_hash` из `botai/agent/actions.py` на тех же полях.
  it("хеш совпадает с хешем сервера побайтово", async () => {
    expect(await fieldsHash({ copies: "2", type: "Об обучении" })).toBe(
      "cceaf2e61a631ff81436b8014b3193e1e7538ba3de566b0fa7e3a4711acee92c",
    );
    expect(await fieldsHash({ comment: 'a "q" \\ \n\t', course: "Базы данных", b: "😀 <&>" })).toBe(
      "48fae1c764856d3cc14390d574736958f30d3d91a288f8b6ba724d77f789207b",
    );
    expect(await fieldsHash({})).toBe("44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a");
  });
});
