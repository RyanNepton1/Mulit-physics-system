import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_startup_prototype as prototype


class FakeChoice:
    _oleobj_ = object()

    def __init__(self, name, checked=True):
        self.Name = name
        self.Checked = checked
        self.id = name


class FakeParameter:
    def __init__(self, name, value, quantity="", unit=""):
        self.Name = name
        self.Value = value
        self.Quantity = quantity
        self.Unit = unit
        self.IsValid = True

    def SetValue(self, value):
        if self.Name == "Center point":
            self.Value = tuple(float(coordinate) for coordinate in value.split())
        elif isinstance(self.Value, FakeChoice):
            self.Value = FakeChoice(value)
        elif self.Unit:
            self.Value = float(value)
        else:
            self.Value = value


class FakeParameterManager:
    def __init__(self, parameters):
        self.Parameters = parameters

    def GetParameterByName(self, name):
        return next(parameter for parameter in self.Parameters if parameter.Name == name)


class FakeChartData:
    def __init__(self, headers, values):
        self.ColumnHeaders = headers
        self.values = values

    def Export(self, filename, _format):
        with Path(filename).open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(["", *self.ColumnHeaders])
            writer.writerow(["1", *self.values])


class FakeFieldChartData:
    ColumnHeaders = ("Angle (deg)", "Value (T)")
    values = (0.25,)

    def Export(self, filename, _format):
        with Path(filename).open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(["", "Angle (deg)", "Value (T)"])
            writer.writerow(["1", "0", "0.25"])


class FakeChartView:
    def __init__(self):
        self.Data = None

    def Clear(self):
        self.Data = None


class FakeResult:
    def __init__(self, name, design_name, app, headers, values):
        self.Name = name
        self.app = app
        self.data = FakeChartData(headers, values)
        self.ParameterManager = FakeParameterManager(
            [
                FakeParameter(
                    "Designs",
                    [FakeChoice(design_name)],
                ),
                FakeParameter("Speed/Accuracy tradeoff", "Default"),
            ]
        )

    def View(self):
        self.app.ChartView.Data = self.data


class FakeFieldResult:
    def __init__(self, name, app, parameters, data=None):
        self.Name = name
        self.app = app
        self.ParameterManager = FakeParameterManager(parameters)
        self.data = data or FakeFieldChartData()

    def View(self):
        self.app.ChartView.Data = self.data


class FakeNamedItem:
    def __init__(self, name):
        self.Name = name


class FakeGroup:
    def __init__(self, name, children):
        self.Name = name
        self.Results = children


class FakeDesign:
    Name = "Prototype Design 1"
    Parts = [
        SimpleNamespace(
            Name="General Settings",
            ParameterManager=FakeParameterManager(
                [
                    FakeParameter("Rated current", 7.85, "Current", "Ampere"),
                    FakeParameter(
                        "Outer diameter",
                        74.9,
                        "Length",
                        "MilliMetre",
                    ),
                ]
            ),
        )
    ]


class FakePythonCom:
    def CoInitialize(self):
        pass

    def CoUninitialize(self):
        pass


