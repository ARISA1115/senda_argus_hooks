import test from "node:test";
import assert from "node:assert/strict";
import { RESULT_SCAN_MAX_CHARS, retrievedDocumentsScanFields } from "../src/core/result_scan.js";

test("a secret across the document cut point is redacted first", () => {
  const value = "S3cr3tTail".repeat(4000);
  const fields = retrievedDocumentsScanFields(["password=" + value]);
  assert.ok(!String(fields.result_scan).includes("S3cr3tTail"));
});

test("a cut document reports truncation and the original length", () => {
  const docs = ["x ".repeat(20000), "y ".repeat(20000)];
  const fields = retrievedDocumentsScanFields(docs);
  assert.equal(fields.result_scan_truncated, true);
  assert.equal(fields.result_scan_length, docs[0].length + docs[1].length + 1);
});

test("many documents stay within the limit", () => {
  const docs = Array.from({ length: 300 }, (_, i) => `doc-${i} ${"z ".repeat(400)}end-${i}`);
  const fields = retrievedDocumentsScanFields(docs);
  assert.ok(String(fields.result_scan).length <= RESULT_SCAN_MAX_CHARS);
  assert.ok(String(fields.result_scan).includes("end-100"));
  assert.equal(fields.result_scan_truncated, true);
});

