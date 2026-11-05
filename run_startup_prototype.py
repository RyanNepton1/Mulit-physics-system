"""Export MotorSolve BLDC torque-speed, thermal-loss, and field data.

Open the .moto design in Simcenter MotorSolve 2212, then run:

    python run_startup_prototype.py --project "C:\\path\\to\\testMotor.moto"

Requires Windows and pywin32. Field-result settings are updated in memory;
the source design is not saved.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MOTORSOLVE_VERSION_PREFIX = "2212"
TORQUE_SPEED_RESULT_PATH = (
    "Motor Results",
    "Performance Charts",
    "Torque vs. speed",
)
THERMAL_LOSS_RESULT_PATH = (
    "Thermal Charts",
    "Losses for thermal analysis",
)
TORQUE_SPEED_REQUIRED_HEADERS = ("rotor speed", "torque")
THERMAL_LOSS_REQUIRED_HEADERS = ("loss",)
INSTANTANEOUS_FIELD_RESULT_PATH = ("Fields", "Instantaneous fields")
FIELD_ARC_PROBE_RESULT_PATH = ("Field Charts & Tables", "Field arc probe")
DEFAULT_FIELD_ARC_RADIUS_MM = 20.9
DEFAULT_FIELD_ARC_SAMPLES = 721
MIN_FIELD_ARC_SAMPLES = 2
MAX_FIELD_ARC_SAMPLES = 3601


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return list(value)
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (tuple, list)):
        return [_serialize_value(item) for item in value]

    if hasattr(value, "_oleobj_"):
        serialized: dict[str, Any] = {}
        for attribute in ("Name", "id", "Type", "values"):
            try:
                item = getattr(value, attribute)
            except AttributeError:
                continue
            serialized[attribute.lower()] = _serialize_value(item)
        if serialized:
            return serialized
        raise TypeError(
            "MotorSolve returned an unsupported COM value while capturing "
            f"parameters: {type(value).__name__}"
        )

    raise TypeError(
        f"Unsupported MotorSolve parameter value type: {type(value).__name__}"
    )


def _parameter_record(parameter: Any, part_name: str | None = None) -> dict[str, Any]:
    record = {
        "name": str(parameter.Name),
        "quantity": str(parameter.Quantity),
        "unit": str(parameter.Unit),
        "value": _serialize_value(parameter.Value),
    }
    if part_name is not None:
        record["part"] = part_name
    return record


def _capture_design_parameters(design: Any) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    for part in _items(design.Parts):
        part_name = str(part.Name)
        for parameter in _items(part.ParameterManager.Parameters):
            parameters.append(_parameter_record(parameter, part_name))
    if not parameters:
        raise RuntimeError(f"No parameters were found in design {design.Name!r}")
    return parameters


def _capture_result_parameters(result: Any) -> list[dict[str, Any]]:
    parameters = [
        _parameter_record(parameter)
        for parameter in _items(result.ParameterManager.Parameters)
    ]
    if not parameters:
        raise RuntimeError(
            f"No analysis settings were found for result {result.Name!r}"
        )
    return parameters


def _get_named_child(items: Iterable[Any], name: str) -> Any:
    matches = [item for item in _items(items) if str(item.Name) == name]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one MotorSolve item named {name!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _find_result(motor: Any, result_path: tuple[str, ...]) -> Any:
    current: Any = motor.ResultManager
    for name in result_path:
        collection = current.Results
        current = _get_named_child(collection, name)
    return current


def _selected_designs(result: Any) -> list[str]:
    parameter = result.ParameterManager.GetParameterByName("Designs")
    selected: list[str] = []
    for choice in _items(parameter.Value):
        if bool(choice.Checked):
            selected.append(str(choice.Name))
    return selected


def _export_chart(
    app: Any,
    result: Any,
    design_name: str,
    output_path: Path,
    required_headers: tuple[str, ...],
) -> dict[str, Any]:
    selected = _selected_designs(result)
    if design_name not in selected:
        raise RuntimeError(
            f"{result.Name!r} is configured for designs {selected!r}, not "
            f"{design_name!r}. Select the requested design in MotorSolve and "
            "try again."
        )

    started = time.perf_counter()
    app.ChartView.Clear()
    result.View()
    elapsed_seconds = time.perf_counter() - started

    chart_data = app.ChartView.Data
    headers = [str(header) for header in _items(chart_data.ColumnHeaders)]
    values = _items(chart_data.values)
    if not headers or not values:
        raise RuntimeError(
            f"MotorSolve returned no chart data for result {result.Name!r}"
        )

    normalized_headers = " ".join(headers).casefold()
    missing_headers = [
        header for header in required_headers if header not in normalized_headers
    ]
    if missing_headers:
        raise RuntimeError(
            f"Unexpected columns in {result.Name!r}: {headers!r}; "
            f"expected columns containing {missing_headers!r}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    app.ChartView.Data.Export(
        str(output_path),
        app.Constants.TableExportFormat_CSV,
    )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(
            f"MotorSolve did not create chart CSV: {output_path}"
        )
    with output_path.open("r", newline="", encoding="utf-8-sig") as source:
        rows = list(csv.reader(source))
    if len(rows) < 2:
        raise RuntimeError(
            f"MotorSolve exported a chart without data rows: {output_path}"
        )

    return {
        "result": str(result.Name),
        "designs": selected,
        "headers": headers,
        "data_rows": len(rows) - 1,
        "elapsed_seconds": elapsed_seconds,
        "csv": output_path.name,
    }


def _set_choice_parameter(result: Any, name: str, choice_name: str) -> None:
    parameter = result.ParameterManager.GetParameterByName(name)
    parameter.SetValue(choice_name)
    current_value = parameter.Value
    if str(current_value.Name) != choice_name or not parameter.IsValid:
        raise RuntimeError(
            f"Could not set {result.Name!r} parameter {name!r} to "
            f"{choice_name!r}"
        )


def _set_numeric_parameter(result: Any, name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    parameter = result.ParameterManager.GetParameterByName(name)
    parameter.SetValue(format(value, ".12g"))
    if not parameter.IsValid or not math.isclose(
        float(parameter.Value),
        value,
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        raise RuntimeError(
            f"Could not set {result.Name!r} parameter {name!r} to {value}"
        )


def _validate_field_arc_samples(sample_count: int) -> None:
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < MIN_FIELD_ARC_SAMPLES
        or sample_count > MAX_FIELD_ARC_SAMPLES
    ):
        raise ValueError(
            f"field arc samples must be an integer between "
            f"{MIN_FIELD_ARC_SAMPLES} and {MAX_FIELD_ARC_SAMPLES}"
        )


def _field_arc_samples(
    radius_mm: float,
    center_z_mm: float,
    start_angle_deg: float,
    end_angle_deg: float,
    sample_count: int,
) -> list[tuple[float, float, float, float]]:
    _validate_field_arc_samples(sample_count)
    if not math.isfinite(radius_mm) or radius_mm <= 0:
        raise ValueError("field arc radius must be a finite positive number")
    if not all(
        math.isfinite(value)
        for value in (center_z_mm, start_angle_deg, end_angle_deg)
    ):
        raise ValueError("field arc coordinates and angles must be finite")
    if start_angle_deg == end_angle_deg:
        raise ValueError("field arc start and end angles must differ")

    samples = []
    for index in range(sample_count):
        angle_deg = start_angle_deg + (
            (end_angle_deg - start_angle_deg) * index / (sample_count - 1)
        )
        angle_rad = math.radians(angle_deg)
        samples.append(
            (
                round(angle_deg, 6),
                round(radius_mm * math.cos(angle_rad), 6),
                round(radius_mm * math.sin(angle_rad), 6),
                round(center_z_mm, 6),
            )
        )
    return samples


def _write_instantaneous_field_arc_csv(
    raw_csv_path: Path,
    output_path: Path,
    requested_samples: list[tuple[float, float, float, float]],
) -> int:
    with raw_csv_path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.reader(source)
        header = next(reader, None)
        if header is None:
            raise RuntimeError("MotorSolve field-arc CSV has no header")

        angle_columns = [
            index
            for index, name in enumerate(header)
            if "angle" in name.casefold()
        ]
        value_columns = [
            index
            for index, name in enumerate(header)
            if "value" in name.casefold()
        ]
        if len(angle_columns) != 1 or len(value_columns) != 1:
            raise RuntimeError(
                "Could not identify the angle and flux-density columns in "
                f"MotorSolve field-arc CSV: {header!r}"
            )
        angle_column = angle_columns[0]
        value_column = value_columns[0]
        values_by_angle: dict[float, str] = {}
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) <= max(angle_column, value_column):
                raise RuntimeError(
                    "MotorSolve returned an incomplete field-arc row: "
                    f"{row!r}"
                )
            try:
                angle = round(float(row[angle_column]), 6)
            except ValueError as exc:
                raise RuntimeError(
                    "MotorSolve returned an invalid field-arc angle: "
                    f"{row[angle_column]!r}"
                ) from exc
            if angle in values_by_angle:
                raise RuntimeError(
                    f"MotorSolve returned duplicate field-arc angle {angle}"
                )
            values_by_angle[angle] = row[value_column].strip()

    requested_angles = {sample[0] for sample in requested_samples}
    unexpected_angles = set(values_by_angle).difference(requested_angles)
    if unexpected_angles:
        raise RuntimeError(
            "MotorSolve returned field-arc angles that were not requested: "
            f"{sorted(unexpected_angles)[:5]!r}"
        )
    sampled_count = len(values_by_angle)
    if sampled_count == 0:
        raise RuntimeError("MotorSolve returned no instantaneous arc values")

    with output_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(
            (
                "angle_deg",
                "x_mm",
                "y_mm",
                "z_mm",
                "flux_density_T",
                "sampled",
            )
        )
        writer.writerows(
            (
                angle_deg,
                x_mm,
                y_mm,
                z_mm,
                values_by_angle.get(angle_deg, ""),
                str(angle_deg in values_by_angle).lower(),
            )
            for angle_deg, x_mm, y_mm, z_mm in requested_samples
        )
    return sampled_count


def _export_instantaneous_field(
    app: Any,
    motor: Any,
    design: Any,
    sample_dir: Path,
    arc_radius_mm: float,
    arc_samples: int,
    center_z_mm: float,
    start_angle_deg: float,
    end_angle_deg: float,
    current_percent: float,
    advance_angle_deg: float,
    rotor_position_deg: float,
) -> dict[str, Any]:
    field_result = _find_result(motor, INSTANTANEOUS_FIELD_RESULT_PATH)
    probe_result = _find_result(motor, FIELD_ARC_PROBE_RESULT_PATH)

    for parameter_name, value in (
        ("Design", str(design.Name)),
        ("Thermal state", "Ambient temperature"),
        ("Contour plot field", "Flux density"),
        ("Shaded plot field", "Flux density"),
        ("Current input method", "% of rated current"),
    ):
        _set_choice_parameter(field_result, parameter_name, value)
    for parameter_name, value in (
        ("Peak line current percentage", current_percent),
        ("Advance angle", advance_angle_deg),
        ("Rotor position", rotor_position_deg),
    ):
        _set_numeric_parameter(field_result, parameter_name, value)

    requested_samples = _field_arc_samples(
        arc_radius_mm,
        center_z_mm,
        start_angle_deg,
        end_angle_deg,
        arc_samples,
    )
    for parameter_name, value in (
        ("Radius", arc_radius_mm),
        ("Start angle", start_angle_deg),
        ("End angle", end_angle_deg),
        ("Number of sample points", float(arc_samples)),
    ):
        _set_numeric_parameter(probe_result, parameter_name, value)
    center_parameter = probe_result.ParameterManager.GetParameterByName(
        "Center point"
    )
    center_parameter.SetValue(f"0 0 {center_z_mm:.12g}")
    accepted_center = _items(center_parameter.Value)
    expected_center = (0.0, 0.0, center_z_mm)
    if len(accepted_center) != 3 or any(
        not math.isclose(float(actual), expected, abs_tol=1e-9)
        for actual, expected in zip(accepted_center, expected_center)
    ):
        raise RuntimeError(
            "MotorSolve did not accept the requested field-arc center point"
        )
    _set_choice_parameter(probe_result, "Exchange X and Y axes", "No")

    started = time.perf_counter()
    app.ChartView.Clear()
    field_result.View()
    probe_result.View()
    elapsed_seconds = time.perf_counter() - started

    chart_data = app.ChartView.Data
    output_path = sample_dir / "instantaneous_flux_density.csv"
    with tempfile.TemporaryDirectory(prefix="motorsolve-field-") as temp_dir:
        raw_csv_path = Path(temp_dir) / "field_probe.csv"
        chart_data.Export(
            str(raw_csv_path),
            app.Constants.TableExportFormat_CSV,
        )
        if not raw_csv_path.is_file() or raw_csv_path.stat().st_size == 0:
            raise RuntimeError(
                "MotorSolve did not export instantaneous field-arc data"
            )
        sampled_count = _write_instantaneous_field_arc_csv(
            raw_csv_path,
            output_path,
            requested_samples,
        )

    return {
        "result": str(field_result.Name),
        "probe_result": str(probe_result.Name),
        "field": "Flux density",
        "value": "Magnitude",
        "unit": "T",
        "design": str(design.Name),
        "thermal_state": "Ambient temperature",
        "current_input_method": "% of rated current",
        "peak_line_current_percentage": current_percent,
        "advance_angle_deg": advance_angle_deg,
        "rotor_position_deg": rotor_position_deg,
        "probe_type": "circular arc",
        "arc_radius_mm": arc_radius_mm,
        "arc_center_mm": [0.0, 0.0, center_z_mm],
        "start_angle_deg": start_angle_deg,
        "end_angle_deg": end_angle_deg,
        "arc_samples": arc_samples,
        "angular_spacing_deg": round(
            (end_angle_deg - start_angle_deg) / (arc_samples - 1),
            6,
        ),
        "requested_points": len(requested_samples),
        "sampled_points": sampled_count,
        "unsampled_points": len(requested_samples) - sampled_count,
        "elapsed_seconds": elapsed_seconds,
        "csv": output_path.name,
    }


def _next_sample_dir(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    sample_numbers = []
    for child in output_root.iterdir():
        match = re.fullmatch(r"sample_(\d{4,})", child.name)
        if child.is_dir() and match:
            sample_numbers.append(int(match.group(1)))
    number = max(sample_numbers, default=0) + 1
    while True:
        sample_dir = output_root / f"sample_{number:04d}"
        try:
            sample_dir.mkdir()
        except FileExistsError:
            number += 1
            continue
        return sample_dir


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _attach_to_motorsolve() -> tuple[Any, Any, Any]:
    if os.name != "nt":
        raise RuntimeError("MotorSolve COM automation is available only on Windows")
    try:
        import pythoncom
        import pywintypes
        from win32com.client import GetActiveObject
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is required. Install it with: "
            "python -m pip install pywin32"
        ) from exc

    pythoncom.CoInitialize()
    try:
        app = GetActiveObject("MotorSolve.Application")
    except pywintypes.com_error as exc:
        pythoncom.CoUninitialize()
        raise RuntimeError(
            "Could not attach to MotorSolve. Open Simcenter MotorSolve 2212 "
            "and the supplied .moto design, then rerun this script."
        ) from exc
    return pythoncom, pywintypes, app


def run(
    project_path: Path,
    output_root: Path,
    design_name: str | None = None,
    field_arc_radius_mm: float = DEFAULT_FIELD_ARC_RADIUS_MM,
    field_arc_samples: int = DEFAULT_FIELD_ARC_SAMPLES,
    field_arc_center_z_mm: float = 0.0,
    field_arc_start_angle_deg: float = 0.0,
    field_arc_end_angle_deg: float = 360.0,
    field_current_percent: float = 100.0,
    field_advance_angle_deg: float = 0.0,
    field_rotor_position_deg: float = 0.0,
) -> Path:
    project_path = project_path.resolve()
    if not project_path.is_file():
        raise FileNotFoundError(f"MotorSolve project not found: {project_path}")
    if project_path.suffix.lower() != ".moto":
        raise ValueError(f"Expected a MotorSolve .moto project: {project_path}")

    pythoncom, pywintypes, app = _attach_to_motorsolve()
    sample_dir: Path | None = None
    metadata: dict[str, Any] = {
        "status": "running",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "project": str(project_path),
    }
    try:
        version = str(app.Version)
        metadata["motorsolve_version"] = version
        if not version.startswith(f"{MOTORSOLVE_VERSION_PREFIX}."):
            raise RuntimeError(
                f"Connected to MotorSolve {version}; this workflow targets "
                f"MotorSolve {MOTORSOLVE_VERSION_PREFIX}. Open the 2212 "
                "instance and retry."
            )

        motor = app.Motor
        active_project = Path(str(motor.filename)).resolve()
        if os.path.normcase(str(active_project)) != os.path.normcase(
            str(project_path)
        ):
            raise RuntimeError(
                f"MotorSolve currently has {active_project} open, not "
                f"{project_path}. Open the requested project in MotorSolve "
                "before running this script."
            )

        designs = _items(motor.DesignManager.Designs)
        if design_name is None:
            if len(designs) != 1:
                raise RuntimeError(
                    "The project must contain exactly one design unless "
                    "--design-name is supplied."
                )
            design = designs[0]
            design_name = str(design.Name)
        else:
            design = _get_named_child(designs, design_name)

        metadata["design_name"] = design_name
        metadata["design_parameters"] = _capture_design_parameters(design)

        torque_result = _find_result(motor, TORQUE_SPEED_RESULT_PATH)
        loss_result = _find_result(motor, THERMAL_LOSS_RESULT_PATH)
        metadata["analysis_parameters"] = {
            "torque_speed": _capture_result_parameters(torque_result),
            "thermal_losses": _capture_result_parameters(loss_result),
        }

        _validate_field_arc_samples(field_arc_samples)
        for name, value in (
            ("field arc radius", field_arc_radius_mm),
            ("field arc center Z", field_arc_center_z_mm),
            ("field arc start angle", field_arc_start_angle_deg),
            ("field arc end angle", field_arc_end_angle_deg),
            ("field current percentage", field_current_percent),
            ("field advance angle", field_advance_angle_deg),
            ("field rotor position", field_rotor_position_deg),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if field_arc_radius_mm <= 0:
            raise ValueError("field arc radius must be positive")
        if field_arc_start_angle_deg == field_arc_end_angle_deg:
            raise ValueError("field arc start and end angles must differ")

        sample_dir = _next_sample_dir(output_root.resolve())
        metadata["sample_dir"] = str(sample_dir)
        parameters_path = sample_dir / "parameters.json"
        parameter_manifest = {
            "project": str(project_path),
            "design_name": design_name,
            "design_parameters": metadata["design_parameters"],
            "analysis_parameters": metadata["analysis_parameters"],
        }
        _write_json(parameters_path, parameter_manifest)

        metadata["torque_speed"] = _export_chart(
            app,
            torque_result,
            design_name,
            sample_dir / "torque_speed.csv",
            TORQUE_SPEED_REQUIRED_HEADERS,
        )
        metadata["thermal_losses"] = _export_chart(
            app,
            loss_result,
            design_name,
            sample_dir / "thermal_losses.csv",
            THERMAL_LOSS_REQUIRED_HEADERS,
        )
        metadata["instantaneous_field"] = _export_instantaneous_field(
            app,
            motor,
            design,
            sample_dir,
            field_arc_radius_mm,
            field_arc_samples,
            field_arc_center_z_mm,
            field_arc_start_angle_deg,
            field_arc_end_angle_deg,
            field_current_percent,
            field_advance_angle_deg,
            field_rotor_position_deg,
        )
        parameter_manifest["analysis_parameters"]["instantaneous_field"] = (
            _capture_result_parameters(
                _find_result(motor, INSTANTANEOUS_FIELD_RESULT_PATH)
            )
        )
        parameter_manifest["analysis_parameters"]["field_arc_probe"] = (
            _capture_result_parameters(
                _find_result(motor, FIELD_ARC_PROBE_RESULT_PATH)
            )
        )
        _write_json(parameters_path, parameter_manifest)
        metadata["status"] = "completed"
        _write_json(sample_dir / "metadata.json", metadata)
        return sample_dir
    except pywintypes.com_error as exc:
        metadata["status"] = "failed"
        metadata["error"] = str(exc)
        if sample_dir is not None:
            _write_json(sample_dir / "metadata.json", metadata)
        raise RuntimeError(
            "MotorSolve reported an automation error while generating the "
            f"sample: {exc}"
        ) from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        metadata["status"] = "failed"
        metadata["error"] = str(exc)
        if sample_dir is not None:
            _write_json(sample_dir / "metadata.json", metadata)
        raise
    finally:
        pythoncom.CoUninitialize()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        type=Path,
        required=True,
        help="MotorSolve .moto project already open in MotorSolve 2212",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.cwd() / "motorsolve_samples",
        help="Directory for incrementally numbered result samples",
    )
    parser.add_argument(
        "--design-name",
        default=None,
        help="Design to simulate (defaults to the only design in the project)",
    )
    parser.add_argument(
        "--field-arc-radius-mm",
        type=float,
        default=DEFAULT_FIELD_ARC_RADIUS_MM,
        help="Radius of the air-gap field arc in millimeters (default: 20.9)",
    )
    parser.add_argument(
        "--field-arc-samples",
        type=int,
        default=DEFAULT_FIELD_ARC_SAMPLES,
        help=(
            "Number of samples along the field arc "
            f"({MIN_FIELD_ARC_SAMPLES}-{MAX_FIELD_ARC_SAMPLES}, "
            f"default: {DEFAULT_FIELD_ARC_SAMPLES})"
        ),
    )
    parser.add_argument(
        "--field-arc-center-z-mm",
        type=float,
        default=0.0,
        help="Axial coordinate of the circular arc center in millimeters",
    )
    parser.add_argument(
        "--field-arc-start-angle-deg",
        type=float,
        default=0.0,
        help="Start angle for the field arc in degrees",
    )
    parser.add_argument(
        "--field-arc-end-angle-deg",
        type=float,
        default=360.0,
        help="End angle for the field arc in degrees",
    )
    parser.add_argument(
        "--field-current-percent",
        type=float,
        default=100.0,
        help="Peak line current as a percent of rated current",
    )
    parser.add_argument(
        "--field-advance-angle-deg",
        type=float,
        default=0.0,
        help="Instantaneous-field current advance angle in degrees",
    )
    parser.add_argument(
        "--field-rotor-position-deg",
        type=float,
        default=0.0,
        help="Rotor position for the instantaneous field snapshot in degrees",
    )
    args = parser.parse_args()

    sample_dir = run(
        args.project,
        args.output_root,
        args.design_name,
        args.field_arc_radius_mm,
        args.field_arc_samples,
        args.field_arc_center_z_mm,
        args.field_arc_start_angle_deg,
        args.field_arc_end_angle_deg,
        args.field_current_percent,
        args.field_advance_angle_deg,
        args.field_rotor_position_deg,
    )
    print(f"Saved MotorSolve sample to: {sample_dir}")
    print(f"  Torque-speed curve: {sample_dir / 'torque_speed.csv'}")
    print(f"  Thermal losses:     {sample_dir / 'thermal_losses.csv'}")
    print(
        "  Instantaneous field: "
        f"{sample_dir / 'instantaneous_flux_density.csv'}"
    )
    print(f"  Inputs/metadata:    {sample_dir / 'parameters.json'}")


if __name__ == "__main__":
    main()
