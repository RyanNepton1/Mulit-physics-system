# Coupled MAGNET Thermal Motor Generator

`create_motor.py` builds a parameterized outer-rotor electric motor inside a
Simcenter MAGNET Thermal 2212 model. It uses ThermNet's COM automation API,
leaves the finished design visible in ThermNet, and saves the design and
successful coupled solutions in incrementally numbered sample folders.

## Requirements

- Windows
- Simcenter MAGNET Thermal 2212 installed
- Python 3.9 or newer
- `pywin32` installed for the Python interpreter used to run the script

Install the Python dependency:

```powershell
python -m pip install pywin32
```

Use a **clean MAGNET Thermal coupled-model template** with the required
electromagnetic and thermal setup. The template can have an `.mn` or `.tn`
extension. Do not use a model that already has generated motor geometry unless
you intend to add another copy of the motor.

## Run

```powershell
python .\create_motor.py --template "C:\path\to\coupled_template.tn"
```

To export CSV maps from an already-generated sample without solving it again:

```powershell
python .\create_motor.py --export-sample ".\motor_samples\sample_0005"
```

To render heatmaps for every result CSV in a sample folder (searched
recursively):

```powershell
python .\generate_heatmap.py ".\motor_samples\sample_0005"
```

Pass `.\motor_samples` to process all nested sample folders. This writes one
SVG beside each recognized result CSV. The default map is an XY projection
that averages the field values into a 2D grid across the model's axial depth.
Use `--time 5` to plot a specific transient time in seconds (static maps remain
static), or `--bins 400` to increase grid resolution. For a single CSV,
`--output "C:\plots\heatmap.svg"` can select the output path. SVG files open in
a web browser and do not require extra Python plotting packages.

When `create_motor.py` finishes exporting the sample's solution CSVs, it
automatically runs the recursive heatmap generator on that sample before
finalizing its numbered output folder.

The heat-source CSV for `sample_0006` currently contains only zero values, so
its heatmap explicitly indicates that there is no heat source in that result.
Add electromagnetic excitation or thermal heat sources to the model to produce
a non-uniform heat-loss map.

Optional arguments:

| Argument | Default | Description |
|---|---:|---|
| `--template` | Required unless exporting | Path to a clean coupled MAGNET Thermal `.mn` or `.tn` template |
| `--export-sample` | — | Export CSV maps from an existing numbered sample folder instead of generating a new sample |
| `--output-root` | `.\motor_samples` | Folder for numbered sample directories |
| `--time-steps` | `10` | Numeric model parameter assigned to `TimeSteps` |
| `--duration-seconds` | `10` | Transient stop time |
| `--step-seconds` | `1` | Fixed transient time interval |

The duration must be a positive integer multiple of the time interval. For
example:

```powershell
python .\create_motor.py `
  --template "C:\models\coupled_template.mn" `
  --output-root "C:\models\motor_samples" `
  --duration-seconds 20 `
  --step-seconds 0.5
```

## Generated motor

The default design is a 12-slot, 6-pole outer-rotor motor with a 50 mm axial
length. Dimensions and material-library names are configured in the
`MotorParameters` dataclass in `create_motor.py`.

Default materials:

- Stator and rotor: `M-19 29 Ga`
- Coils: `Copper: 5.77e7 Siemens/meter`
- Permanent magnets: `N35`

Permanent-magnet segments are assigned alternating radial outward and inward
directions. The generated coils are conductor placeholders; slot insulation is
not modeled separately.

## Solutions and output

Each run selects a new sample number and creates a folder such as
`motor_samples\sample_0001`. It saves:

```text
sample_0001\
  design.mn
  coupled_static_2d.mn
  coupled_transient_2d.mn
  coupled_static_3d.mn
  coupled_transient_3d.mn
  coupled_static_2d_temperature.csv
  coupled_static_2d_heat_source_density.csv
  ...
```

Solution files are written only when ThermNet reports the corresponding solver
is valid and the solve completes successfully; invalid solution types are
skipped. A solution file is a ThermNet model copy containing that solution,
not an exported report or image.

For every successful solution, the script also exports spatial CSV maps:

- `coupled_<solution>_temperature.csv` contains time, XYZ coordinates in
  meters, and temperature in the model's temperature unit.
- `coupled_<solution>_heat_source_density.csv` contains time, field quantity,
  XYZ coordinates in meters, and total heat-source density in W/m³.