class RunStartupPrototypeTests(unittest.TestCase):
    def test_serializes_motor_solve_values(self):
        self.assertEqual(prototype._serialize_value(7.85), 7.85)
        self.assertEqual(
            prototype._serialize_value([FakeChoice("Prototype Design 1")]),
            [{"name": "Prototype Design 1", "id": "Prototype Design 1"}],
        )

    def test_finds_nested_result_by_exact_path(self):
        target = FakeNamedItem("Torque vs. speed")
        motor = SimpleNamespace(
            ResultManager=SimpleNamespace(
                Results=[
                    FakeGroup(
                        "Motor Results",
                        [
                            FakeGroup(
                                "Performance Charts",
                                [target],
                            )
                        ],
                    )
                ]
            )
        )
        self.assertIs(
            prototype._find_result(
                motor,
                ("Motor Results", "Performance Charts", "Torque vs. speed"),
            ),
            target,
        )

    def test_allocates_next_sample_without_overwriting_existing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "sample_0001").mkdir()
            (root / "sample_0004").mkdir()
            sample = prototype._next_sample_dir(root)
            self.assertEqual(sample.name, "sample_0005")
            self.assertTrue(sample.is_dir())

    def test_builds_field_arc_and_rejects_invalid_sample_count(self):
        samples = prototype._field_arc_samples(20.9, 1.5, 0.0, 360.0, 5)
        self.assertEqual(
            samples,
            [
                (0.0, 20.9, 0.0, 1.5),
                (90.0, 0.0, 20.9, 1.5),
                (180.0, -20.9, 0.0, 1.5),
                (270.0, 0.0, -20.9, 1.5),
                (360.0, 20.9, 0.0, 1.5),
            ],
        )
        with self.assertRaises(ValueError):
            prototype._field_arc_samples(20.9, 0.0, 0.0, 360.0, 1)

    def test_normalizes_arc_csv_and_marks_missing_angles(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_csv = root / "raw.csv"
            output_csv = root / "field.csv"
            raw_csv.write_text(
                ",Angle (deg),Value (T)\n"
                "1,0,0.25\n",
                encoding="utf-8",
            )
            samples = [
                (0.0, 20.9, 0.0, 0.0),
                (90.0, 0.0, 20.9, 0.0),
            ]

            sampled = prototype._write_instantaneous_field_arc_csv(
                raw_csv,
                output_csv,
                samples,
            )

            self.assertEqual(sampled, 1)
            with output_csv.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["angle_deg"], "0.0")
            self.assertEqual(rows[0]["x_mm"], "20.9")
            self.assertEqual(rows[0]["flux_density_T"], "0.25")
            self.assertEqual(rows[0]["sampled"], "true")
            self.assertEqual(rows[1]["flux_density_T"], "")
            self.assertEqual(rows[1]["sampled"], "false")

    def test_run_exports_curves_and_parameter_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "testMotor.moto"
            project.write_bytes(b"test project")

            app = SimpleNamespace(
                Version="2212.0.0.0",
                ChartView=FakeChartView(),
                Constants=SimpleNamespace(TableExportFormat_CSV=2),
            )
            torque = FakeResult(
                "Torque vs. speed",
                FakeDesign.Name,
                app,
                ("Rotor speed (rpm)", "Torque (N.m)"),
                (100, 1.1),
            )
            losses = FakeResult(
                "Losses for thermal analysis",
                FakeDesign.Name,
                app,
                ("Loss - Total (kW)", "Loss - Winding (kW)"),
                (0.04, 0.038),
            )
            field = FakeFieldResult(
                "Instantaneous fields",
                app,
                [
                    FakeParameter("Design", FakeChoice(FakeDesign.Name)),
                    FakeParameter("Thermal state", FakeChoice("Ambient temperature")),
                    FakeParameter("Contour plot field", FakeChoice("Flux density")),
                    FakeParameter("Shaded plot field", FakeChoice("Flux density")),
                    FakeParameter("Current input method", FakeChoice("% of rated current")),
                    FakeParameter("Peak line current percentage", 100.0),
                    FakeParameter("Advance angle", 0.0, "Angle", "Degree"),
                    FakeParameter("Rotor position", 0.0, "Angle", "Degree"),
                ],
            )
            arc_probe = FakeFieldResult(
                "Field arc probe",
                app,
                [
                    FakeParameter("Radius", 20.9, "Length", "MilliMetre"),
                    FakeParameter("Center point", (0.0, 0.0, 0.0), "Length", "MilliMetre"),
                    FakeParameter("Start angle", 0.0, "Angle", "Degree"),
                    FakeParameter("End angle", 360.0, "Angle", "Degree"),
                    FakeParameter("Number of sample points", 721.0),
                    FakeParameter("Exchange X and Y axes", FakeChoice("No")),
                ],
            )
            app.Motor = SimpleNamespace(
                filename=str(project),
                DesignManager=SimpleNamespace(Designs=[FakeDesign()]),
                ResultManager=SimpleNamespace(
                    Results=[
                        FakeGroup(
                            "Motor Results",
                            [
                                FakeGroup(
                                    "Performance Charts",
                                    [torque],
                                )
                            ],
                        ),
                        FakeGroup("Thermal Charts", [losses]),
                        FakeGroup("Fields", [field]),
                        FakeGroup("Field Charts & Tables", [arc_probe]),
                    ]
                ),
            )
            pywintypes = SimpleNamespace(
                com_error=type("ComError", (Exception,), {})
            )

            with patch.object(
                prototype,
                "_attach_to_motorsolve",
                return_value=(FakePythonCom(), pywintypes, app),
            ):
                sample = prototype.run(project, root / "output")

            self.assertEqual(sample.name, "sample_0001")
            self.assertTrue((sample / "torque_speed.csv").is_file())
            self.assertTrue((sample / "thermal_losses.csv").is_file())
            self.assertTrue(
                (sample / "instantaneous_flux_density.csv").is_file()
            )
            manifest = json.loads((sample / "parameters.json").read_text())
            self.assertEqual(
                manifest["design_parameters"][0]["value"],
                7.85,
            )
            metadata = json.loads((sample / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["torque_speed"]["data_rows"], 1)
            self.assertEqual(metadata["thermal_losses"]["data_rows"], 1)
            self.assertEqual(
                metadata["instantaneous_field"]["requested_points"],
                prototype.DEFAULT_FIELD_ARC_SAMPLES,
            )
            self.assertEqual(
                metadata["instantaneous_field"]["arc_radius_mm"],
                prototype.DEFAULT_FIELD_ARC_RADIUS_MM,
            )


if __name__ == "__main__":
    unittest.main()