# Quality Assurance

DuetWebControl / DuetSoftwareFramework 3.7 plugin by Meltingplot GmbH that records the process
parameters of every print job: temperatures and heater load, filament monitor readings,
extrusion, speeds, positions, supply voltages, setpoint changes and events, layer by layer.
It keeps a long-term history in SQLite on the printer's SBC and serves it over an HTTP API to
its own DWC page, to the CHX 350 operator interface and to a later Quality Control plugin.

It records; it does not judge.

- Design and decisions: [PLAN.md](PLAN.md)
- Developer guide: [CLAUDE.md](CLAUDE.md)
- Bundling with the CHX 350 image: [docs/image.md](docs/image.md)

## Requirements

DSF and DWC 3.7 in SBC mode, Python 3.11 or newer with dsf-python 3.7, `ffmpeg` for the
timelapse. On the CHX 350 the plugin comes with the image; it is not installed from
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
