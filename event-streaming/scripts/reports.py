from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path

from .models import EventTiming

BAR_COLORS = ("#2563eb", "#0f766e", "#b45309", "#7c3aed", "#be185d", "#64748b")


@dataclass(frozen=True)
class _Measurement:
    operation: str
    seconds: float
    pages: int | None

    @property
    def pages_per_second(self) -> float | None:
        if self.pages is None:
            return None
        return self.pages / self.seconds

    @property
    def pages_per_minute(self) -> float | None:
        rate = self.pages_per_second
        return rate * 60 if rate is not None else None


@dataclass(frozen=True)
class _ReportEvent:
    label: str
    event_id: str
    measurements: tuple[_Measurement, ...]
    event_count: int = 1


class TimingReports:
    def __init__(
        self, *, output_dir: Path, event_timings: Mapping[str, EventTiming], operations: Sequence[str],
    ) -> None:
        self.output_dir = output_dir
        self.event_timings = event_timings
        self.operations = operations
        self.warmup: _ReportEvent | None = None

    def _measurements(self, event_ids: Sequence[str]) -> list[_Measurement]:
        events = [self.event_timings[event_id] for event_id in event_ids]
        counts = [event.pages_processed for event in events]
        pages = sum(counts) if None not in counts else None
        durations = [event.flow.duration_seconds for event in events]
        flow_seconds = durations[0] if len(events) == 1 else (
            max(event.flow.ended_at for event in events) - min(event.flow.started_at for event in events)
        ).total_seconds()
        measurements = [_Measurement(operation="flow", seconds=flow_seconds, pages=pages)]

        for operation in self.operations:
            page_counts = []
            seconds = 0.0
            for event in events:
                timing = event.operators[operation]
                seconds += timing.duration_seconds
                page_count = timing.pages_processed if timing.pages_processed is not None else event.pages_processed
                page_counts.append(page_count)
            operator_pages = sum(page_counts) if None not in page_counts else None
            measurements.append(_Measurement(operation=operation, seconds=seconds, pages=operator_pages))
        return measurements

    @staticmethod
    def _tooltip_attributes(description: str, label: str, value: float, unit: str) -> str:
        display = f"{value:,.2f} {unit}"
        if unit == "pages/s":
            display += f" ({value * 60:,.2f} pages/min)"
        tooltip = escape(f"{description}\n{label}: {display}", quote=True)
        return f'tabindex="0" data-tooltip="{tooltip}" aria-label="{tooltip}"'

    @staticmethod
    def _chart(events: Sequence[_ReportEvent], *, throughput: bool, aggregate: bool = False) -> str:
        title = "Pages per second" if throughput else "Execution time"
        unit = "Pages per second" if throughput else "Seconds"
        operations = list(dict.fromkeys(item.operation for event in events for item in event.measurements[1:]))
        series = ["flow", *operations]
        values = [
            {item.operation: item.pages_per_second if throughput else item.seconds for item in event.measurements}
            for event in events
        ]
        maxima = [
            max((value for value in event.values() if value is not None), default=0.0)
            if throughput else max(event["flow"], sum(event[op] for op in operations))
            for event in values
        ]
        axis_max = (max(maxima) or 1.0) * 1.15
        width = max(820, len(events) * max(96, len(series) * 20 + 20) + 110)
        left, top, plot_height, bottom = 90, 42, 280, 322
        group_width = (width - left - 20) / len(events)
        chart = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="390" '
            f'viewBox="0 0 {width} 390" role="img" aria-label="{title} {"aggregate" if aggregate else "per event"}">',
            f'<text x="12" y="20" class="axis-label">{unit}</text>',
        ]
        for tick in range(5):
            value = axis_max * tick / 4
            y = bottom - plot_height * tick / 4
            chart.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width - 20}" y2="{y:.2f}" class="grid"/>')
            chart.append(
                f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" class="tick">{value:,.2f}</text>'
            )
        chart.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"/>')
        for index, (record, event) in enumerate(zip(events, values)):
            center = left + (index + .5) * group_width
            description = record.event_id if aggregate else f"Event {record.label} ({record.event_id})"
            chart.append("<g>")
            if throughput:
                bar_width = min(24, (group_width - 20) / len(series))
                for position, operation in enumerate(series):
                    value = event[operation]
                    x = center + (position - len(series) / 2) * bar_width
                    color = "#172033" if position == 0 else BAR_COLORS[(position - 1) % len(BAR_COLORS)]
                    if value is None:
                        chart.append(
                            f'<text x="{x + bar_width / 2:.2f}" y="{bottom - 5}" '
                            'text-anchor="middle" class="tick">—</text>'
                        )
                        continue
                    height = value / axis_max * plot_height
                    label = "Complete flow" if operation == "flow" else operation
                    tooltip = TimingReports._tooltip_attributes(description, label, value, "pages/s")
                    chart.append(
                        f'<rect x="{x:.2f}" y="{bottom - height:.2f}" width="{bar_width - 2:.2f}" '
                        f'height="{height:.2f}" fill="{color}" class="chart-mark chart-bar" {tooltip}/>'
                    )
            else:
                bar_width = min(54, group_width * .6)
                x = center - bar_width / 2
                stacked = 0.0
                for position, operation in enumerate(operations):
                    value = event[operation]
                    height = value / axis_max * plot_height
                    y = bottom - (stacked + value) / axis_max * plot_height
                    color = BAR_COLORS[position % len(BAR_COLORS)]
                    tooltip = TimingReports._tooltip_attributes(description, operation, value, "seconds")
                    chart.append(
                        f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{height:.2f}" '
                        f'fill="{color}" class="chart-mark chart-bar" {tooltip}/>'
                    )
                    if height >= 18:
                        chart.append(
                            f'<text x="{center:.2f}" y="{y + height / 2 + 4:.2f}" '
                            f'text-anchor="middle" class="segment-label">{value:.2f}</text>'
                        )
                    stacked += value
                flow = event["flow"]
                y = bottom - flow / axis_max * plot_height
                tooltip = TimingReports._tooltip_attributes(description, "Complete flow", flow, "seconds")
                chart.append(
                    f'<line x1="{x - 5:.2f}" y1="{y:.2f}" x2="{x + bar_width + 5:.2f}" '
                    f'y2="{y:.2f}" class="chart-mark flow-marker" {tooltip}/>'
                )
                chart.append(
                    f'<text x="{center:.2f}" y="{y - 7:.2f}" text-anchor="middle" '
                    f'class="flow-label">{flow:,.2f}</text>'
                )
            chart.append(
                f'<text x="{center:.2f}" y="{bottom + 23}" text-anchor="middle" '
                f'class="tick">{escape(record.label)}</text></g>'
            )
        axis_label = "Run" if aggregate else "Events"
        chart.append(
            f'<text x="{(left + width - 20) / 2:.2f}" y="378" text-anchor="middle">{axis_label}</text></svg>'
        )
        return f'<figure><figcaption>{title}</figcaption><div class="chart">' + "".join(chart) + "</div></figure>"

    @staticmethod
    def _summary_row(measurements: Sequence[_Measurement], *, event_count: int) -> dict[str, str]:
        row: dict[str, int | float | None] = {"event_count": event_count}
        row.update({f"{measurement.operation}_seconds": measurement.seconds for measurement in measurements})
        row["total_pages"] = measurements[0].pages
        row.update(
            {f"{measurement.operation}_pages_per_minute": measurement.pages_per_minute for measurement in measurements}
        )
        return {
            key: f"{value:.2f}" if isinstance(value, float) else "" if value is None else str(value)
            for key, value in row.items()
        }

    @staticmethod
    def _summary_table(summaries: Sequence[tuple[str, dict[str, str]]]) -> str:
        fields: dict[str, None] = {}
        for _, summary in summaries:
            fields.update(dict.fromkeys(summary))
        headers = '<th scope="col">Phase</th>' + "".join(f'<th scope="col">{escape(key)}</th>' for key in fields)
        table_rows = []
        for label, summary in summaries:
            cells = "".join(f"<td>{escape(summary[key] or '—')}</td>" for key in fields)
            table_rows.append(f'<tr><th scope="row">{escape(label)}</th>{cells}</tr>')
        return (
            '<div class="table-wrap"><table><caption>Run summary</caption>'
            f"<thead><tr>{headers}</tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>"
        )

    def _write_page(
        self, *, filename: str, phase: str, title: str, measurements: Sequence[_Measurement], event_count: int,
        chart_events: Sequence[_ReportEvent], summaries: Sequence[tuple[str, dict[str, str]]], aggregate: bool,
    ) -> None:
        pages = measurements[0].pages
        page_summary = f"{pages:,} pages" if pages is not None else "page count unavailable"
        operations = list(dict.fromkeys(item.operation for event in chart_events for item in event.measurements[1:]))
        legend = ['<li><span class="flow-swatch"></span>Complete flow</li>']
        legend.extend(
            f'<li><span class="swatch" style="background:{BAR_COLORS[index % len(BAR_COLORS)]}"></span>'
            f'{escape(operation)}</li>'
            for index, operation in enumerate(operations)
        )
        charts = "".join(
            self._chart(chart_events, throughput=throughput, aggregate=aggregate) for throughput in (False, True)
        )
        report = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(phase)} performance report — {escape(title)}</title>
  <style>
    body {{ max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #172033;
      font: 15px/1.5 system-ui, sans-serif; }}
    h1 {{ margin-bottom: .25rem; font-size: 1.6rem; }}
    p, small {{ color: #526174; }}
    figure {{ margin: 1.5rem 0; padding: 1rem; border: 1px solid #dbe2ea; border-radius: 8px; }}
    figcaption {{ margin-bottom: 1rem; font-size: 1.1rem; font-weight: 650; }}
    small {{ font-size: .8rem; font-weight: 400; }}
    .chart, .table-wrap {{ overflow-x: auto; }}
    .chart-mark:focus-visible {{ outline: 2px solid #2563eb; outline-offset: 3px; }}
    .chart-bar {{ stroke: white; stroke-width: .75; vector-effect: non-scaling-stroke; }}
    .chart-bar:hover, .chart-bar:focus-visible {{ stroke: #172033; stroke-width: 1.5; }}
    #chart-tooltip {{ position: fixed; z-index: 10; pointer-events: none; white-space: pre-line;
      box-sizing: border-box; width: max-content;
      max-width: min(460px, calc(100vw - 24px)); padding: .75rem; border-radius: 6px;
      background: #172033; color: white; font-size: .85rem; overflow-wrap: anywhere;
      box-shadow: 0 4px 16px #0003; }}
    table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: .85rem; }}
    caption {{ text-align: left; font-size: 1.1rem; font-weight: 650; margin-bottom: .75rem; }}
    th, td {{ border-bottom: 1px solid #dbe2ea; padding: .6rem; text-align: right; }}
    th {{ background: #f1f5f9; min-width: 7rem; overflow-wrap: anywhere; vertical-align: bottom; }}
    svg {{ display: block; height: auto; }}
    .grid {{ stroke: #e2e8f0; }}
    .axis {{ stroke: #64748b; }}
    .tick, .axis-label {{ fill: #526174; font-size: 12px; }}
    .flow-marker {{ stroke: #172033; stroke-width: 3; }}
    .flow-marker:hover, .flow-marker:focus-visible {{ stroke: #2563eb; }}
    .flow-label {{ fill: #172033; font-size: 11px; font-weight: 700; paint-order: stroke;
      stroke: white; stroke-width: 3px; stroke-linejoin: round; pointer-events: none; }}
    .segment-label {{ fill: white; font-size: 10px; font-weight: 600; pointer-events: none; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: .5rem 1rem; padding: 0; list-style: none; }}
    .legend li {{ display: flex; align-items: center; gap: .4rem; overflow-wrap: anywhere; }}
    .swatch, .flow-swatch {{ display: inline-block; width: .8rem; height: .8rem; flex-shrink: 0; }}
    .flow-swatch {{ background: #172033; }}
    .summary, .legend {{ font-size: .9rem; }}
    nav {{ display: flex; gap: 1rem; margin-bottom: 1rem; }}
    nav a {{ color: #2563eb; }}
    nav [aria-current="page"] {{ color: #172033; font-weight: 650; }}
  </style>
</head>
<body>
  <main>
    <nav aria-label="Report pages">
      <a href="timings.html"{' aria-current="page"' if aggregate else ''}>Aggregate</a>
      <a href="timings-events.html"{'' if aggregate else ' aria-current="page"'}>Warm-up and events</a>
    </nav>
    <h1>{escape(phase)} performance report — {escape(title)}</h1>
    <p class="summary">{event_count} events · {page_summary}</p>
    <ul class="legend">{''.join(legend)}</ul>
    {charts}
    {self._summary_table(summaries)}
  </main>
  <div id="chart-tooltip" role="tooltip" hidden></div>
  <script>
    const popup = document.getElementById("chart-tooltip");
    function positionPopup(x, y) {{
      const box = popup.getBoundingClientRect();
      const left = Math.min(x - box.width / 2, window.innerWidth - box.width - 12);
      const top = y >= box.height + 24 ? y - box.height - 12 : y + 12;
      popup.style.left = `${{Math.max(12, left)}}px`;
      popup.style.top = `${{Math.max(12, top)}}px`;
    }}
    function showPopup(mark) {{
      popup.textContent = mark.dataset.tooltip;
      popup.hidden = false;
      const box = mark.getBoundingClientRect();
      positionPopup(box.left + box.width / 2, box.top);
    }}
    function hidePopup() {{ popup.hidden = true; }}
    document.querySelectorAll(".chart-mark").forEach(mark => {{
      mark.addEventListener("pointerenter", () => showPopup(mark));
      mark.addEventListener("pointerleave", hidePopup);
      mark.addEventListener("focus", () => showPopup(mark));
      mark.addEventListener("blur", hidePopup);
    }});
    document.addEventListener("keydown", key => {{ if (key.key === "Escape") hidePopup(); }});
    window.addEventListener("scroll", hidePopup, true);
    window.addEventListener("resize", hidePopup);
  </script>
</body>
</html>
"""
        path = self.output_dir / filename
        path.write_text(report, encoding="utf-8")
        print(f"{phase} HTML timing report saved to {path}", flush=True)

    def write(self, *, event_ids: Sequence[str], phase: str) -> None:
        measurements = self._measurements(event_ids)
        row = self._summary_row(measurements, event_count=len(event_ids))
        is_warmup = phase == "Warm-up"
        if is_warmup:
            self.warmup = _ReportEvent(
                label="0 (warm-up)", event_id=f"Warm-up phase ({len(event_ids)} events)",
                measurements=tuple(measurements), event_count=len(event_ids),
            )
        warmup = self.warmup
        chart_events = [warmup] if warmup else []
        if not is_warmup:
            chart_events.extend(
                _ReportEvent(label=str(index), event_id=event_id, measurements=tuple(self._measurements([event_id])))
                for index, event_id in enumerate(event_ids, 1)
            )
        summaries = [
            (
                "Warm-up (Event 0)" if event is warmup else f"Event {event.label} ({event.event_id})",
                self._summary_row(event.measurements, event_count=event.event_count),
            )
            for event in chart_events
        ]
        if not is_warmup:
            summaries.append((phase, row))
        aggregate_events = [
            _ReportEvent(
                label="Aggregate", event_id=f"{phase} ({len(event_ids)} events)",
                measurements=tuple(measurements), event_count=len(event_ids),
            )
        ]
        self._write_page(
            filename="timings.html", phase=phase, title="Aggregate", measurements=measurements,
            event_count=len(event_ids), chart_events=aggregate_events, summaries=[(phase, row)], aggregate=True,
        )
        self._write_page(
            filename="timings-events.html", phase=phase, title="Warm-up and events", measurements=measurements,
            event_count=len(event_ids), chart_events=chart_events, summaries=summaries, aggregate=False,
        )
