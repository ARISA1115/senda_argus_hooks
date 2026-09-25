import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type { EventRecord, Exporter } from "../core/types.js";

export class JsonlExporter implements Exporter {
  constructor(public readonly path = "./senda-events.jsonl") {}
  emit(event: EventRecord): void {
    mkdirSync(dirname(this.path), { recursive: true });
    appendFileSync(this.path, `${JSON.stringify(event)}\n`, "utf8");
  }
}
