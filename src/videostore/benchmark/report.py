"""Human-readable and machine-readable benchmark reports (spec section 44)."""
from __future__ import annotations

import csv
import json

from .runner import BenchmarkResult


def write_json(results: list[BenchmarkResult], path: str) -> None:
    with open(path, "w") as f:
        json.dump([r.to_dict() for r in results], f, indent=2)


def write_csv(results: list[BenchmarkResult], path: str) -> None:
    if not results:
        with open(path, "w") as f:
            f.write("")
        return
    fields = list(results[0].to_dict().keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow(r.to_dict())


def _svg_bar_chart(labels: list[str], values: list[float], title: str, unit: str = "") -> str:
    width, height = 640, 40 + 28 * len(labels)
    max_v = max(values) if values and max(values) > 0 else 1.0
    bars = []
    for i, (label, v) in enumerate(zip(labels, values)):
        y = 30 + i * 28
        bar_w = max(2, (v / max_v) * 420)
        bars.append(
            f'<text x="4" y="{y+14}" font-size="12" fill="currentColor">{label}</text>'
            f'<rect x="150" y="{y}" width="{bar_w:.1f}" height="18" fill="#4c8dff" rx="2"/>'
            f'<text x="{150+bar_w+6:.1f}" y="{y+14}" font-size="12" fill="currentColor">{v:.3g}{unit}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:640px">'
        f'<text x="4" y="16" font-size="13" font-weight="bold" fill="currentColor">{title}</text>'
        + "".join(bars)
        + "</svg>"
    )


def write_html(results: list[BenchmarkResult], path: str) -> None:
    def _row(r: BenchmarkResult) -> str:
        psnr_cell = f"{r.psnr_db:.1f}" if r.psnr_db is not None else "n/a"
        return (
            "<tr>"
            f"<td>{r.test_file}</td><td>{r.profile}</td><td>{r.modulation}</td>"
            f"<td>{r.channel}</td><td>{r.resolution}@{r.fps}</td>"
            f"<td>{r.video_duration_seconds:.2f}s</td>"
            f"<td>{r.effective_payload_bitrate_mbps:.3f}</td>"
            f"<td>{'PASS' if r.success else 'FAIL'}</td>"
            f"<td>{r.block_error_rate:.4%}</td>"
            f"<td>{psnr_cell}</td>"
            f"<td>{r.error}</td>"
            "</tr>"
        )

    rows = "".join(_row(r) for r in results)

    # group by (profile, channel) for a capacity-vs-reliability style summary
    by_channel: dict[str, list[BenchmarkResult]] = {}
    for r in results:
        by_channel.setdefault(r.channel, []).append(r)
    success_rate_by_channel = {
        ch: sum(1 for r in rs if r.success) / len(rs) for ch, rs in by_channel.items()
    }
    bitrate_by_channel = {
        ch: sum(r.effective_payload_bitrate_mbps for r in rs) / len(rs) for ch, rs in by_channel.items()
    }

    chart1 = _svg_bar_chart(
        list(success_rate_by_channel.keys()), [v * 100 for v in success_rate_by_channel.values()], "Success rate by channel", "%"
    )
    chart2 = _svg_bar_chart(
        list(bitrate_by_channel.keys()), list(bitrate_by_channel.values()), "Mean effective payload bitrate by channel", " Mbit/s"
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>VideoStore benchmark report</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
td, th {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; }}
th {{ background: #f2f2f2; }}
.charts {{ display: flex; gap: 2rem; flex-wrap: wrap; margin-bottom: 2rem; }}
</style></head>
<body>
<h1>VideoStore benchmark report</h1>
<p>{len(results)} runs. See docs/benchmarking.md for methodology and caveats
(these are LOCAL channel simulations, not verified against a real YouTube upload
unless this run used --youtube-url).</p>
<div class="charts">{chart1}{chart2}</div>
<table>
<tr><th>file</th><th>profile</th><th>modulation</th><th>channel</th><th>res@fps</th>
<th>duration</th><th>payload Mbit/s</th><th>result</th><th>block error rate</th><th>PSNR</th><th>error</th></tr>
{rows}
</table>
</body></html>"""
    with open(path, "w") as f:
        f.write(html)
