"""Add a parameterized outer-rotor motor to a coupled MAGNET Thermal template.

Requires Windows, Python 3.9+, Simcenter MAGNET Thermal 2212, and pywin32:
    python -m pip install pywin32
    python create_motor.py --template "C:\\models\\coupled_template.tn"

Each run starts a visible ThermNet instance, opens the supplied coupled
template, adds the motor geometry with alternating radial magnet polarity, and
writes a design plus valid coupled 2D/3D static/transient solutions to the next
motor_samples\\sample_#### folder. The completed design remains open in
ThermNet. Existing sample folders and ThermNet application work files are not
removed.

Edit MotorParameters below to change dimensions and material-library names.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path


RADIAL_INWARD_DIRECTION_INDEX = 2
RADIAL_OUTWARD_DIRECTION_INDEX = 3
MATERIAL_DIRECTION_OPTION_COUNT = 7
FIXED_INTERVAL_TIME_STEP_METHOD = 1
STATIC_COUPLED_METHOD = "SolveMagneticFirst"
TRANSIENT_COUPLED_METHOD = "AtEachTimeStep"


@dataclass(frozen=True)
class MotorParameters:
    slots: int = 12
    poles: int = 6
    time_steps: int = 10
    axial_length_mm: float = 50.0
    bore_radius_mm: float = 13.0
    stator_back_iron_radius_mm: float = 26.0
    stator_tip_radius_mm: float = 35.0
    coil_inner_radius_mm: float = 28.0
    coil_outer_radius_mm: float = 33.0
    air_gap_mm: float = 2.2
    magnet_thickness_mm: float = 3.0
    rotor_yoke_thickness_mm: float = 6.0
    steel_material: str = "M-19 29 Ga"
    copper_material: str = "Copper: 5.77e7 Siemens/meter"
    magnet_material: str = "N35"

    def validate(self) -> None:
        if self.slots < 3 or self.slots % 3:
            raise ValueError("slots must be a positive multiple of 3")
        if self.poles < 2 or self.poles % 2:
            raise ValueError("poles must be a positive even number")
        if (
            isinstance(self.time_steps, bool)
            or not isinstance(self.time_steps, int)
            or self.time_steps < 1
        ):
            raise ValueError("time_steps must be a positive integer")
        if self.axial_length_mm <= 0:
            raise ValueError("axial_length_mm must be positive")
        if not (
            0 < self.bore_radius_mm
            < self.stator_back_iron_radius_mm
            < self.stator_tip_radius_mm
        ):
            raise ValueError("Stator radii must be positive and strictly increasing")
        if not (
            self.stator_back_iron_radius_mm
            < self.coil_inner_radius_mm
            < self.coil_outer_radius_mm
            < self.stator_tip_radius_mm
        ):
            raise ValueError(
                "Coil radii must fit between the back iron and stator tip"
            )
        if self.air_gap_mm <= 0 or self.magnet_thickness_mm <= 0:
            raise ValueError("Air gap and magnet thickness must be positive")
        if self.rotor_yoke_thickness_mm <= 0:
            raise ValueError("rotor_yoke_thickness_mm must be positive")
        for material in (
            self.steel_material,
            self.copper_material,
            self.magnet_material,
        ):
            if not material or any(char in material for char in ('"', "\r", "\n")):
                raise ValueError("Material names must be nonempty and contain no quotes")

    @property
    def magnet_inner_radius_mm(self) -> float:
        return self.stator_tip_radius_mm + self.air_gap_mm

    @property
    def magnet_outer_radius_mm(self) -> float:
        return self.magnet_inner_radius_mm + self.magnet_thickness_mm

    @property
    def rotor_outer_radius_mm(self) -> float:
        return self.magnet_outer_radius_mm + self.rotor_yoke_thickness_mm


def _n(value: float) -> str:
    return format(value, ".10g")


def _draw_circle(app, radius_mm: float) -> None:
    app.processCommand("REDIM arcs(1)")
    app.processCommand(
        "Call getDocument.getView.newCircle("
        f"0, 0, {_n(radius_mm)}, arcs(0), arcs(1))"
    )


def _draw_arc(
    app, radius_mm: float, start_angle: float, end_angle: float
) -> None:
    cx = cy = 0.0
    sx = radius_mm * math.cos(start_angle)
    sy = radius_mm * math.sin(start_angle)
    ex = radius_mm * math.cos(end_angle)
    ey = radius_mm * math.sin(end_angle)
    app.processCommand("REDIM arc(0)")
    app.processCommand(
        "Call getDocument.getView.newArc("
        f"{_n(cx)}, {_n(cy)}, {_n(sx)}, {_n(sy)}, "
        f"{_n(ex)}, {_n(ey)}, arc)"
    )


def _draw_line(app, start: tuple[float, float], end: tuple[float, float]) -> None:
    app.processCommand("REDIM line(0)")
    app.processCommand(
        "Call getDocument.getView.newLine("
        f"{_n(start[0])}, {_n(start[1])}, "
        f"{_n(end[0])}, {_n(end[1])}, line)"
    )


def _draw_annular_sector(
    app, inner_radius: float, outer_radius: float, start_angle: float, end_angle: float
) -> None:
    _draw_arc(app, outer_radius, start_angle, end_angle)
    _draw_arc(app, inner_radius, end_angle, start_angle)
    _draw_line(
        app,
        (outer_radius * math.cos(end_angle), outer_radius * math.sin(end_angle)),
        (inner_radius * math.cos(end_angle), inner_radius * math.sin(end_angle)),
    )
    _draw_line(
        app,
        (inner_radius * math.cos(start_angle), inner_radius * math.sin(start_angle)),
        (outer_radius * math.cos(start_angle), outer_radius * math.sin(start_angle)),
    )


def _make_component_with_direction(
    app, command: str, thermnet_pid: int, direction_index: int
) -> None:
    import win32con
    import win32gui
    import win32process

    failure: list[RuntimeError] = []
    completed = threading.Event()
    stop_watcher = threading.Event()

    def handle_direction_dialog() -> None:
        try:
            deadline = time.monotonic() + 15.0
            dialog_handle = None
            while time.monotonic() < deadline and not stop_watcher.is_set():
                matches: list[int] = []

                def find_dialog(hwnd, _extra) -> bool:
                    if (
                        win32gui.GetWindowText(hwnd) == "Set Material Direction"
                        and win32process.GetWindowThreadProcessId(hwnd)[1]
                        == thermnet_pid
                    ):
                        matches.append(hwnd)
                    return True

                win32gui.EnumWindows(find_dialog, None)
                if matches:
                    dialog_handle = matches[0]
                    break
                time.sleep(0.025)

            if stop_watcher.is_set():
                return
            if dialog_handle is None:
                raise RuntimeError(
                    "ThermNet did not open the expected material-direction "
                    "dialog for a permanent magnet."
                )

            controls: list[int] = []

            def find_type_combo(hwnd, _extra) -> bool:
                if (
                    win32gui.GetClassName(hwnd) == "ComboBox"
                    and win32gui.SendMessage(
                        hwnd, win32con.CB_GETCOUNT, 0, 0
                    )
                    == MATERIAL_DIRECTION_OPTION_COUNT
                ):
                    controls.append(hwnd)
                return True

            win32gui.EnumChildWindows(dialog_handle, find_type_combo, None)
            if not controls:
                _dismiss_direction_dialog(win32gui, win32con, dialog_handle)
                raise RuntimeError(
                    "Could not identify ThermNet's material-direction type "
                    "selector; the dialog was canceled."
                )

            combo_handle = controls[0]
            expected_direction = {
                RADIAL_INWARD_DIRECTION_INDEX: "Radially Inwards (Cylindrical)",
                RADIAL_OUTWARD_DIRECTION_INDEX: "Radially Outwards (Cylindrical)",
            }.get(direction_index)
            if expected_direction is None:
                _dismiss_direction_dialog(win32gui, win32con, dialog_handle)
                raise ValueError(f"Unsupported material direction index: {direction_index}")

            send_message = ctypes.WinDLL("user32", use_last_error=True).SendMessageW
            send_message.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_size_t,
                ctypes.c_void_p,
            ]
            send_message.restype = ctypes.c_ssize_t
            item_text = ctypes.create_unicode_buffer(128)
            text_length = send_message(
                combo_handle,
                win32con.CB_GETLBTEXT,
                direction_index,
                ctypes.cast(item_text, ctypes.c_void_p),
            )
            if text_length < 0 or item_text.value != expected_direction:
                _dismiss_direction_dialog(win32gui, win32con, dialog_handle)
                raise RuntimeError(
                    "ThermNet's material-direction choices do not match the "
                    "expected radial options; the dialog was canceled."
                )

            selected = win32gui.SendMessage(
                combo_handle, win32con.CB_SETCURSEL, direction_index, 0
            )
            if selected != direction_index:
                _dismiss_direction_dialog(win32gui, win32con, dialog_handle)
                raise RuntimeError(
                    "ThermNet rejected the requested radial magnet direction; "
                    "the dialog was canceled."
                )

            ok_buttons: list[int] = []

            def find_ok_button(hwnd, _extra) -> bool:
                if (
                    win32gui.GetClassName(hwnd) == "Button"
                    and win32gui.GetDlgCtrlID(hwnd) == win32con.IDOK
                ):
                    ok_buttons.append(hwnd)
                return True

            win32gui.EnumChildWindows(dialog_handle, find_ok_button, None)
            if not ok_buttons:
                _dismiss_direction_dialog(win32gui, win32con, dialog_handle)
                raise RuntimeError(
                    "Could not identify the OK button in ThermNet's "
                    "material-direction dialog; the dialog was canceled."
                )

            win32gui.SendMessage(ok_buttons[0], win32con.BM_CLICK, 0, 0)
            close_deadline = time.monotonic() + 2.0
            while (
                win32gui.IsWindow(dialog_handle)
                and time.monotonic() < close_deadline
            ):
                time.sleep(0.025)
            if win32gui.IsWindow(dialog_handle):
                raise RuntimeError(
                    "ThermNet did not close the material-direction dialog "
                    "after applying the radial direction."
                )
        except Exception as exc:
            failure.append(
                RuntimeError(f"Could not set a permanent-magnet direction: {exc}")
            )
        finally:
            completed.set()

    worker = threading.Thread(target=handle_direction_dialog, daemon=True)
    worker.start()
    try:
        app.processCommand(command)
    except Exception:
        stop_watcher.set()
        worker.join(timeout=1.0)
        raise
    if not completed.wait(16.0):
        stop_watcher.set()
        raise RuntimeError(
            "Timed out while assigning a permanent-magnet direction in ThermNet."
        )
    worker.join()
    if failure:
        raise failure[0]


def _dismiss_direction_dialog(win32gui, win32con, dialog_handle: int) -> None:
    cancel_buttons: list[int] = []

    def find_cancel_button(hwnd, _extra) -> bool:
        if (
            win32gui.GetClassName(hwnd) == "Button"
            and win32gui.GetDlgCtrlID(hwnd) == win32con.IDCANCEL
        ):
            cancel_buttons.append(hwnd)
        return True

    win32gui.EnumChildWindows(dialog_handle, find_cancel_button, None)
    if cancel_buttons:
        win32gui.SendMessage(cancel_buttons[0], win32con.BM_CLICK, 0, 0)


def _make_component(
    app,
    name: str,
    material: str,
    interior_point: tuple[float, float],
    axial_length_mm: float,
    thermnet_pid: int | None = None,
    material_direction_index: int | None = None,
) -> None:
    x, y = interior_point
    app.processCommand(
        "Call getDocument.getView.selectAt("
        f"{_n(x)}, {_n(y)}, infoSetSelection, Array(infoSliceSurface))"
    )
    app.processCommand("REDIM nmArray(0)")
    app.processCommand(f'nmArray(0) = "{name}"')
    command = (
        "ret = getDocument.getView.makeComponentInALine("
        f"{_n(axial_length_mm)}, nmArray, "
        f'"Name={material}", infoMakeComponentRemoveVertices)'
    )
    if material_direction_index is None:
        app.processCommand(command)
    else:
        if thermnet_pid is None:
            raise ValueError("ThermNet process ID is required for magnet direction")
        _make_component_with_direction(
            app, command, thermnet_pid, material_direction_index
        )
    app.processCommand("Call setvariant(0, ret)")
    status = app.getVariant(0)
    if status != 0:
        raise RuntimeError(
            f"MAGNET could not create component {name!r} "
            f"(makeComponentInALine returned {status!r})"
        )


def create_motor(app, params: MotorParameters, thermnet_pid: int) -> None:
    params.validate()
    app.processCommand(
        'Call getDocument().setDefaultLengthUnit("millimeters", False)'
    )
    app.processCommand(
        'Call getDocument().setParameter("", "TimeSteps", '
        f'"{params.time_steps}", infoNumberParameter)'
    )

    length = params.axial_length_mm
    pitch = 2.0 * math.pi / params.slots

    # Stator back iron.
    _draw_circle(app, params.bore_radius_mm)
    _draw_circle(app, params.stator_back_iron_radius_mm)
    _make_component(
        app,
        "Stator_Back_Iron",
        params.steel_material,
        ((params.bore_radius_mm + params.stator_back_iron_radius_mm) / 2.0, 0.0),
        length,
    )

    # Twelve radial teeth, with two copper conductors in each slot.
    tooth_fraction = 0.42
    for slot in range(params.slots):
        slot_center = slot * pitch
        tooth_center = slot_center + pitch / 2.0
        tooth_half_angle = tooth_fraction * pitch / 2.0
        _draw_annular_sector(
            app,
            params.stator_back_iron_radius_mm,
            params.stator_tip_radius_mm,
            tooth_center - tooth_half_angle,
            tooth_center + tooth_half_angle,
        )
        tooth_mid_radius = (
            params.stator_back_iron_radius_mm + params.stator_tip_radius_mm
        ) / 2.0
        _make_component(
            app,
            f"Stator_Tooth_{slot + 1:02d}",
            params.steel_material,
            (
                tooth_mid_radius * math.cos(tooth_center),
                tooth_mid_radius * math.sin(tooth_center),
            ),
            length,
        )

        # Two insulated-slot placeholders; insulation is not modeled separately.
        coil_width = 0.18 * pitch
        coil_offset = 0.14 * pitch
        for side, sign in (("A", -1.0), ("B", 1.0)):
            center = slot_center + sign * coil_offset
            _draw_annular_sector(
                app,
                params.coil_inner_radius_mm,
                params.coil_outer_radius_mm,
                center - coil_width / 2.0,
                center + coil_width / 2.0,
            )
            coil_mid_radius = (
                params.coil_inner_radius_mm + params.coil_outer_radius_mm
            ) / 2.0
            _make_component(
                app,
                f"Coil_{side}_{slot + 1:02d}",
                params.copper_material,
                (
                    coil_mid_radius * math.cos(center),
                    coil_mid_radius * math.sin(center),
                ),
                length,
            )

    # Six alternating-position permanent-magnet segments on the outer rotor.
    magnet_pitch = 2.0 * math.pi / params.poles
    magnet_fraction = 0.86
    magnet_half_angle = magnet_fraction * magnet_pitch / 2.0
    magnet_mid_radius = (
        params.magnet_inner_radius_mm + params.magnet_outer_radius_mm
    ) / 2.0
    for pole in range(params.poles):
        center = pole * magnet_pitch
        _draw_annular_sector(
            app,
            params.magnet_inner_radius_mm,
            params.magnet_outer_radius_mm,
            center - magnet_half_angle,
            center + magnet_half_angle,
        )
        _make_component(
            app,
            f"Permanent_Magnet_{pole + 1:02d}",
            params.magnet_material,
            (magnet_mid_radius * math.cos(center), magnet_mid_radius * math.sin(center)),
            length,
            thermnet_pid=thermnet_pid,
            # Alternate inward/outward radial magnetization around the rotor.
            material_direction_index=(
                RADIAL_OUTWARD_DIRECTION_INDEX
                if pole % 2 == 0
                else RADIAL_INWARD_DIRECTION_INDEX
            ),
        )

    # Outer-rotor steel yoke.
    _draw_circle(app, params.magnet_outer_radius_mm)
    _draw_circle(app, params.rotor_outer_radius_mm)
    _make_component(
        app,
        "Rotor_Yoke",
        params.steel_material,
        (
            (params.magnet_outer_radius_mm + params.rotor_outer_radius_mm) / 2.0,
            0.0,
        ),
        length,
    )


def _thermnet_processes() -> dict[int, str]:
    import pywintypes
    import win32api
    import win32con
    import win32process

    processes = {}
    access = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
    for pid in win32process.EnumProcesses():
        try:
            handle = win32api.OpenProcess(access, False, pid)
        except pywintypes.error:
            continue
        try:
            executable = win32process.GetModuleFileNameEx(handle, None)
        except pywintypes.error:
            continue
        finally:
            win32api.CloseHandle(handle)
        if Path(executable).name.lower() == "thermnet.exe":
            processes[pid] = executable
    return processes


def _connect_to_thermnet():
    try:
        import pythoncom
        import pywintypes
        from win32com.client import CastTo, DispatchEx
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is required. Install it with: python -m pip install pywin32"
        ) from exc

    pythoncom.CoInitialize()
    try:
        existing_processes = _thermnet_processes()
        raw_app = DispatchEx("ThermNet.Application")
        app = CastTo(raw_app, "IKWinApp")
        document_api = CastTo(raw_app, "IIWinApp")
        thermnet_pid = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and thermnet_pid is None:
            new_processes = _thermnet_processes()
            started = set(new_processes) - set(existing_processes)
            if started:
                thermnet_pid = max(started)
                break
            time.sleep(0.1)
        if thermnet_pid is None:
            app.close(False)
            raise RuntimeError(
                "ThermNet did not start a new process; refusing to automate "
                "material dialogs in an existing user instance."
            )
    except pywintypes.com_error:
        pythoncom.CoUninitialize()
        raise RuntimeError(
            "Could not start or bind the Simcenter MAGNET Thermal interfaces."
        ) from None
    except Exception:
        pythoncom.CoUninitialize()
        raise
    return pythoncom, app, document_api, thermnet_pid


def _remove_incomplete_stage(stage_dir: Path) -> None:
    def make_writable_and_retry(function, path, _error) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        function(path)

    last_error = None
    for attempt in range(3):
        try:
            shutil.rmtree(stage_dir, onerror=make_writable_and_retry)
            return
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25)
    if last_error is not None:
        raise last_error


def _create_sample_stage(output_root: Path) -> tuple[int, Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    sample_number = 1
    while True:
        sample_name = f"sample_{sample_number:04d}"
        final_dir = output_root / sample_name
        stage_dir = output_root / f".{sample_name}.incomplete"
        if final_dir.exists() or stage_dir.exists():
            sample_number += 1
            continue
        stage_dir.mkdir()
        return sample_number, stage_dir, final_dir


def _script_value(app, expression: str):
    app.processCommand(f"ret = {expression}")
    app.processCommand("Call setvariant(0, ret)")
    return app.getVariant(0)


def _prepare_thermnet(app) -> None:
    app.setMessageBoxAlwaysVisible(False)
    message_box_never = _script_value(app, "infoMessageBoxVisibleNever")
    app.setMessageBoxVisibleState(message_box_never)
    app.Visible = True


def _open_document(app, document_api, path: Path) -> None:
    document_api.openDocument(str(path), False)
    opened_path = _script_value(app, "getDocument().getFilePath()")
    if Path(str(opened_path)).resolve() != path.resolve():
        raise RuntimeError(
            f"ThermNet opened {opened_path!r}, expected the template {str(path)!r}"
        )


def _save_copy(app, path: Path) -> None:
    escaped_path = str(path).replace('"', '""')
    app.processCommand(f'Call getDocument().saveCopy("{escaped_path}")')
    if not path.is_file():
        raise RuntimeError(f"ThermNet did not create the expected file: {path}")


def _run_solution(
    app, solution_name: str, duration_seconds: float, step_seconds: float
) -> bool:
    validation_method, solve_method = {
        "static_2d": ("isValidForStatic2dSolver", "solveStatic2d"),
        "static_3d": ("isValidForStatic3dSolver", "solveStatic3d"),
        "transient_2d": ("isValidForTransient2dSolver", "solveTransient2d"),
        "transient_3d": ("isValidForTransient3dSolver", "solveTransient3d"),
    }[solution_name]

    import pywintypes
    from win32com.client import CastTo

    try:
        document = app.getDocument()
        model = CastTo(document, "IIDocument")
        thermal_model = CastTo(document, "IThermNetDocument")

        if solution_name.startswith("transient"):
            model.setDefaultTimeUnit("seconds", False)
            model.setFixedIntervalTimeSteps(
                0.0, duration_seconds, step_seconds
            )
            model.setTimeStepMethod(FIXED_INTERVAL_TIME_STEP_METHOD)
            thermal_model.setTransientCoupledMethod(TRANSIENT_COUPLED_METHOD)
        else:
            thermal_model.setStaticCoupledMethod(STATIC_COUPLED_METHOD)

        is_valid = getattr(thermal_model, validation_method)()
        if not is_valid:
            print(f"Skipping {solution_name}: the template reports it is not valid.")
            return False

        solved = getattr(thermal_model, solve_method)()
        if not solved:
            raise RuntimeError(
                f"ThermNet reported that {solution_name} did not solve"
            )
        return True
    except pywintypes.com_error as exc:
        raise RuntimeError(
            f"ThermNet failed while validating or running {solution_name}: {exc}"
        ) from exc


def _close_active_document(app) -> None:
    app.closeDocument(False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template",
        type=Path,
        required=True,
        help="Path to a clean coupled MAGNET Thermal .mn or .tn template",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.cwd() / "motor_samples",
        help="Folder in which incrementally numbered sample folders are created",
    )
    parser.add_argument(
        "--time-steps",
        type=int,
        default=10,
        help="Value assigned to the required numeric TimeSteps model parameter",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=10.0,
        help="Transient stop time in seconds (default: 10)",
    )
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=1.0,
        help="Fixed transient interval in seconds (default: 1)",
    )
    args = parser.parse_args()

    template = args.template.resolve()
    if not template.is_file():
        raise FileNotFoundError(f"Coupled model template not found: {template}")
    if template.suffix.lower() not in {".mn", ".tn"}:
        raise ValueError(
            f"Expected a MAGNET Thermal .mn or .tn template: {template}"
        )
    if not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0:
        raise ValueError("--duration-seconds must be a positive finite number")
    if not math.isfinite(args.step_seconds) or args.step_seconds <= 0:
        raise ValueError("--step-seconds must be a positive finite number")
    step_count = args.duration_seconds / args.step_seconds
    if not math.isclose(step_count, round(step_count), rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(
            "--duration-seconds must be an integer multiple of --step-seconds"
        )

    params = MotorParameters(time_steps=args.time_steps)
    params.validate()
    sample_number, stage_dir, final_dir = _create_sample_stage(
        args.output_root.resolve()
    )
    try:
        pythoncom, app, document_api, thermnet_pid = _connect_to_thermnet()
        leave_design_open = False
        try:
            _prepare_thermnet(app)
            _open_document(app, document_api, template)
            create_motor(app, params, thermnet_pid)
            design_path = stage_dir / "design.mn"
            _save_copy(app, design_path)
            _close_active_document(app)

            saved_solutions: list[str] = []
            solution_names = (
                "static_2d",
                "transient_2d",
                "static_3d",
                "transient_3d",
            )
            for solution_name in solution_names:
                _open_document(app, document_api, design_path)
                solved = _run_solution(
                    app,
                    solution_name,
                    args.duration_seconds,
                    args.step_seconds,
                )
                if solved:
                    coupled_name = f"coupled_{solution_name}"
                    _save_copy(app, stage_dir / f"{coupled_name}.mn")
                    saved_solutions.append(coupled_name)
                _close_active_document(app)

            # Close every staged file before renaming its directory. Opening
            # the final design beforehand leaves a Windows file lock in staging.
            stage_dir.rename(final_dir)
            _open_document(app, document_api, final_dir / design_path.name)
            app.Visible = True
            leave_design_open = True
        finally:
            try:
                if not leave_design_open:
                    app.close(False)
            finally:
                pythoncom.CoUninitialize()

        print(f"Saved sample {sample_number:04d} to: {final_dir}")
        print(f"Saved solutions: {', '.join(saved_solutions) or 'none'}")
    except Exception as run_error:
        if stage_dir.is_dir():
            try:
                _remove_incomplete_stage(stage_dir)
            except OSError as cleanup_error:
                raise RuntimeError(
                    f"{run_error}\nAdditionally, could not remove the "
                    f"incomplete sample folder {stage_dir}: {cleanup_error}"
                ) from run_error
        raise


if __name__ == "__main__":
    main()
