import type { EventRecord, Exporter } from "../core/types.js";

export class NullExporter implements Exporter {
  emit(_event: EventRecord): void {}
}
