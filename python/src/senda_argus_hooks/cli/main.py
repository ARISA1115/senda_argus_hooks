from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from senda_argus_hooks.exporters.parquet import flatten_event

REQUIRED_KEYS = {"schema_version", "event_id", "trace_id", "span_id", "timestamp", "project", "environment", "event_type"}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_no}: {exc}") from exc


def read_events(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        return read_parquet_dir(path)
    if path.suffix.lower() == ".jsonl":
        return list(iter_jsonl(path))
    if path.suffix.lower() == ".parquet":
        return read_parquet_file(path)
    raise ValueError(f"Unsupported input: {path}")


def read_parquet_file(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Reading parquet requires pyarrow.") from exc
    table = pq.read_table(path)
    return table.to_pylist()


def read_parquet_dir(path: Path) -> list[dict[str, Any]]:
    events = []
    for file_path in sorted(path.glob("*.parquet")):
        events.extend(read_parquet_file(file_path))
    return events


def cmd_inspect(args) -> int:
    events = read_events(Path(args.path))
    if args.summary:
        summary: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("event_type"))
            summary[event_type] = summary.get(event_type, 0) + 1
        print(json.dumps({"count": len(events), "event_types": summary}, ensure_ascii=False, indent=2))
        return 0
    for event in events[: args.limit]:
        print(json.dumps(event, ensure_ascii=False, indent=2 if args.pretty else None, default=str))
    return 0


def cmd_validate(args) -> int:
    errors = []
    for idx, event in enumerate(read_events(Path(args.path)), 1):
        missing = REQUIRED_KEYS - set(event)
        if missing:
            errors.append({"index": idx, "missing": sorted(missing)})
    if errors:
        print(json.dumps({"valid": False, "errors": errors[:50], "error_count": len(errors)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"valid": True}, ensure_ascii=False, indent=2))
    return 0


def cmd_convert(args) -> int:
    src = Path(args.path)
    events = read_events(src)
    if args.to == "parquet":
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Converting to parquet requires pyarrow.") from exc
        out = Path(args.out)
        if out.suffix.lower() == ".parquet":
            out.parent.mkdir(parents=True, exist_ok=True)
            out_file = out
        else:
            out.mkdir(parents=True, exist_ok=True)
            out_file = out / "events-000001.parquet"
        rows = [flatten_event(event) for event in events]
        pq.write_table(pa.Table.from_pylist(rows), out_file, compression=args.compression)
        print(str(out_file))
        return 0
    if args.to == "jsonl":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for event in events:
                if "raw_json" in event:
                    try:
                        event = json.loads(event["raw_json"])
                    except Exception:
                        pass
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        print(str(out))
        return 0
    raise ValueError(f"Unsupported target: {args.to}")




def _event_sort_key(event: dict[str, Any]) -> str:
    return str(event.get("timestamp") or "")


def cmd_trace(args) -> int:
    events = sorted(read_events(Path(args.path)), key=_event_sort_key)
    trace_id = args.trace_id
    if trace_id is None:
        trace_ids = [e.get("trace_id") for e in events if e.get("trace_id")]
        trace_id = trace_ids[0] if trace_ids else None
    selected = [e for e in events if e.get("trace_id") == trace_id] if trace_id else []
    print(json.dumps({"trace_id": trace_id, "count": len(selected), "events": [_brief_event(e) for e in selected]}, ensure_ascii=False, indent=2))
    return 0


def cmd_tools(args) -> int:
    events = read_events(Path(args.path))
    tools: dict[str, dict[str, Any]] = {}
    for event in events:
        mcp = (event.get("data") or {}).get("mcp") or {}
        tool = mcp.get("tool")
        if not tool:
            continue
        key = f"{mcp.get('server') or 'unknown'}::{tool}"
        entry = tools.setdefault(key, {"server": mcp.get("server"), "tool": tool, "count": 0, "purpose_ids": set(), "mcp_profile_ids": set()})
        entry["count"] += 1
        if mcp.get("purpose_id"):
            entry["purpose_ids"].add(mcp.get("purpose_id"))
        if mcp.get("mcp_profile_id"):
            entry["mcp_profile_ids"].add(mcp.get("mcp_profile_id"))
    rows = []
    for entry in tools.values():
        rows.append({**entry, "purpose_ids": sorted(entry["purpose_ids"]), "mcp_profile_ids": sorted(entry["mcp_profile_ids"])})
    print(json.dumps({"tools": rows}, ensure_ascii=False, indent=2))
    return 0



def cmd_retrievals(args) -> int:
    events = read_events(Path(args.path))
    retrievals: dict[str, dict[str, Any]] = {}
    embeddings: dict[str, dict[str, Any]] = {}
    for event in events:
        data = event.get("data") or {}
        retrieval = data.get("retrieval") or {}
        embedding = data.get("embedding") or {}
        if retrieval:
            key = "::".join(
                [
                    str(retrieval.get("framework") or "unknown"),
                    str(retrieval.get("retriever_name") or "unknown"),
                    str(retrieval.get("index_name") or retrieval.get("collection_name") or "unknown"),
                ]
            )
            entry = retrievals.setdefault(
                key,
                {
                    "framework": retrieval.get("framework"),
                    "retriever_name": retrieval.get("retriever_name"),
                    "retriever_type": retrieval.get("retriever_type"),
                    "index_name": retrieval.get("index_name"),
                    "collection_name": retrieval.get("collection_name"),
                    "vector_store": retrieval.get("vector_store"),
                    "count": 0,
                    "completed": 0,
                    "failed": 0,
                    "result_count_total": 0,
                    "latency_ms_total": 0,
                    "latency_ms_count": 0,
                    "purpose_ids": set(),
                },
            )
            entry["count"] += 1
            if event.get("event_type") == "retrieval.completed":
                entry["completed"] += 1
                if retrieval.get("result_count") is not None:
                    entry["result_count_total"] += int(retrieval.get("result_count") or 0)
            if event.get("event_type") == "retrieval.failed":
                entry["failed"] += 1
            if event.get("latency_ms") is not None:
                entry["latency_ms_total"] += int(event.get("latency_ms") or 0)
                entry["latency_ms_count"] += 1
            if retrieval.get("purpose_id"):
                entry["purpose_ids"].add(retrieval.get("purpose_id"))
        if embedding:
            key = "::".join([str(embedding.get("framework") or "unknown"), str(embedding.get("provider") or "unknown"), str(embedding.get("model") or "unknown")])
            entry = embeddings.setdefault(key, {"framework": embedding.get("framework"), "provider": embedding.get("provider"), "model": embedding.get("model"), "count": 0, "completed": 0, "failed": 0, "purpose_ids": set()})
            entry["count"] += 1
            if event.get("event_type") == "embedding.completed":
                entry["completed"] += 1
            if event.get("event_type") == "embedding.failed":
                entry["failed"] += 1
            if embedding.get("purpose_id"):
                entry["purpose_ids"].add(embedding.get("purpose_id"))
    retrieval_rows = []
    for entry in retrievals.values():
        completed = int(entry["completed"] or 0)
        latency_count = int(entry["latency_ms_count"] or 0)
        retrieval_rows.append(
            {
                **{k: v for k, v in entry.items() if not k.endswith("_total") and k not in {"latency_ms_count", "purpose_ids"}},
                "avg_result_count": (entry["result_count_total"] / completed) if completed else None,
                "avg_latency_ms": (entry["latency_ms_total"] / latency_count) if latency_count else None,
                "purpose_ids": sorted(entry["purpose_ids"]),
            }
        )
    embedding_rows = [{**entry, "purpose_ids": sorted(entry["purpose_ids"])} for entry in embeddings.values()]
    print(json.dumps({"retrievals": retrieval_rows, "embeddings": embedding_rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args) -> int:
    events = read_events(Path(args.path))
    by_type: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    by_purpose: dict[str, int] = {}
    for event in events:
        by_type[str(event.get("event_type"))] = by_type.get(str(event.get("event_type")), 0) + 1
        if event.get("agent_id"):
            by_agent[str(event.get("agent_id"))] = by_agent.get(str(event.get("agent_id")), 0) + 1
        if event.get("purpose_id"):
            by_purpose[str(event.get("purpose_id"))] = by_purpose.get(str(event.get("purpose_id")), 0) + 1
    print(json.dumps({"count": len(events), "event_types": by_type, "agents": by_agent, "purposes": by_purpose}, ensure_ascii=False, indent=2))
    return 0


def _brief_event(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    mcp = data.get("mcp") or {}
    llm = data.get("llm") or {}
    tool = data.get("tool") or {}
    retrieval = data.get("retrieval") or {}
    embedding = data.get("embedding") or {}
    return {
        "timestamp": event.get("timestamp"),
        "event_type": event.get("event_type"),
        "status": event.get("status"),
        "span_id": event.get("span_id"),
        "parent_span_id": event.get("parent_span_id"),
        "agent_id": event.get("agent_id"),
        "purpose_id": event.get("purpose_id"),
        "tool": mcp.get("tool") or tool.get("tool_name"),
        "server": mcp.get("server"),
        "model": llm.get("model") or embedding.get("model"),
        "retriever": retrieval.get("retriever_name"),
        "index": retrieval.get("index_name") or retrieval.get("collection_name"),
        "latency_ms": event.get("latency_ms"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="senda-hooks", description="Inspect, validate, and convert Senda-Argus hook event files.")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser("inspect", help="Print events or summary from JSONL/Parquet")
    inspect_p.add_argument("path")
    inspect_p.add_argument("--limit", type=int, default=10)
    inspect_p.add_argument("--pretty", action="store_true")
    inspect_p.add_argument("--summary", action="store_true")
    inspect_p.set_defaults(func=cmd_inspect)

    validate_p = sub.add_parser("validate", help="Validate required event fields")
    validate_p.add_argument("path")
    validate_p.set_defaults(func=cmd_validate)

    convert_p = sub.add_parser("convert", help="Convert JSONL/Parquet files")
    convert_p.add_argument("path")
    convert_p.add_argument("--to", choices=["jsonl", "parquet"], required=True)
    convert_p.add_argument("--out", required=True)
    convert_p.add_argument("--compression", default="zstd")
    convert_p.set_defaults(func=cmd_convert)


    trace_p = sub.add_parser("trace", help="Show events in one trace")
    trace_p.add_argument("path")
    trace_p.add_argument("--trace-id")
    trace_p.set_defaults(func=cmd_trace)

    tools_p = sub.add_parser("tools", help="Summarize MCP tool usage")
    tools_p.add_argument("path")
    tools_p.set_defaults(func=cmd_tools)


    retrievals_p = sub.add_parser("retrievals", help="Summarize RAG retrieval and embedding usage")
    retrievals_p.add_argument("path")
    retrievals_p.set_defaults(func=cmd_retrievals)

    stats_p = sub.add_parser("stats", help="Summarize events, agents, and purposes")
    stats_p.add_argument("path")
    stats_p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
