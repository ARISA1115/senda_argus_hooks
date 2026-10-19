import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { loadCredential, signatureOf, signHeaders } from "../src/deviceIdentity.js";

const SECRET = Buffer.from(Array.from({ length: 32 }, (_, i) => i)).toString("hex");

function clean() {
  for (const k of ["SENDA_ARGUS_DEVICE_ID", "SENDA_ARGUS_DEVICE_SECRET", "SENDA_ARGUS_DEVICE_EXPIRES_AT", "SENDA_ARGUS_DEVICE_FILE"]) {
    delete process.env[k];
  }
}

test("署名は受け取り側と共有する固定の値に一致する", () => {
  assert.equal(
    signatureOf(SECRET, "dev_x", 1760000000, "5", Buffer.from('{"events":[]}')),
    "aa12665233fa2f6adb99b5ff07424eacc960a9d88daec41c4bb1f3ba2205bd66",
  );
});

test("鍵が無いときは署名を付けない", () => {
  clean();
  process.env.SENDA_ARGUS_DEVICE_FILE = join(mkdtempSync(join(tmpdir(), "dev-")), "none.json");
  assert.deepEqual(signHeaders("{}"), {});
});

test("環境変数の鍵は署名とヘッダーを作り、通し番号は毎回変わる", () => {
  clean();
  process.env.SENDA_ARGUS_DEVICE_ID = "wl-1";
  process.env.SENDA_ARGUS_DEVICE_SECRET = SECRET;
  const a = signHeaders("body");
  const b = signHeaders("body");
  assert.equal(a["X-Argus-Device-Id"], "wl-1");
  assert.equal(
    a["X-Argus-Device-Sig"],
    signatureOf(SECRET, "wl-1", Number(a["X-Argus-Device-Ts"]), a["X-Argus-Device-Seq"], Buffer.from("body")),
  );
  assert.notEqual(a["X-Argus-Device-Seq"], b["X-Argus-Device-Seq"]);
  assert.ok(a["X-Argus-Device-Seq"].length <= 19);
});

test("hex でない秘密では署名を付けない", () => {
  clean();
  process.env.SENDA_ARGUS_DEVICE_ID = "wl-1";
  process.env.SENDA_ARGUS_DEVICE_SECRET = "not-hex";
  assert.deepEqual(signHeaders("x"), {});
});

test("権限が広い鍵のファイルは使わない", { skip: process.platform === "win32" }, () => {
  clean();
  const dir = mkdtempSync(join(tmpdir(), "dev-"));
  const path = join(dir, "device.json");
  writeFileSync(path, JSON.stringify({ device_id: "d1", device_secret: SECRET }));
  process.env.SENDA_ARGUS_DEVICE_FILE = path;
  chmodSync(path, 0o600);
  assert.equal(loadCredential()?.deviceId, "d1");
  chmodSync(path, 0o644);
  assert.equal(loadCredential(), null);
});