Transient CSVs contain rows for every saved solution time. Static CSVs leave
`time_s` blank. Element-based heat-source values are located at the centroid
of their mesh element; fields stored at mesh points retain their point
coordinates. Use `--export-sample` to regenerate these CSVs from existing
solution files; the sample's design and solution files are left unchanged.

The script configures static coupled solves to solve magnetic first, then
thermal. For transient solves it sets the time unit to seconds, selects the
fixed-interval time-step method, and configures magnetic solving at each time
step. This explicit time-step method is required for ThermNet to consider the
transient setup valid.

Each run launches a visible ThermNet instance. On success, the numbered
sample's `design.mn` is left open so the geometry can be inspected. Existing
numbered samples and ThermNet application work files are preserved. If a run
fails, cleanup is limited to that run's incomplete sample folder.

## MotorSolve BLDC baseline

`run_startup_prototype.py` uses the MotorSolve 2212 COM API to run the supplied
parameterized `.moto` design and export its built-in torque-speed and thermal
loss charts plus instantaneous air-gap flux density sampled with MotorSolve's
field arc probe. The supplied
`testMotor.moto` baseline contains a 12-slot,
4-pole, 3-phase design named `Prototype Design 1`; its rated current is
7.85 A, rated speed is 1000 rpm, and supply voltage is 15 V. These are the
starter model's settings, not independently validated motor ratings.

Open the design in **Simcenter MotorSolve 2212** before running the script:

```powershell
python .\run_startup_prototype.py `
  --project "C:\Users\rnepto\Documents\testMotor.moto"
```

The script attaches to the running MotorSolve instance and checks that the
active project and software version match the requested inputs. It does not
launch another MotorSolve version, alter the motor design, or save the source
model; field-result settings are updated in memory for the export. It creates a
new numbered folder under `.\motorsolve_samples` containing:

```text
sample_0001\
  torque_speed.csv
  thermal_losses.csv
  instantaneous_flux_density.csv
  parameters.json
  metadata.json
```

`instantaneous_flux_density.csv` contains the field arc's angle, XY location,
axial position, flux density in tesla, and a `sampled` flag. The default probe
is a full circle of radius 20.9 mm centered on the motor axis, using 721
samples (0.5° spacing). MotorSolve may omit positions where field values are
unavailable; those angles remain in the CSV with a blank value and
`sampled=false`. The arc radius, center, angle range, sample count, current
percentage, advance angle, rotor position, and field-result configuration are
recorded alongside the motor and chart parameters in `parameters.json`;
`metadata.json` also records sample counts and angular spacing. Field arc
probe settings are changed in MotorSolve memory for the export; the source
model is not saved. The thermal-loss chart is MotorSolve's
“Losses for thermal analysis” result at the
model's rated operating point; it requires a Thermal Results license and is
not a spatial temperature map or a transient thermal simulation.

Optional arguments:

| Argument | Default | Description |
|---|---:|---|
| `--project` | Required | `.moto` project already open in MotorSolve 2212 |
| `--output-root` | `.\motorsolve_samples` | Folder for numbered data samples |
| `--design-name` | The only design in the project | Explicit design to run |
| `--field-arc-radius-mm` | `20.9` | Circular air-gap probe radius in millimeters |
| `--field-arc-samples` | `721` | Number of samples along the arc (2–3601) |
| `--field-arc-center-z-mm` | `0` | Axial coordinate of the arc center |
| `--field-arc-start-angle-deg` | `0` | Start angle of the arc |
| `--field-arc-end-angle-deg` | `360` | End angle of the arc |
| `--field-current-percent` | `100` | Peak line current as a percent of rated current |
| `--field-advance-angle-deg` | `0` | Current advance angle for the field snapshot |
| `--field-rotor-position-deg` | `0` | Rotor position for the field snapshot |

This is the baseline data-export step; parameter sweeps are not included yet.
A loss-chart/license failure leaves the completed torque-speed CSV and a
failed `metadata.json` in the sample folder for diagnosis.

## Validation note

The workflow was exercised against the session's coupled template. That run
created `design.mn` and all four coupled 2D/3D static/transient solution files.
The transient validation issue was traced to the missing fixed-interval
time-step method selection; setting only the start, stop, and step values was
not sufficient.

CSV export reports the fields present in the solved model; it does not create
electrical excitation or losses. The generated coils are conductor placeholders
and are not assigned winding currents by this script. In the exported
`sample_0005`, heat-source density is zero and temperature remains at 20 °C.
Without excitations or other heat sources in the template, maps can therefore
contain zeros even when the solve and export succeed.
