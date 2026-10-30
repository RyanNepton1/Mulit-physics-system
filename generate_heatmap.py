"""Generate a dependency-free SVG heatmap from a MAGNET Thermal CSV field map."""

from __future__ import annotations

import argparse
import csv
import html
import math
from pathlib import Path


SVG_WIDTH = 1000
SVG_HEIGHT = 760
PLOT_LEFT = 105
PLOT_TOP = 85
PLOT_SIZE = 600


def _interpolate_color(value: float, minimum: float, maximum: float) -> str:
    stops = (
        (0.0, (49, 54, 149)),
        (0.25, (69, 117, 180)),
        (0.5, (116, 173, 209)),
        (0.75, (253, 174, 97)),
        (1.0, (165, 0, 38)),
    )
    ratio = (value - minimum) / (maximum - minimum)
    ratio = min(1.0, max(0.0, ratio))
    for index in range(len(stops) - 1):
        lower_position, lower_color = stops[index]
        upper_position, upper_color = stops[index + 1]
        if ratio <= upper_position:
            portion = (ratio - lower_position) / (upper_position - lower_position)
            channels = tuple(
                round(lower + portion * (upper - lower))
                for lower, upper in zip(lower_color, upper_color)
            )
            return f"#{channels[0]:02x}{channels[1]:02x}{channels[2]:02x}"
    return "#a50026"


def _scan_csv(
    csv_path: Path, time_column: str, x_column: str, y_column: str, value_column: str
) -> tuple[float | None, tuple[float, float, float, float], int]:
    latest_time: float | None = None
    bounds = [math.inf, math.inf, -math.inf, -math.inf]
    row_count = 0
    with csv_path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {csv_path}")
        required = {x_column, y_column, value_column}
        if time_column in reader.fieldnames:
            required.add(time_column)
        missing = required.difference(reader.fieldnames)
        if missing:
            raise ValueError(
                f"CSV is missing required columns: {', '.join(sorted(missing))}"
            )
        has_time = time_column in reader.fieldnames
        for row in reader:
            try:
                x = float(row[x_column])
                y = float(row[y_column])
                value = float(row[value_column])
                raw_time = row[time_column] if has_time else ""
                time_value = float(raw_time) if raw_time else None
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"CSV row {reader.line_num} has a missing or invalid "
                    "coordinate, value, or time"
                ) from exc
            if not all(math.isfinite(item) for item in (x, y, value)):
                raise ValueError(f"CSV row {reader.line_num} has a non-finite value")
            if time_value is not None:
                if not math.isfinite(time_value):
                    raise ValueError(f"CSV row {reader.line_num} has a non-finite time")
                latest_time = (
                    time_value
                    if latest_time is None
                    else max(latest_time, time_value)
                )
            bounds[0] = min(bounds[0], x)
            bounds[1] = min(bounds[1], y)
            bounds[2] = max(bounds[2], x)
            bounds[3] = max(bounds[3], y)
            row_count += 1
    if not row_count:
        raise ValueError(f"CSV contains no field data: {csv_path}")
    return latest_time, (bounds[0], bounds[1], bounds[2], bounds[3]), row_count


def _aggregate_xy(
    csv_path: Path,
    time_column: str,
    requested_time: float | None,
    has_time: bool,
    x_column: str,
    y_column: str,
    value_column: str,
    bounds: tuple[float, float, float, float],
    bins: int,
) -> tuple[dict[tuple[int, int], tuple[float, int]], float]:
    min_x, min_y, max_x, max_y = bounds
    span_x = max_x - min_x or 1.0
    span_y = max_y - min_y or 1.0
    grid: dict[tuple[int, int], tuple[float, int]] = {}
    with csv_path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        for row in reader:
            if has_time:
                raw_time = row[time_column]
                row_time = float(raw_time) if raw_time else None
                if row_time != requested_time:
                    continue
            x = float(row[x_column])
            y = float(row[y_column])
            value = float(row[value_column])
            ix = min(bins - 1, int((x - min_x) / span_x * bins))
            iy = min(bins - 1, int((y - min_y) / span_y * bins))
            total, count = grid.get((ix, iy), (0.0, 0))
            grid[(ix, iy)] = (total + value, count + 1)
    if not grid:
        raise ValueError("No CSV rows match the requested time")
    values = [total / count for total, count in grid.values()]
    return grid, min(values)


