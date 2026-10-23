"""Generate benchmark comparison reports and individual run summaries."""

import csv
import json
from collections.abc import Sequence
from dataclasses import asdict
from html import escape
from pathlib import Path
from urllib.parse import quote

from .models import BatchRunResult, JobRunStats, ResourceSummary


class SuiteReports:
    """Write compact run summaries and a consolidated benchmark report."""

    @staticmethod
    def _row(run: BatchRunResult) -> dict:
        stats = run.job_stats or JobRunStats(environment=run.environment, file_type=run.file_type)
        host_cpu = next((r for r in run.resources if r.scope == "host" and r.metric == "cpu_percent"), None)
        host_memory = next((r for r in run.resources if r.scope == "host" and r.metric == "memory_bytes"), None)
        gpu = next((r for r in run.resources if r.scope == "gpu" and r.metric == "gpu_utilization_percent"), None)
        row = {
            "environment": run.environment,
            "file_type": run.file_type,
            "status": run.status,
            "completed_docs": stats.completed_docs,
            "failed_docs": stats.failed_docs,
            "skipped_docs": stats.skipped_docs,
            "pages": stats.total_pages_processed,
            "duration_seconds": stats.total_execution_time_seconds,
            "documents_per_second": stats.documents_per_second,
            "pages_per_second": stats.pages_per_second,
            "host_cpu_avg_percent": host_cpu.avg_value if host_cpu else None,
            "host_memory_peak_gib": host_memory.max_value / 1024**3 if host_memory else None,
            "gpu_avg_percent": gpu.avg_value if gpu else None,
        }
        # The bundled benchmark flows share these five operators.
        for column, operator in (
            ("ingest_seconds", "ingest_with_filesystem"),
            ("extract_seconds", "extract_with_docling_serve"),
            ("chunker_seconds", "chunker_with_docling_serve"),
            ("embeddings_seconds", "embeddings_with_ollama"),
            ("vectordb_seconds", "vectordb_with_opensearch"),
        ):
            row[column] = stats.operator_timings_seconds.get(operator)
        return {key: round(value, 3) if isinstance(value, float) else value for key, value in row.items()}

    @staticmethod
    def _write_csv(*, path: Path, rows: list[dict], default_fields: tuple[str, ...] = ()) -> None:
        fields = list(dict.fromkeys(key for row in rows for key in row)) or list(default_fields)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)

    @staticmethod
    def _table(*, headers: list[str], rows: Sequence[Sequence[object]]) -> str:
        def cell(value: object) -> str:
            if value is None:
                return "—"
            if isinstance(value, float):
                return f"{value:,.3f}"
            return escape(str(value))

        return (
            '<div class="table"><table><thead><tr>'
            + "".join(f"<th>{escape(header)}</th>" for header in headers)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell(value)}</td>" for value in row) + "</tr>" for row in rows)
            + "</tbody></table></div>"
        )

    @staticmethod
    def write(*, output_dir: Path, runs: Sequence[BatchRunResult]) -> None:
        individual = len(runs) == 1 and runs[0].output_dir == output_dir
        report_name = "report.html" if individual else "consolidated_report.html"
        for run in runs:
            if run.output_dir != output_dir:
                SuiteReports.write(output_dir=run.output_dir, runs=[run])
        rows = [SuiteReports._row(run) for run in runs]
        SuiteReports._write_csv(path=output_dir / "job_run_stats.csv", rows=rows)
        if individual:
            (output_dir / "_stats").mkdir(parents=True, exist_ok=True)
            SuiteReports._write_csv(
                path=output_dir / "resource_summary.csv",
                rows=[asdict(resource) for resource in runs[0].resources],
                default_fields=tuple(ResourceSummary.__dataclass_fields__),
            )
        comparison = SuiteReports._table(
            headers=[
                "Environment",
                "File type",
                "Status",
                "Completed docs",
                "Failed",
                "Skipped",
                "Pages",
                "Seconds",
                "Docs/s",
                "Pages/s",
                "Host CPU avg %",
                "Host RAM peak (GiB)",
                "GPU avg %",
            ],
            rows=[
                [
                    row[key]
                    for key in (
                        "environment",
                        "file_type",
                        "status",
                        "completed_docs",
                        "failed_docs",
                        "skipped_docs",
                        "pages",
                        "duration_seconds",
                        "documents_per_second",
                        "pages_per_second",
                        "host_cpu_avg_percent",
                        "host_memory_peak_gib",
                        "gpu_avg_percent",
                    )
                ]
                for row in rows
            ],
        )
        details = []
        for index, run in enumerate(runs, 1):
            stats = run.job_stats
            metrics = asdict(stats) if stats else {}
            metrics.pop("operator_timings_seconds", None)
            summary = SuiteReports._table(
                headers=["Metric", "Value"],
                rows=[
                    ["Outcome", run.status],
                    ["Job run ID", run.job_run_id],
                    ["Started", run.started_at],
                    ["Ended", run.ended_at],
                    ["Case seconds (setup through cleanup)", run.case_duration_seconds],
                    *[[key, value] for key, value in metrics.items()],
                ],
            )
            operators = SuiteReports._table(
                headers=["Operator", "Seconds"],
                rows=list((stats.operator_timings_seconds if stats else {}).items()),
            )
            resources = SuiteReports._table(
                headers=[
                    "Scope",
                    "Service/target",
                    "Metric",
                    "Unit",
                    "Samples",
                    "Min",
                    "Average",
                    "P95",
                    "P99",
                    "Peak",
                ],
                rows=[
                    [
                        r.scope,
                        r.target,
                        r.metric,
                        r.unit,
                        r.samples,
                        r.min_value,
                        r.avg_value,
                        r.p95_value,
                        r.p99_value,
                        r.max_value,
                    ]
                    for r in run.resources
                ],
            )
            capacity_file = run.output_dir / "_resources" / "resource_capacity.json"
            capacity = json.loads(capacity_file.read_text(encoding="utf-8")) if capacity_file.exists() else {}
            config = escape(json.dumps({**run.configuration, **capacity}, indent=2))
            errors = escape("\n".join(filter(None, [run.error, *run.diagnostic_errors, *run.cleanup_errors])))
            artifacts = " · ".join(
                f'<a href="{quote(path.relative_to(output_dir).as_posix())}">{escape(path.relative_to(run.output_dir).as_posix())}</a>'
                for path in sorted(run.output_dir.rglob("*"))
                if path.is_file() and path != output_dir / report_name
            )
            resource_details = (
                f"<h3>Resources</h3>{resources}"
                if individual
                else f"<details><summary>Detailed resources</summary>{resources}</details>"
            )
            details.append(
                f'<section id="run-{index}"><h2>{index}. {escape(run.environment)} / {escape(run.file_type)}</h2>'
                f'{summary}<pre class="errors">{errors}</pre><h3>Operator timings</h3>{operators}'
                f"{resource_details}<details><summary>Configuration and capacity</summary><pre>{config}</pre>"
                f"</details><p>{artifacts}</p></section>"
            )
        extra_links = (
            ' · <a href="resource_summary.csv">Resource CSV</a> · <a href="_stats/run_summary.json">Run JSON</a>'
            if individual
            else ""
        )
        document = (
            '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            "<title>Batching benchmark report</title><style>"
            "body{font:15px system-ui;max-width:1500px;margin:32px auto;padding:0 24px;color:#18212c}"
            "table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}"
            "td,th{border:1px solid #d7dce2;padding:8px;text-align:left;white-space:nowrap}"
            "th{background:#edf2f7}tr:nth-child(even){background:#f8fafc}.table{overflow:auto}"
            "section{margin-top:40px;border-top:2px solid #d7dce2}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
            ".errors{color:#9b2020}a{color:#175ca5}summary{cursor:pointer}</style>"
            f"<h1>{'Run summary' if individual else 'Consolidated benchmark report'}</h1>"
            f'<p><a href="job_run_stats.csv">Summary CSV</a>{extra_links}</p>'
            "<p>CPU and memory percentages use host capacity in all scopes. CPU cores and memory bytes provide absolute "
            "usage. Resource summaries cover submission through completion detection, excluding collector warm-up. Host readings include services and other activity; do not add overlapping scopes. Native memory "
            "is summed RSS; container memory excludes inactive file cache. GPU is host-wide driver utilization (maximum across reported GPUs). Missing metrics are blank "
            "in CSV. Job seconds exclude setup and teardown. Operator timings may overlap.</p>"
            + comparison
            + "".join(details)
            + "</html>"
        )
        outputs = [(report_name, document)]
        if individual:
            outputs.append(("_stats/run_summary.json", json.dumps(asdict(runs[0]), indent=2, default=str) + "\n"))
        for name, content in outputs:
            path = output_dir / name
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        if not individual:
            # Remove only obsolete files generated by the previous suite report layout.
            for obsolete in ("report.html", "resource_summary.csv", "_stats/suite_summary.json"):
                (output_dir / obsolete).unlink(missing_ok=True)
            legacy_stats = output_dir / "_stats"
            if legacy_stats.exists() and not any(legacy_stats.iterdir()):
                legacy_stats.rmdir()
