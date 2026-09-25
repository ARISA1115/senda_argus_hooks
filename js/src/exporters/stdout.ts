import type { EventRecord, Exporter } from "../core/types.js";
export class StdoutExporter implements Exporter {
  emit(event: EventRecord): void { process.stdout.write(`${JSON.stringify(event)}\n`); }
}