def _make_svg(
    csv_path: Path,
    output_path: Path,
    grid: dict[tuple[int, int], tuple[float, int]],
    bounds: tuple[float, float, float, float],
    bins: int,
    time_value: float | None,
    value_column: str,
    minimum: float,
) -> None:
    max_x_value = max(total / count for total, count in grid.values())
    min_x, min_y, max_x, max_y = bounds
    span_x = max_x - min_x or 1.0
    span_y = max_y - min_y or 1.0
    plot_width = PLOT_SIZE
    plot_height = PLOT_SIZE * span_y / span_x
    if plot_height > PLOT_SIZE:
        plot_width = PLOT_SIZE * span_x / span_y
        plot_height = PLOT_SIZE
    left = PLOT_LEFT + (PLOT_SIZE - plot_width) / 2
    top = PLOT_TOP + (PLOT_SIZE - plot_height) / 2
    cell_width = plot_width / bins
    cell_height = plot_height / bins

    title = f"{csv_path.stem} | XY projection"
    time_label = "Static solution" if time_value is None else f"t = {time_value:g} s"
    all_zero = minimum == 0.0 and max_x_value == 0.0
    displayed_minimum = minimum
    displayed_maximum = max_x_value
    if displayed_minimum == displayed_maximum:
        padding = max(abs(displayed_minimum) * 0.01, 0.5)
        minimum = displayed_minimum - padding
        max_x_value = displayed_maximum + padding

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" '
        f'height="{SVG_HEIGHT}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}">',
        "<defs>",
        '<linearGradient id="legend" x1="0" y1="1" x2="0" y2="0">',
        '<stop offset="0%" stop-color="#313695"/>',
        '<stop offset="25%" stop-color="#4575b4"/>',
        '<stop offset="50%" stop-color="#74add1"/>',
        '<stop offset="75%" stop-color="#fdae61"/>',
        '<stop offset="100%" stop-color="#a50026"/>',
        "</linearGradient></defs>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{SVG_WIDTH / 2}" y="32" text-anchor="middle" '
        f'font-family="Arial,sans-serif" font-size="17" font-weight="bold">'
        f"{html.escape(title)}</text>",
        f'<text x="{SVG_WIDTH / 2}" y="57" text-anchor="middle" '
        f'font-family="Arial,sans-serif" font-size="14">'
        f"{html.escape(time_label)}; mean over axial depth</text>",
        f'<rect x="{left:.2f}" y="{top:.2f}" width="{plot_width:.2f}" '
        f'height="{plot_height:.2f}" fill="#f2f2f2"/>',
    ]

    for (ix, iy), (total, count) in grid.items():
        value = total / count
        color = _interpolate_color(value, minimum, max_x_value)
        x = left + ix * cell_width
        y = top + (bins - 1 - iy) * cell_height
        parts.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{cell_width + 0.35:.2f}" '
            f'height="{cell_height + 0.35:.2f}" fill="{color}"/>'
        )

    legend_x = PLOT_LEFT + PLOT_SIZE + 35
    parts.extend(
        [
            f'<rect x="{legend_x}" y="{top:.2f}" width="22" '
            f'height="{plot_height:.2f}" fill="url(#legend)"/>',
            f'<text x="{legend_x + 32}" y="{top + 8:.2f}" '
            f'font-family="Arial,sans-serif" font-size="12">'
            f"{displayed_maximum:.5g}</text>",
            f'<text x="{legend_x + 32}" y="{top + plot_height:.2f}" '
            f'font-family="Arial,sans-serif" font-size="12">'
            f"{displayed_minimum:.5g}</text>",
            f'<text x="{left + plot_width / 2:.2f}" y="{top + plot_height + 38:.2f}" '
            f'text-anchor="middle" font-family="Arial,sans-serif" font-size="13">'
            "x (m)</text>",
            f'<text x="25" y="{top + plot_height / 2:.2f}" '
            f'text-anchor="middle" transform="rotate(-90 25 '
            f'{top + plot_height / 2:.2f})" font-family="Arial,sans-serif" '
            'font-size="13">y (m)</text>',
            f'<text x="{legend_x}" y="{top + plot_height + 30:.2f}" '
            f'font-family="Arial,sans-serif" font-size="12">'
            f"{html.escape(value_column)}</text>",
        ]
    )
    if all_zero:
        parts.append(
            f'<text x="{left + plot_width / 2:.2f}" '
            f'y="{top + plot_height / 2:.2f}" text-anchor="middle" '
            'font-family="Arial,sans-serif" font-size="18" font-weight="bold" '
            'fill="#111111">All values are zero: no heat source is present</text>'
        )
    parts.append("</svg>")
    output_path.write_text("\n".join(parts), encoding="utf-8")


