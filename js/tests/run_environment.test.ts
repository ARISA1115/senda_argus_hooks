import test from "node:test";
import assert from "node:assert/strict";
import { register } from "../src/register.js";
import { emitEvent } from "../src/runtime.js";
import { validRunEnvironment } from "../src/core/event.js";
import type { EventRecord, Exporter } from "../src/core/types.js";

class MemoryExporter implements Exporter {
  events: EventRecord[] = [];
  emit(event: EventRecord) { this.events.push(event); }
}

test("the run environment tag is limited to the allowed values", () => {
  assert.equal(validRunEnvironment(" Test "), "test");
  assert.equal(validRunEnvironment("dev"), null);
  assert.equal(validRunEnvironment(undefined), null);
});

test("events carry the configured run environment tag at the top level", () => {
  const sink = new MemoryExporter();
  register({ project: "test", exporters: [sink], runEnvironment: "evaluation" });
  emitEvent("tool_call.requested", { data: { run_environment: "production" } });
  const event = sink.events.at(-1)!;
  assert.equal(event.run_environment, "evaluation");
});

test("an unknown tag is not carried", () => {
  const sink = new MemoryExporter();
  register({ project: "test", exporters: [sink], runEnvironment: "dev" });
  emitEvent("tool_call.requested", { data: {} });
  assert.equal(sink.events.at(-1)!.run_environment, null);
});
