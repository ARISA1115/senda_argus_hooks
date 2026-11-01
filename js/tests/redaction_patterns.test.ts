import test from "node:test";
import assert from "node:assert/strict";
import { redactValue } from "../src/core/redaction.js";
import { argumentsScanFields } from "../src/core/result_scan.js";

// 鍵名が一般の項目へ渡された資格情報。形式を分割して書き、リポジトリに本物の形式の値を置かない。
const SECRETS: Array<[string, string]> = [
  ["github pat", "gh" + "p_" + "a".repeat(36)],
  ["github oauth", "gh" + "o_" + "b".repeat(36)],
  ["github fine grained", "github_" + "pat_" + "c".repeat(30)],
  ["slack bot", "xo" + "xb-" + "1234567890-abcdefghij"],
  ["slack app", "xa" + "pp-" + "1-A0123456789-abcdef"],
  ["google api key", "AI" + "za" + "d".repeat(35)],
  ["google oauth", "ya" + "29." + "e".repeat(30)],
  ["jwt", "ey" + "Jhbc" + "." + "payload" + "." + "signature"]
];

for (const [name, secret] of SECRETS) {
  test(`${name} is redacted from a neutral key`, () => {
    const out = redactValue({ value: `use ${secret} now` }) as { value: string };
    assert.ok(!out.value.includes(secret));
    assert.ok(out.value.includes("***REDACTED***"));
  });

  test(`${name} does not reach the arguments scan text`, () => {
    const fields = argumentsScanFields({ value: secret });
    assert.ok(!String(fields.arguments_scan).includes(secret));
  });
}

test("bearer keeps its prefix and hides the rest", () => {
  const out = redactValue({ h: "Bearer abc.def-ghi" }) as { h: string };
  assert.equal(out.h, "Bearer ***REDACTED***");
});

test("ordinary text is not changed", () => {
  assert.deepEqual(redactValue({ q: "list the files in src" }), { q: "list the files in src" });
});
