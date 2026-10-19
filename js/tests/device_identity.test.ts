import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { ArgusExporter } from "../src/exporters/argus.js";
import { encodeHeaderValue, loadCredential, signatureOf, signHeaders } from "../src/deviceIdentity.js";

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

test("非 ASCII の利用者名はヘッダーで percent エンコードされ、ヘッダーに入れられる", () => {
  assert.equal(encodeHeaderValue("山田"), "%E5%B1%B1%E7%94%B0");
  assert.equal(encodeHeaderValue("a".repeat(300)).length, 256);
  assert.ok(encodeHeaderValue("山".repeat(100)).length <= 256);
  assert.ok(/^[\x20-\x7e]*$/.test(encodeHeaderValue("ü名")));
});

test("exporter が実際に送る本文とヘッダーの署名が一致し、報告にも別の本文で署名する", async () => {
  clean();
  process.env.SENDA_ARGUS_DEVICE_ID = "wl-1";
  process.env.SENDA_ARGUS_DEVICE_SECRET = SECRET;
  const sent: { url: string; init: any }[] = [];
  (globalThis as any).fetch = async (url: string, init: any) => {
    sent.push({ url, init });
    return { ok: true, status: 200 };
  };
  const exporter = new ArgusExporter("http://argus.example", "key");
  (exporter as any).noteDiscard(new Date(Date.now() - 60_000).toISOString());
  exporter.emit({ event_id: "e1", event_type: "x", timestamp: new Date().toISOString() } as any);
  await exporter.flush();
  assert.ok(sent.length >= 2);
  for (const { init } of sent) {
    const h = init.headers;
    assert.equal(
      h["X-Argus-Device-Sig"],
      signatureOf(SECRET, "wl-1", Number(h["X-Argus-Device-Ts"]), h["X-Argus-Device-Seq"], Buffer.from(init.body)),
    );
  }
  assert.notEqual(sent[0].init.headers["X-Argus-Device-Sig"], sent[1].init.headers["X-Argus-Device-Sig"]);
});
