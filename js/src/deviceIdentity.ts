// 端末の登録鍵による送信の署名。Python の hooks と同じ式と同じヘッダー。
//
// 鍵は SENDA_ARGUS_DEVICE_ID と SENDA_ARGUS_DEVICE_SECRET の環境変数、または利用者だけが読めるファイル
// (SENDA_ARGUS_DEVICE_FILE か ~/.config/senda-argus/device.json) から読む。鍵が無い、読めない、壊れている、
// 権限が広い、hex として読めない場合は、署名を付けず、送信は止めない。
// 署名の材料は "argus-device-v1" と端末の識別子と時刻と通し番号と本文の SHA-256 を改行でつないだ文字列で、
// 鍵は device_secret を hex として戻した 32 バイト。通し番号は、プロセスの間で重ならないよう乱数にする。

import { createHash, createHmac, randomBytes } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { homedir, hostname, userInfo } from "node:os";
import { join } from "node:path";

const VERSION = "argus-device-v1";
const MAX_FILE_BYTES = 16_384;
const MAX_FIELD = 512;
const HEX = /^(?:[0-9a-fA-F]{2})+$/;

export interface DeviceCredential {
  deviceId: string;
  secret: string;
  expiresAt?: number;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim().slice(0, MAX_FIELD) : "";
}

function filePath(): string {
  const override = (process.env.SENDA_ARGUS_DEVICE_FILE ?? "").trim();
  if (override) return override;
  const base = (process.env.XDG_CONFIG_HOME ?? "").trim() || join(homedir(), ".config");
  return join(base, "senda-argus", "device.json");
}

export function loadCredential(): DeviceCredential | null {
  const id = text(process.env.SENDA_ARGUS_DEVICE_ID);
  const secret = text(process.env.SENDA_ARGUS_DEVICE_SECRET);
  if (id && secret) {
    const raw = Number(process.env.SENDA_ARGUS_DEVICE_EXPIRES_AT);
    return { deviceId: id, secret, expiresAt: Number.isFinite(raw) && raw > 0 ? raw : undefined };
  }
  try {
    const path = filePath();
    const stat = statSync(path);
    if (!stat.isFile() || stat.size > MAX_FILE_BYTES) return null;
    // 他の利用者が読める鍵は使わない。
    if (process.platform !== "win32" && (stat.mode & 0o077) !== 0) return null;
    const data = JSON.parse(readFileSync(path, "utf8"));
    const deviceId = text(data?.device_id);
    const fileSecret = text(data?.device_secret);
    if (!deviceId || !fileSecret) return null;
    const expiresAt = typeof data.expires_at === "number" ? data.expires_at : undefined;
    return { deviceId, secret: fileSecret, expiresAt };
  } catch {
    return null;
  }
}

export function signatureOf(secret: string, deviceId: string, ts: number, seq: string, body: Buffer): string {
  const material = [VERSION, deviceId, String(ts), seq, createHash("sha256").update(body).digest("hex")].join("\n");
  return createHmac("sha256", Buffer.from(secret, "hex")).update(material, "utf8").digest("hex");
}

// 本文の文字列に付ける署名のヘッダー。鍵が無いときは空を返す。送信のたびに呼び、再送は署名し直す。
export function signHeaders(body: string, credential: DeviceCredential | null = loadCredential()): Record<string, string> {
  if (!credential || !HEX.test(credential.secret)) return {};
  const ts = Math.floor(Date.now() / 1000);
  // 63 ビットの乱数。受け取り側は 20 桁までを受け、見た番号の集合で判定する。
  const seq = (randomBytes(8).readBigUInt64BE() >> 1n).toString();
  const headers: Record<string, string> = {
    "X-Argus-Device-Id": credential.deviceId,
    "X-Argus-Device-Ts": String(ts),
    "X-Argus-Device-Seq": seq,
    "X-Argus-Device-Sig": signatureOf(credential.secret, credential.deviceId, ts, seq, Buffer.from(body, "utf8")),
  };
  try {
    headers["X-Argus-Device-User"] = userInfo().username.slice(0, MAX_FIELD);
  } catch {
    // 利用者名を引けない環境でも送信は止めない。
  }
  const host = hostname().slice(0, MAX_FIELD);
  if (host) headers["X-Argus-Device-Host"] = host;
  return headers;
}
