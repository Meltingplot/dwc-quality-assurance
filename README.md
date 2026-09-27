# Quality Assurance

DuetWebControl / DuetSoftwareFramework 3.7 plugin by Meltingplot GmbH that records the process
parameters of every print job: temperatures and heater load, filament monitor readings,
extrusion, speeds, positions, supply voltages, setpoint changes and events, layer by layer.
It keeps a long-term history in SQLite on the printer's SBC and serves it over an HTTP API to
its own DWC page, to the CHX 350 operator interface and to a later Quality Control plugin.

It records; it does not judge.

What it keeps per job: context (file, slicer settings, machine configuration), time series with
full-resolution blocks around anything unusual, layer aggregates (temperatures, heater load,
filament measured vs. commanded, flow), events with position, a replay of each layer from the
G-code, a timelapse video (one frame per layer, AV1) and vibration spectra from the accelerometer.

- HTTP API (also the contract for the CHX 350 UI and Quality Control): [docs/api.md](docs/api.md)
- Storage, database and retention: [docs/schema.md](docs/schema.md)
- CHX 350 UI integration: [docs/chx-integration.md](docs/chx-integration.md)
- Bundling with the CHX 350 image: [docs/image.md](docs/image.md); testing on a machine without an
  image build: [docs/sideload.md](docs/sideload.md)
- Proposals for dwc-vigil: [docs/vigil-candidates.md](docs/vigil-candidates.md)
- Design and decisions: [PLAN.md](PLAN.md); developer guide: [CLAUDE.md](CLAUDE.md)

## Requirements

DSF and DWC 3.7 in SBC mode, Python 3.11 or newer with dsf-python 3.7, `ffmpeg` with SVT-AV1 for
the timelapse (Debian trixie's package has it). Every endpoint needs a DWC session (docs/api.md
"Authentication"). On the CHX 350 the plugin comes with the image; it is not installed from
Settings › Plugins.

## Development

```bash
npm ci
npm run lint && npm test          # frontend (vitest, Vuetify 4)
scripts/ci-local.sh python        # daemon tests against dsf-python 3.7.0b1
scripts/ci-local.sh build         # plugin ZIP against Meltingplot/DuetWebControl v3.7-dev
```

## License

MIT