def _heatmap_csv(
    csv_path: Path,
    time: float | None = None,
    bins: int = 250,
    output: Path | None = None,
) -> Path:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as source:
        fieldnames = csv.DictReader(source).fieldnames
    if fieldnames is None:
        raise ValueError(f"CSV has no header: {csv_path}")
    if "heat_source_density_W_per_m3" in fieldnames:
        value_column = "heat_source_density_W_per_m3"
    elif "temperature_celsius" in fieldnames:
        value_column = "temperature_celsius"
    else:
        raise ValueError(
            "CSV must contain heat_source_density_W_per_m3 or "
            "temperature_celsius"
        )
    time_column = "time_s" if "time_s" in fieldnames else ""
    has_time = bool(time_column)
    latest_time, bounds, _row_count = _scan_csv(
        csv_path, time_column, "x_m", "y_m", value_column
    )
    time_value = time if time is not None and latest_time is not None else latest_time
    grid, _grid_minimum = _aggregate_xy(
        csv_path,
        time_column,
        time_value,
        has_time,
        "x_m",
        "y_m",
        value_column,
        bounds,
        bins,
    )
    output_path = (
        output.resolve()
        if output is not None
        else csv_path.with_name(f"{csv_path.stem}_heatmap.svg")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _make_svg(
        csv_path,
        output_path,
        grid,
        bounds,
        bins,
        time_value,
        value_column,
        min(total / count for total, count in grid.values()),
    )
    return output_path


def generate_heatmaps(
    input_path: Path,
    time: float | None = None,
    bins: int = 250,
    output: Path | None = None,
) -> list[Path]:
    """Generate an SVG beside each result CSV in a file or directory tree."""
    input_path = input_path.resolve()
    if not 10 <= bins <= 2000:
        raise ValueError("bins must be between 10 and 2000")
    if time is not None and not math.isfinite(time):
        raise ValueError("time must be finite")
    if input_path.is_file():
        if input_path.suffix.lower() != ".csv":
            raise ValueError(f"Expected a CSV file: {input_path}")
        csv_files = [input_path]
    elif input_path.is_dir():
        csv_files = sorted(
            path
            for path in input_path.rglob("*")
            if path.is_file() and path.suffix.lower() == ".csv"
        )
        if not csv_files:
            raise FileNotFoundError(f"No CSV files found under: {input_path}")
    else:
        raise FileNotFoundError(f"CSV file or folder not found: {input_path}")
    if output is not None and len(csv_files) != 1:
        raise ValueError("An explicit output path can only be used with one CSV file")

    outputs = []
    for index, csv_path in enumerate(csv_files):
        output_path = _heatmap_csv(
            csv_path,
            time=time,
            bins=bins,
            output=output if index == 0 else None,
        )
        outputs.append(output_path)
        print(f"Saved heatmap to: {output_path}")
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Render XY-projected SVG heatmaps from a MAGNET Thermal CSV or "
            "recursively from all CSVs in a folder."
        )
    )
    parser.add_argument(
        "input_path",
        type=Path,
        help="Result CSV file or folder to search recursively for result CSVs",
    )
    parser.add_argument(
        "--time",
        type=float,
        default=None,
        help="Transient time in seconds (default: latest saved time)",
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=250,
        help="Number of grid bins along each axis (default: 250)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output SVG path (only valid when input_path is one CSV file)",
    )
    args = parser.parse_args()
    outputs = generate_heatmaps(
        args.input_path, time=args.time, bins=args.bins, output=args.output
    )
    print(
        f"Generated {len(outputs)} heatmap(s) from {args.input_path}"
    )


if __name__ == "__main__":
    main()
