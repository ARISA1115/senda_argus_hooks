declare const process: {
  version: string; platform: string; arch: string; pid: number;
  env: Record<string, string | undefined>;
  argv: string[];
  cwd(): string;
  once(event: string, fn: (...args: any[]) => void): any;
  stdout: { write(value: string): void };
  stderr: { write(value: string): void };
};
declare module "node:async_hooks" { export class AsyncLocalStorage<T> { getStore(): T | undefined; run<R>(store: T, callback: () => R): R; } }
declare module "node:crypto" { export function randomUUID(): string; export function createHash(name: string): { update(value: string): any; digest(encoding: "hex"): string }; export function createHmac(name: string, key: string): { update(value: string): any; digest(encoding: "base64url" | "hex"): string }; export function randomBytes(size: number): { toString(encoding: "hex"): string }; }
declare module "node:fs" { export function appendFileSync(path: string, data: string, encoding: string): void; export function mkdirSync(path: string, opts?: { recursive?: boolean }): void; export function readFileSync(path: string, encoding: string): string; export function existsSync(path: string): boolean; export function writeFileSync(path: string, data: string, encoding: string): void; }
declare module "node:path" { export function dirname(path: string): string; export function join(...parts: string[]): string; }
declare module "node:perf_hooks" { export const performance: { now(): number }; }
declare module "node:test" { const test: (name: string, fn: () => void | Promise<void>) => void; export default test; }
declare module "node:assert/strict" { const assert: any; export default assert; }

declare module "node:os" { export function homedir(): string; }
declare module "node:module" { const Module: any; export default Module; export function register(specifier: string | URL, parentURL?: string | URL): void; export function createRequire(filename: string | URL): any; }
declare module "node:url" { export function pathToFileURL(path: string): URL; }

declare function fetch(input: string, init?: any): Promise<any>;
declare class AbortController { signal: any; abort(): void; }
declare function setTimeout(fn: () => void, ms: number): any;
declare function clearTimeout(id: any): void;
declare function setInterval(fn: () => void, ms: number): any;
declare function clearInterval(id: any): void;
declare class TextEncoder { encode(value: string): Uint8Array; }
