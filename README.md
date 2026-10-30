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

Optional arguments:

| Argument | Default | Description |
|---|---:|---|
| `--template` | Required | Path to a clean coupled MAGNET Thermal `.mn` or `.tn` template |
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
```

Solution files are written only when ThermNet reports the corresponding solver
is valid and the solve completes successfully; invalid solution types are
skipped. A solution file is a ThermNet model copy containing that solution,
not an exported report or image.

The script configures static coupled solves to solve magnetic first, then
thermal. For transient solves it sets the time unit to seconds, selects the
fixed-interval time-step method, and configures magnetic solving at each time
step. This explicit time-step method is required for ThermNet to consider the
transient setup valid.

Each run launches a visible ThermNet instance. On success, the numbered
sample's `design.mn` is left open so the geometry can be inspected. Existing
numbered samples and ThermNet application work files are preserved. If a run
fails, cleanup is limited to that run's incomplete sample folder.

## Validation note

The workflow was exercised against the session's coupled template. That run
created `design.mn` and all four coupled 2D/3D static/transient solution files.
The transient validation issue was traced to the missing fixed-interval
time-step method selection; setting only the start, stop, and step values was
not sufficient.
