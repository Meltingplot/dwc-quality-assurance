# Plan: dwc-quality-assurance (DWC + DSF Plugin, DSF 3.7)

Stand: überarbeitet am 2026-09-26 nach Abgleich mit dem Umsetzungsstand (CHX350-UI, CHX350-Backend,
Pi-Image, HMI, Maschine) und Tims Antworten vom selben Tag. Die Änderungen gegenüber der Fassung vom
2026-09-22 stehen in §0.

## 0. Revision 2026-09-26

**Umsetzungsstand QA (2026-09-27):** Phasen 0–6 umgesetzt und auf `main` (PRs #1–#8 in
`Meltingplot/dwc-quality-assurance`), dazu die Sitzungsprüfung aller Endpunkte (#5). Phase 7
(Abschluss) läuft. Noch nicht auf der Maschine erprobt: Zeitraffer mit der echten Kamera, M956
während eines Drucks, AppArmor-Regeln des Image-Builds. (Bis 2026-09-26 war nichts umgesetzt.)

**Seit dem Plan entstanden** (Meltingplot-Fork von DWC, Branch `v3.7-dev`): das eingebaute
Layout-Plugin `src/plugins/CHX350/` (Bedienoberfläche der Maschine, übernimmt das Layout), sein
SBC-Backend `/machine/CHX350/{status,fileinfo,history,diagnostics}` (läuft auf der Maschine seit dem
Image mit 3.7.0-rc.2+mp.4), das Pi-Image mit eigener AppArmor-Plugin-Richtlinie und in chx350-config
eine eigene Auswertung des Filamentmonitors.

| # | Thema | Plan 2026-09-22 | Jetzt | Grund |
|---|---|---|---|---|
| 1 | Plattform | Pi OS Bookworm, Python 3.11, Node 22, DSF rc.1 | Raspberry Pi 5, Debian 13 trixie, Python 3.13; DWC/DSF 3.7.0-rc.2+mp.N; Vue 3.5, Vuetify 4.1, Pinia 4, Chart.js 4.5, Vite 8 (Node ≥ 22.12, Dev-Umgebung Node 24) | Maschine und DWC-Fork am 2026-09-26 |
| 2 | Installation | Settings › Plugins | nur über den Image-Build (`plugins.list` + eigener AppArmor-Block); ffmpeg als apt-Abhängigkeit, die der Image-Build mitinstalliert (§5.12) | das Image lässt nachinstallierte Plugins keinen Code ausführen (CE) |
| 3 | Bedienoberfläche | eigene QA-Seite | Verlauf und Analyse der CHX-UI lesen die QA-API (§5.10); die QA-Seite ist die Engineering-Ansicht im klassischen DWC | CHX350-Layout übernimmt die Oberfläche |
| 4 | Heizlast | nur `avgPwm` als Kanal | eigene Kennzahl Heizlast = `avgPwm / model.maxPwm`, gleiche Definition wie das Heizlast-Banner der CHX-UI: Lagenaggregate, Events, Zusammenfassung, Trend (§5.4.1) | Anforderung Tim 2026-09-26 |
| 5 | MFM der Maschine | – | tolerierte Fehler, Auto-Recovery, Flow-Bias-Erkennung und E-Step-Korrektur als Events und Kanäle aus `global.mfm_*` (§5.4.2) | Entscheidung Tim 2026-09-26; `finish.g` setzt E-Steps nach jedem Job zurück, nur QA hält den Verlauf |
| 6 | Zeitraffer | – | Snapshot je Lage von einer konfigurierbaren Kamera-URL, nach Jobende **nur AV1**; Frame je Lage in der Analyse, Video-Download (§5.11) | Entscheidung Tim 2026-09-26 |
| 7 | Befunde, Prüfbericht | – | gehören zu Quality Control; QA bleibt reine Erfassung. Der CHX-UI-Plan, der sie QA zuordnet, ist damit überholt | Entscheidung Tim 2026-09-26 |
| 8 | Live-WebSocket | WS `live` | bleibt; der haproxy des HMI leitet WebSockets schon über ein eigenes Backend ohne Verbindungsgrenze, keine Änderung nötig. Normale QA-Anfragen müssen dagegen schnell antworten (§5.6) | Entscheidung Tim + haproxy.cfg vom 2026-09-26 16:02 |
| 9 | Kammer | `heat.chamberHeaterMapping` | Fallback auf den Analogsensor „SZP coil“ wie in der CHX-UI | die CHX 350 hat keinen Kammerheizer |
| 10 | Materialcharge | `global.filamentBatch` | gibt es auf der CHX nicht; stattdessen konfigurierbare Liste von Globals im Kontext (§5.9) | Globals der Maschine |
| 11 | Slicer-Kontext | Felder aus `job.file` | zusätzlich OrcaSlicer-`CONFIG_BLOCK` vom Dateiende (Parser aus dem CHX350-Backend) | DSF liest `;customInfo` nur im Dateikopf |
| 12 | Accelerometer | Phase 4 | Phase 6 mit dem Accelerometer des SZP (CAN 60, Port `60.i2c.lis`, 800 Hz; der Listenindex variiert, Laborgerät: 1), gewählt über `accelerometer.board`; Spektren in reinem Python statt numpy (keine Python-Pakete im Image-Build nötig) | Tim 2026-09-27; Objektmodell im Screenshot vom 2026-09-27 |
| 13 | Daemon-Threads | ein HTTP/WS-Thread | dsf-python bedient jeden Endpunkt in einem eigenen Thread mit eigenem Event-Loop (§5.3) | CHX350-Backend, dsf-python 3.7.0b1 |
| 14 | Backend-Erkennung | Banner nach Fehler | Plugin-pid im Objektmodell; `sbcAutoRestart: true` | DSF lässt Endpunkte nach einem Absturz registriert |
| 16 | CRC32 | via M38 | `zlib.crc32` über die aufgelöste Datei; M38 nicht nötig (DSF berechnet M38 selbst und leert vorher die Code-Queue des Kanals) | DSF `MCodeHandler.cs` |
| 17 | Treiberfehler | `boards[].drivers[].status` | Status nur für Mainboard-Treiber verlässlich; Erweiterungsboards melden Änderungen nicht von selbst → dort über Events/`messages[]` | Wiki CAN_limitations, OM 2026-09-26 |
| 15 | Mehrere Düsen | – | alle Kanäle und Aggregate dynamisch je Heizer/Extruder; bis 16 Düsen, davon zwei gleichzeitig druckend | Roadmap CHX 350 |

## 1. Kontext

Ein Druckjob brach wiederholt mit `too little movement` des Filamentmonitors ab. Die
E-Step-Werte lagen mit 800–880 statt 760–800 auffällig hoch, was zunächst auf eine
fehlerhafte Kalibrierung deutete. Ursache war ein verschlissenes Extruderbauteil, das
die Filamentbewegung zeitweise bremste. Mit einer Erfassung der Prozesswerte pro Job und
einer Langzeithistorie wäre der schleichende Ausfall über Wochen sichtbar gewesen.

Seitdem wertet die Maschine den Filamentmonitor selbst aus (chx350-config): sie toleriert
einzelne Fehler, versucht im `pause.g` eine Auto-Recovery und korrigiert eine erkannte
Flow-Abweichung per `M92` im laufenden Job. `print/finish.g` setzt die E-Steps am Jobende
zurück, jeder Job erkennt neu. Den Verlauf über Jobs, also genau das Signal aus dem Vorfall,
hält nur QA. Ebenso zeigt die CHX-UI ein Heizlast-Banner (Düse dauerhaft ≥ 80 % ihrer
PWM-Grenze), aber nur live im Browser; QA speichert die Heizlast je Lage und Job.

`dwc-quality-assurance` erfasst **Prozessparameter** pro Druckjob, stellt sie in DWC
grafisch dar (inkl. Replay und Zeitraffer Lage für Lage) und liefert sie per HTTP-API an
die CHX-UI und an ein späteres Plugin `Quality Control`. Es erfasst nur und bewertet nicht.

Nachbarprojekte, deren Konventionen übernommen werden: `/home/tim/dwc/dwc-vigil`
(Maschinenparameter), `/home/tim/dwc/dwc-meltingplot-config` und das CHX350-Plugin im
DWC-Fork (`/home/tim/dwc/DuetWebControl/src/plugins/CHX350`, v. a. `dsf/`, `api.ts`,
`backend.ts`, `stores/heaterLoad.ts`, `composables/useJobHistory.ts`, `useJobAnalysis.ts`).
DSF-Referenz: `/home/tim/dwc/DuetSoftwareFramework` (Meltingplot-Fork, 3.7.0-rc.2+mp.N).
RRF-Fakten ausschließlich aus dem Duet3D-Wiki (`/home/tim/dwc/wiki-content`), bei Bedarf
ergänzt durch den RRF-Quellcode.

## 2. Abgrenzungsregel QA ↔ Vigil ↔ CHX350-Backend ↔ QC

**Regel:** QA erfasst alles, was einem Job zugeordnet ist oder das Druckergebnis
bestimmt (Prozess-Observablen mit Zeit, Lage, Koordinate). Vigil erfasst
jobunabhängige Maschinenzustände (Verschleißzähler, Versorgung als Tages-Min/Max,
Reboots, SBC-Ressourcen, Volumes).

**Grenzfälle** (entschieden): Heater-Faults, Treiberfehler, Spannungseinbrüche,
Kalibrierdrift werden in QA **nur mit Job-Kontext** als Job-Event bzw. Kontext-Schnappschuss
gespeichert; die jobunabhängige Zählung und Historie bleibt Vigil.

**Heizlast:** Vigil zählt jobunabhängig `full_load_seconds` (roh `avgPwm ≥ 0.95` bei
eingeschaltetem Heizer, nicht auf `maxPwm` normiert). Das bleibt Vigil. Die jobbezogene
Heizlast (§5.4.1) ist QA. Vorschlag für `docs/vigil-candidates.md`: Vigils Schwelle auf
`model.maxPwm` normieren.

**CHX350-Backend:** bleibt für `fileinfo` (Jobprüfung vor dem Start, unabhängig von QA)
und als Fallback-Verlauf aus dem Ereignisprotokoll. Läuft QA, liest die CHX-UI den
Verlauf aus QA (§5.10).

**Quality Control:** Bewertungen, Befunde, Prüfbericht, Notizen. QA hat keine Eingabefelder.

**Folge für die Umsetzung:** Wird bei der Implementierung ein Maschinenparameter
identifiziert, der nicht prozessbestimmend ist (kein Bezug zu Lage, Koordinate oder
Druckergebnis), wird er **nicht** in QA aufgenommen, sondern als Vorschlag für Vigil in
`docs/vigil-candidates.md` notiert. Beispiele, die explizit **nicht** in QA landen:
Betriebsstunden, Achsen-Fahrwege, Lüfterlaufzeit, tägliche vIn-Min/Max, Reboots,
SBC-CPU/RAM, freier Speicher.

## 3. Entschiedene Rahmenbedingungen

| Thema | Entscheidung |
|---|---|
| Generation | **Nur DWC 3.7 / DSF 3.7** (Meltingplot-Forks, 3.7.0-rc.2+mp.N): Vue 3.5 + Vuetify 4.1 + Pinia 4 + Chart.js 4.5, Vite 8, Node ≥ 22.12; SBC Raspberry Pi 5 mit Debian 13 trixie, Python 3.13, dsf-python 3.7 |
| Identität | ID `QualityAssurance`, Name „Quality Assurance“, Autor Meltingplot GmbH, MIT; Pfad `/plugins/QualityAssurance`, API `/machine/QualityAssurance/*`, Daten `/opt/dsf/sd/QualityAssurance/` (liegt auf `/persistent`, 2026-09-26: 25,7 GB frei) |
| Auslieferung | Release-Asset (Zip + sha256) → Pin in `plugins.list` des Image-Builds (rpi-image-gen, `mp-dsf-plugins`), eigener AppArmor-Block, ffmpeg als `sbcPackageDependencies` (§5.12). Kein Weg über Settings › Plugins |
| Speicher | SQLite, stromausfallsicher (WAL, `synchronous=FULL`, Quarantäne + Backup-Restore) |
| Verlustfenster | ≤ 30 s für Samples (gepufferte Transaktion); Events sofort committen |
| Retention | Rohdaten (Samples, Blöcke) der letzten 50 Jobs **oder** 90 Tage, DB-Obergrenze 2 GB; Job-Zusammenfassungen, Lagenaggregate, Events, Spektren unbegrenzt; Zeitraffer eigene Grenze (§5.11) |
| Backup | `quick_check` beim Start; korrupt → Quarantäne + Restore aus letztem Backup; `VACUUM INTO` nach jedem Jobende, 2 Generationen |
| Sampling | Ringpuffer im RAM: **jeder Object-Model-Patch**, 90 s zurück; alle **5 s** ein Grobsample in die DB; bei Trigger Puffer + 30 s Nachlauf in voller Auflösung als zusammenhängender Block (Nachlauf verlängert sich bei Folgetrigger) |
| Trigger für Block | jedes gespeicherte Event; Kanalsprung zwischen zwei Samples: Temperatur 5 K, Monitor-Prozent 15 Punkte, vIn 10 %; jede Sollwert-Änderung |
| Sollwerte (nur bei Änderung als Event) | `heater.active/standby`, **`heater.model.maxPwm`** (M307, u. a. je Materialprofil), `stepsPerMm` (mit Ursache, §5.4.2), `pressAdv.k0/k1/d`, `nonlinear`, Filamentmonitor `configured`/`calibrated`, `axes[Z].babystep` |
| Kanäle (5 s + Puffer) | Heizer `current, active, avgPwm, state`; Extruder `position, rawPosition, factor`, `job.rawExtrusion`; Filamentmonitor `status, lastPercentage, avgPercentage, minPercentage, maxPercentage, position, totalExtrusion, calibrated.totalDistance, calibrated.mmPerRev, agc`; `currentMove.topSpeed/requestedSpeed/extrusionRate`, `speedFactor`, Achsen `machinePosition`, Lüfter `actualValue/rpm`; Boards `vIn.current, v12.current, mcuTemp.current`; Maschinen-Globals der MFM-Auswertung (§5.4.2) |
| Abgeleitete Kanäle | Heizlast `heater.<n>.load = avgPwm / model.maxPwm` (nicht gespeichert, beim Lesen aus `avgPwm` und dem gültigen `maxPwm` berechnet; in API und Live-Frames wie ein Kanal) |
| Koordinaten | Immer `machinePosition` aller Achsen + `workplaceNumber` + `axes[].workplaceOffsets[workplaceNumber]` mitschreiben, damit User-Koordinaten später umgerechnet werden können |
| Filamentmonitor-Events | jeder `status`-Wechsel weg von `ok` (mit Rückkehr und Dauer); `lastPercentage` außerhalb `configured.percentMin/Max` ohne Firmware-Fehler (Mindestdauer konfigurierbar); Maschinensignale aus §5.4.2 |
| Heizer-Events | `state == fault`; M143-Monitor ausgelöst (`monitors[].limit` verletzt); **Heizlast** `heater_load` high/limit (§5.4.1). **Keine** Sollwertabweichungs-Events |
| Spannung/ESD | vIn/v12/mcuTemp als Kanal; Event bei vIn < Median(90 s) − 10 %; Phantommessung: Sprung ≥ 15 K einer `sensors.analog[].lastReading`/`heater.current` innerhalb eines Patches mit Rückkehr; Treiberfehler: Mainboard aus `boards[].drivers[].status` (Rohwert speichern), Erweiterungsboards aus den Events `driver-error`/`driver-warning`/`driver-stall` über `messages[]` (ihr Status wird nicht von selbst gemeldet, im OM bleibt er 0) |
| Lebenszyklus-Events | Start, Ende (completed/cancelled/aborted), Pause/Resume mit vermuteter Ursache, Babystep/Z-Offset. Simulationen werden ignoriert. Daemon-Start mid-Job → Job ab jetzt, Flag `partial` + Startlage |
| Werkzeugwechsel | nicht als Event (nicht gewählt); `currentTool` wird als Kontext an Events mitgeführt |
| Heizer-Rollen | Düsen = Heizer der Werkzeuge (je Heizer gezählt, nicht je Werkzeug, wie `useTemps().nozzles` der CHX-UI); Bett aus `heat.bedHeaterMapping` (RRF 3.7: ein Slot kann mehrere Heizer halten; `heat.bedHeaters` ist veraltet und auf der CHX überall −1); Kammer siehe unten |
| Lagenaggregate | je Heizer min/max/mean + Sollwert am Lagenende; je Düsenheizer Heizlast (§5.4.1); je Extruder Filament befohlen (`totalExtrusion`-Delta) und gemessen (`calibrated.totalDistance`-Delta), Volumenstrom (befohlen × Querschnitt / Dauer); Monitor-Prozent min/max/mean/std; Hotend `avgPwm`/`current` std bei konstantem Sollwert; Dauer, Höhe, `fractionPrinted` |
| Kammer | konfigurierbar: auto aus `heat.chamberHeaterMapping`, sonst Analogsensor mit Namen „SZP coil“ (wie `useJobAnalysis.chamberChannel` der CHX-UI), überschreibbar durch Heizer- oder `sensors.analog`-Index |
| Job-Kontext | `job.file` (Name, Slicer, Lagenhöhe, Lagenzahl, Zeit, Filament, Größe, Datum, **CRC32** mit `zlib.crc32`); **Slicer-Einstellungen aus dem `CONFIG_BLOCK` am Dateiende** (§5.7); Extruder (`stepsPerMm, pressAdv, nonlinear, filament, filamentDiameter`), Werkzeug; Filamentmonitor `configured`+`calibrated`; Heizermodelle (PID, heatingRate, maxPwm); `move.shaping`; Firmware/DSF/Plugin-Version, Boardnamen; **Maschinen-Globals** aus einer konfigurierbaren Liste (§5.9), Schnappschuss bei Start und Ende |
| Job-Zusammenfassung | Filament: Verhältnis gemessen/befohlen, `avgPercentage` und `calibrated.mmPerRev` am Jobende, Prozentwert-Verteilung (2 %-Klassen), Kennlinie Fluss vs. Prozent (gebinnt); Thermik: Aufheizzeit je Heizer, **Heizlast je Düsenheizer** (§5.4.1), Kammer min/max/mean; MFM der Maschine: tolerierte Fehler, Recoveries mit Ergebnis, vorgeschlagene/angewandte E-Steps; Ereignisse: Anzahl je Typ, erste/letzte Lage, Abbruchursache; Spulenverbrauch (g) aus `spool_remaining` Start − Ende; Mechanik: Peak-Frequenz und RMS je Achse je Spektrum, Job-Mittel |
| Accelerometer | **bedingt**: nur wenn ein Board `accelerometer != null` meldet (2026-09-26 auf der CHX 350 bei keinem der 7 Boards). Dann: **alle 15 min** im Zustand `processing` `M956 P<M955-Nummer> S1000 A0 F"qa-<job>-<ts>.csv"` (RRF 3.7: P ist die logische Nummer aus M955 ohne Boardadresse, rc.1 kann nur P0; nur ein Sensor gleichzeitig), bei Pause angehalten; CSV aus `0:/sys/accelerometer/` lesen, letzte Zeile (Datenrate, Overflows) prüfen und bei Overflows > 0 verwerfen, FFT in reinem Python wie @duet3d/motionanalysis (seit 2026-09-27, vorher numpy), Spektrum in DB, CSV löschen. Referenz: automatisch (Median der ersten N) mit manueller Übersteuerung |
| Histogramm | je Lage: gemessen vs. befohlen je Extruder |
| Replay | Lage für Lage: Toolpath aus der G-Code-Datei (**on demand im Daemon geparst, nichts gespeichert**, CRC32-Prüfung), Bahn eingefärbt nach befohlener Volumenflussrate (E-Delta × Querschnitt / Segmentdauer), gemessene `extrusionRate`-Samples als Marker darüber, Events als Marker, `currentObject` je Segment/Event, Temperatur- und Heizlastkurven der Lage, Zeitraffer-Frame der Lage. Slicer: PrusaSlicer/Orca/SuperSlicer, Cura, Simplify3D, Z-Heuristik als Fallback |
| Zeitraffer | Snapshot von einer konfigurierbaren Kamera-URL je Lagenwechsel, nach Jobende als AV1-Video (nur AV1, SVT-AV1 über ffmpeg); Frame je Lage in Analyse/Replay, Video als Download (§5.11) |
| QC-Zugriff | ausschließlich HTTP-API (JSON); Notizen/Bewertung/Befunde/Prüfbericht gehören in QC, QA hat keine Eingabefelder |
| Live | WebSocket-Endpunkt (`HttpEndpointType.WebSocket`) für Live-Samples und Events; läuft am HMI über das eigene WebSocket-Backend des haproxy (§5.6) |
| UI | QA-Seite (klassisches DWC): Jobliste, Jobdetail, Replay, Zeitraffer, Trends (+ Spektrenvergleich), Einstellungen; CHX-UI: Verlauf und Analyse aus der QA-API (§5.10); Chart.js 4 + Canvas 2D; i18n **en + de** via `registerPluginMessages` |
| Export | Job als JSON (Kontext, Zusammenfassung, Lagen, Events, Spektren); Zeitraffer als Video-Download |
| Settings | `settings.json` unter `/opt/dsf/sd/QualityAssurance/`, Einstellungs-Tab; `plugin.data` nur Status-Summary |
| Workflow | PR-Track (nie direkt `main`), Releases `main → release → Tag`, GitHub Actions CI + `scripts/ci-local.sh`, pytest mit gemocktem dsf, Vitest + `dwc-plugin-test-kit` gegen echtes Vuetify 4, API-Vertragstests, `CLAUDE.md` mit Verifikationsdatum je API-Aussage |

## 4. Zielwirkungen → Kennzahlen

| Wirkung | Signal in QA |
|---|---|
| Verschleiß Antriebsrad/Andruckrolle/Lager | Trend Verhältnis gemessen/befohlen, `avgPercentage`/`calibrated.mmPerRev` am Jobende und `mfm_esteps_suggested` über Jobs; Prozent-Streuung; Häufung von `percent_out_of_window`, tolerierten MFM-Fehlern und Recoveries |
| Teilverstopfung / Heat Creep im Job | Verhältnis fällt innerhalb des Jobs, korreliert mit Lage, Hotend-Temp, Fluss; Heizlast steigt bei gleichem Fluss |
| Flussgrenze des Hotends | Kennlinie Fluss vs. Prozent und **Heizlast vs. Volumenstrom** je Job, Material und Düse; Vergleich mit `global.filament_max_flow_rate` |
| Hotend an der Schmelzleistungsgrenze | `heater_load`-Events high/limit mit Lage, Koordinate, Volumenstrom; Zeitanteil ≥ 80/90 % je Lage |
| Falsche E-Step/Monitor-Kalibrierung | Kontext-Schnappschuss je Job vs. Historie gleichen Materials; Flow-Bias-Events der Maschine |
| Materialschwankung (Durchmesser, thermisch) | Prozent-Streuung je Lage; avgPwm/current-Streuung bei konstantem Sollwert; Spule/Material im Kontext |
| Kammerverlauf (Warping) | Kammer min/max/mean je Lage |
| Nachlassende Heizleistung | Heizlast bei Sollwert je Düsenheizer über Jobs, gruppiert nach Sollwert, Material und Düsendurchmesser; Aufheizzeit |
| Heizer-Faults / M143 | Event mit Lage und Koordinate |
| Temperatureinbruch durch Lüfter/Tür | Block (voll aufgelöst) mit Lüfterwert und Temperatur um den 5-K-Sprung |
| Riemenspannung, Lager, Shaper, lose Teile | Peak-Frequenz, RMS, neue Peaks relativ zum Referenzspektrum; Vergleich mit `move.shaping` (nur mit Accelerometer) |
| Häufung an Bettpositionen/Objekten | Event-Heatmap über Jobs (machinePosition), Objektzuordnung |
| Lagenzeit/Fluss ↔ Ereignisse | Events tragen Lage, Lagenaggregat, Fluss zum Zeitpunkt |
| Sichtbarer Druckfehler | Zeitraffer-Frame der Lage neben Kanälen und Events |
| Pausen-Auswirkung | Pause/Resume-Events mit Ursache; Block um Pause |
| Abbruchursachen über Jobs | Zusammenfassung `abort_reason`, Jobliste |
| Spannungseinbruch / ESD | vIn-Event, Phantommessung-Event, Treiberfehler-Event mit Koordinate |

## 5. Architektur

### 5.1 Repository-Layout (3.7-only, an Vigil angelehnt, ohne ui36-Zweig)

```
plugin.json
package.json                     Vue 3 Toolchain (DWC-3.7-Builder liest hier)
pyproject.toml                   pytest, pythonpath = ["dsf"]
src/
  index.ts                       registerRoute + registerPluginMessages (@/plugins), ensureBackendRunning, unregister bei dwcPluginUnloaded
  host.ts                        Pinia-Host-Adapter (pluginEntry, startBackend) — wie dwc-vigil/src/ui37/host.js
  core/                          framework-neutral: api.ts, backend.ts (pid-basiert wie CHX350/backend.ts), ws.ts, charts.ts,
                                 format.ts, replay/{parser-types,renderer}.ts
  i18n/{en,de}.json
  QualityAssurance.vue           Seite mit Tabs: Jobs | Job | Replay | Zeitraffer | Trends | Settings
  components/                    JobList, JobDetail, ChannelChart, LayerHistogram, EventList, ContextTable,
                                 ReplayCanvas, ReplayControls, TimelapseViewer, TrendChart, SpectrumCompare, SettingsForm, BackendBanner
dsf/
  qa-daemon.py                   Patches (3.7-Teilmenge aus vigil-daemon.py), Logging (_DeferredWarnings), Connections, Threads, Shutdown
  qa_collector.py                Subscribe-Loop, Ringpuffer, Kanal-Extraktion, Trigger, Layer-Tracking, Events, Lifecycle
  qa_heaterload.py               Heizlast: Rollen, Tracks, 60-s-Fenster, Stufen mit Hysterese, Lagen-Akkumulatoren (§5.4.1)
  qa_machine.py                  Maschinen-Globals: MFM-Signale (§5.4.2), gehärtete Booleans, Kontextliste
  qa_context.py                  Kontext-Schnappschuss, CRC32 (zlib)
  qa_slicer.py                   CONFIG_BLOCK-Parser (aus CHX350/dsf/chx350_gcode.py übernommen, reine Funktionen + Tests)
  qa_summary.py                  Lagenaggregate, Job-Zusammenfassung
  qa_db.py                       SQLite: Schema, Migrationen, Writer-Thread, Durability, Backup/Restore, Retention
  qa_timelapse.py                Snapshot bei Lagenwechsel, Frame-Index, Encoder-Worker, Frame-Extraktion (§5.11)
  qa_accel.py                    M956-Scheduler, CSV-Reader, FFT (reines Python), Referenzspektrum
  qa_gcode.py                    G-Code-Parser: Lagenindex, Segmente, Fluss, Objekte
  qa_api.py                      HTTP-Handler-Registry + WebSocket-Broadcaster
  qa_settings.py                 settings.json laden/speichern/validieren, Defaults
tests/                           test_*.py, core/*.test.ts, components/*.test.ts, api-contract.test.ts
scripts/                         version.js, ci-local.sh (aus Nachbarn, ohne 36-Stage)
.github/workflows/{ci.yml,release.yml}
docs/                            api.md, schema.md, vigil-candidates.md, image.md (AppArmor-Block, plugins.list, apt-Abhängigkeiten)
CLAUDE.md, README.md
```

### 5.2 plugin.json

```jsonc
{
  "id": "QualityAssurance", "name": "Quality Assurance", "author": "Meltingplot GmbH",
  "version": "0.1.0", "license": "MIT",
  "homepage": "https://github.com/Meltingplot/dwc-quality-assurance",
  "dwcVersion": "auto-major", "sbcRequired": true, "sbcDsfVersion": "auto-major",
  "sbcExecutable": "qa-daemon.py", "sbcOutputRedirected": true, "sbcAutoRestart": true,
  "sbcPermissions": ["commandExecution", "objectModelReadWrite", "registerHttpEndpoints",
                     "fileSystemAccess", "readGCodes", "readSystem", "writeSystem",
                     "networkAccess", "launchProcesses"],
  "sbcPythonDependencies": ["dsf-python"],
  "sbcPackageDependencies": ["ffmpeg"],
  "data": { "status": "idle", "currentJobId": "", "lastJobId": "", "dbSizeBytes": "0", "lastError": "" }
}
```
`readGCodes` für Toolpath-Parser, CRC32 und Slicer-Block; `readSystem`/`writeSystem` für Lesen
und Löschen der M956-CSV unter `0:/sys/accelerometer/` (nur mit Accelerometer); `networkAccess`
für den Kamera-Snapshot; `launchProcesses` für ffmpeg (Zeitraffer). `ffmpeg` ist als apt-Paket
deklariert und wird vom Image-Build mitinstalliert (Entscheidung Tim 2026-09-26; der Build muss das
noch lernen, §5.12). Kein numpy: die Spektren rechnet QA in reinem Python (2026-09-27). `sbcAutoRestart`: DSF startet den Daemon 2 s nach
einem unerwarteten Ende neu; ein gewolltes Stoppen setzt vorher pid 0 und löst keinen Neustart aus
(DSF `SetPluginProcess.cs`, `StopPlugin.cs`). `sbcData` wird nicht verwendet. Die Permissions
allein genügen auf dem Image nicht, maßgeblich ist der AppArmor-Block (§5.12).

### 5.3 Daemon-Threads

1. **Collector** (Hauptthread): `SubscribeConnection(SubscriptionMode.PATCH)` ohne Filter
   (Vigil-Muster; `messages[]` nur im Patch-Modus zuverlässig). Erstaufruf
   `get_object_model()`, danach `get_object_model_patch()` + `update_from_json`.
   `TimeoutError` (3 s) ist der Heartbeat für 5-s-Tick, Heizlast-Fenster, Retention-Check, Shutdown.
2. **DB-Writer**: Queue → Transaktion alle 30 s oder bei Event sofort (`commit`).
3. **HTTP/WS**: dsf-python startet **je Endpunkt** einen eigenen Thread (`ThreadPoolExecutor(max_workers=1)`)
   mit eigenem asyncio-Loop. Folgen (Lehre aus dem CHX350-Backend, 2026-09-26):
   - eine gemeinsam genutzte `CommandConnection` (z. B. `resolve_path`) nur hinter einem `threading.Lock`;
     sonst vermischen gleichzeitige Anfragen die Antworten von DSF und ein Endpunkt hängt dauerhaft;
   - SQLite: eine Leseverbindung je Endpunkt-Thread (WAL erlaubt parallele Leser), geschrieben wird nur im DB-Writer;
   - WS-Broadcast aus dem Collector per `loop.call_soon_threadsafe` in den Loop des WS-Endpunkts;
   - `read_request` liest höchstens 32 KiB: POST-Bodies (Settings, Referenz) klein halten.
4. **Timelapse-Worker**: holt den Snapshot beim Lagenwechsel (Signal vom Collector), schreibt Frame + Index;
   nach Jobende Encoding (§5.11).
5. **Accel-Worker**: sendet M956 über die gemeinsame `CommandConnection` (hinter dem Lock, SBC-Kanal), wartet auf
   das `sensors.accelerometers[n].runs`-Inkrement (vom Collector signalisiert; DSF 3.7 hat das Accelerometer
   von `boards[]` nach `sensors.accelerometers` verschoben), liest CSV, FFT, schreibt Spektrum, löscht CSV.

Monkey-Patches aus `dwc-vigil/dsf/vigil-daemon.py` übernehmen, aber nur die mit Tag
`[both]` oder `[3.7 only]`: `BoardState`/`Axis.letter`-Setter, `_connect`-Greeting-Read
(DSF 3.7 sendet einen längeren Gruß als dsf-pythons `recv(50)`; das CHX350-Backend hat
dieselbe Korrektur), `_set_model_prop(None)`, generischer `_missing_`-Hook +
`_KNOWN_ENUM_ADDITIONS`, `_DeferredWarnings` (Warnungen via `write_message`, Fehler auf stderr).

### 5.4 Erfassungslogik (qa_collector.py)

- **Job-Erkennung**: `state.status` → `processing` bei gesetztem `job.file.fileName`
  startet Job (`simulating` ignoriert). Ende bei Wechsel weg von processing/paused/… mit
  `job.lastFileCancelled` / `lastFileAborted` → Ergebnis. DSF setzt die Ergebnis-Flags erst
  nach dem Wechsel: auf die Flags warten wie Vigil seit fef16f0 („wait for DSF's job outcome
  flags before counting a finished job“). Job-ID `YYYYMMDD-HHMMSS-<8 hex crc32(fileName + fileCrc32)>`.
- **Layer-Tracking**: eigener Zähler auf `job.layer`-Wechsel (nicht `job.layers[]`,
  siehe Hinweis in `Job.cs:124-128`; `job.layers[]` hält nur abgeschlossene Lagen und seine
  `temperatures` sind kompakt, d. h. ohne leere Sensor-Slots). Lagenaggregate laufen als
  inkrementelle Akkumulatoren (min/max/sum/sumsq/count je Kanal) und werden am Lagenwechsel geschrieben.
- **Ringpuffer**: `collections.deque` von `(ts_ms, {channel: value})` je Patch, Verwurf
  älter als 90 s. 5-s-Tick schreibt den jüngsten Zustand als `resolution=coarse`.
  Trigger → Puffer als Block `resolution=fine` in DB, `fine_until = now + 30 s`,
  Folgetrigger verlängert; Block-ID an alle Samples und auslösenden Events.
- **Events**: Typkatalog `filament_status`, `filament_percent_window`, `heater_fault`,
  `heater_monitor`, `heater_load`, `mfm_error_tolerated`, `mfm_recovery`, `mfm_flow_bias`,
  `voltage_dip`, `phantom_reading`, `driver_error`, `setpoint_change`, `job_start`, `job_end`,
  `pause`, `resume`, `babystep`, `timelapse_failed`, `accelerometer_failed`,
  `daemon_started_mid_job`. Jedes Event: `ts, layer, machine_pos{}, workplace, offsets{},
  current_tool, current_object, extruder/heater/board-Index, payload JSON, block_id`.
- **Sollwert-Änderung**: Vergleich alter/neuer Wert je Patch für die definierten Felder.
- **Pause-Ursache**: `global.mfm_recovery_requested` gesetzt → `filament_monitor`; sonst
  nächstes Event innerhalb 10 s vor der Pause (`filament_*`, `mfm_*`, `heater_*`,
  `driver_error`), sonst `messageBox`-Titel, sonst `user`.
- **Mehrere Düsen**: Heizer, Extruder, Filamentmonitore und Werkzeuge werden bei jedem
  Patch aus dem Objektmodell gelesen, nie fest verdrahtet (heute 1 Düse, vorbereitet 2,
  später bis 16, davon zwei gleichzeitig druckend).

#### 5.4.1 Heizlast (neu)

Gleiche Definition wie das Heizlast-Banner der CHX-UI (`src/plugins/CHX350/stores/heaterLoad.ts`,
Schwellen von Tim am 2026-09-25 bestätigt), damit Banner und Aufzeichnung übereinstimmen:

- **Wert:** `load = clamp(avgPwm / model.maxPwm, 0, 1)` (`maxPwm ≤ 0` → 1). `avgPwm` ist ein
  gleitender Mittelwert der Heizer-PWM über etwa 5 s, Skala 0…1 (Wiki, Gcodes.md M573); die
  Firmware begrenzt die PWM auf `model.maxPwm` (RRF `LocalHeater.cpp`), daher die Normierung.
- **Welche Heizer:** Events und Stufen nur für Düsenheizer (Heizer der Werkzeuge, je Heizer
  einmal). Bett und Kammer bekommen Lagenaggregate und Zusammenfassung, keine Events.
- **Wann gezählt:** nur im Zustand `processing`, Heizer `active` mit Sollwert > 0 und erst,
  nachdem der Sollwert erreicht wurde (`current ≥ active − 2 K`). Ein Sollwertwechsel
  (Erstlagentemperatur, Standby → Active) oder eine Pause beginnt neu. Sinkt die Temperatur
  nach dem Erreichen unter den Sollwert, wird weiter gezählt (das ist der Überlastfall selbst).
- **Fenster:** zeitgewichteter Mittelwert über 60 s; gültig erst, wenn das Fenster zu
  mindestens 75 % mit Daten belegt ist (die UI verlangt 45 von 60 Sekunden-Samples).
- **Stufen:** `high` ab 80 %, `limit` ab 90 %; eine Stufe endet erst 5 Punkte unter ihrer
  Schwelle (Hysterese).
- **Event `heater_load`:** Beginn beim Eintritt in `high` oder `limit`, Stufenwechsel als
  Payload, Ende beim Verlassen; Payload: Heizer, Werkzeug, Sollwert, Stufe, Spitzenwert des
  60-s-Mittels, Dauer, Volumenstrom und `speedFactor` zum Zeitpunkt. Das Event löst wie jedes
  Event einen voll aufgelösten Block aus.
- **Lagenaggregat** je Heizer (`load_stats`): mean, max, p95 der Last bei Sollwert, höchstes
  60-s-Mittel, Zeitanteil bei Sollwert, Zeitanteil ≥ `high` und ≥ `limit`, Sollwert.
- **Zusammenfassung** je Düsenheizer: mittlere Last bei Sollwert (je Sollwert getrennt),
  höchstes 60-s-Mittel, Zeitanteil ≥ `high`/`limit`, Anzahl `heater_load`-Events, erste Lage mit Event.
- **Trends:** `heater_load_mean` über Jobs, gruppiert nach Heizer, Sollwert, Material
  (`filament_settings_id` bzw. `extruder.filament`) und Düsendurchmesser. Referenzwert der
  Maschine (2026-09-25): T0 braucht beim Drucken von PLA mit 0,8-mm-Düse bei 220 °C etwa 45–60 %.
- **Kennlinie:** Last vs. Volumenstrom je Job (gebinnt), um die Schmelzleistungsgrenze je
  Material und Düse zu sehen.
- Schwellen, Fenster und Toleranz stehen in den Settings (§5.9); Defaults = Werte der CHX-UI.
  Ändert sich eine Seite, wird die andere mitgezogen (Hinweis in beiden `CLAUDE.md`).

#### 5.4.2 Maschinensignale der MFM-Auswertung (neu, CHX-spezifisch)

chx350-config wertet den Filamentmonitor (Rotating Magnet auf dem Toolboard, CAN 20) selbst aus
und legt den Zustand in Globals ab (Semantik siehe chx350-config `CLAUDE.md`, Abschnitte „MFM“).
QA liest diese Globals, wenn vorhanden; fehlen sie (andere Maschine), entfällt der Block ohne Fehler.
Gehärtete Booleans der Konfiguration dekodieren: `1431655765` = true, `2863311530` = false.

| Signal | Globals | QA |
|---|---|---|
| Tolerierter MFM-Fehler (die ersten 3 Fehler P=4/P=5 innerhalb 30 mm pausieren nicht) | `mfm_error_count`, `mfm_error_time`, `mfm_error_start_pos` | Event `mfm_error_tolerated` bei jedem Anstieg von `mfm_error_count`; Kanal |
| Auto-Recovery im `pause.g` | `mfm_recovery_requested`, `mfm_recovery_result` (−1 = nicht gelaufen), `mfm_recovery_resume_time` | Event `mfm_recovery` mit Ergebnis; Pause-Ursache `filament_monitor` |
| Flow-Bias-Erkennung (`filament/mfm-flow-bias.g`, alle 60 s, 600 s eingeschwungen, ±3 % Totband) | `mfm_esteps_detected`, `mfm_esteps_suggested` (±5 % begrenzt), `mfm_esteps_baseline`, `mfm_esteps_drift_avg`, `mfm_esteps_drift_since` | Event `mfm_flow_bias` beim Setzen von `detected`; Kanäle |
| Angewandte E-Step-Korrektur (`M92` in `filament-error.g`, erneut in `resume.g`, zurück in `finish.g`) | `move.extruders[].stepsPerMm` + obige Globals | `setpoint_change` von `stepsPerMm` mit Ursache `mfm_flow_bias`, wenn der neue Wert `mfm_esteps_suggested` entspricht, `baseline_restore` beim Zurücksetzen |
| Unterdrückung | `mfm_suppress_until`, `mfm_ignore_events` | nur Kontext an MFM-Events |

Firmware-Meldungen (`M118`) mit Filamentbezug bleiben weiterhin **kein** Event (§8); die
strukturierten Globals decken dieselbe Information ab.

### 5.5 Datenbank (qa_db.py)

Pfad `/opt/dsf/sd/QualityAssurance/qa.db`, Backups `qa.backup.1.db`, `qa.backup.2.db`.
`PRAGMA journal_mode=WAL; synchronous=FULL; foreign_keys=ON`. Beim Start `quick_check`;
Fehler → Datei nach `qa.db.corrupt.<epoch>` verschieben, jüngstes Backup mit bestandenem
`quick_check` zurückkopieren, sonst leere DB. Nach Jobende `integrity_check` → `VACUUM INTO`
neues Backup, Rotation auf 2. `schema_meta.version` mit Migrationen.

Tabellen:
- `jobs(id PK, file_name, started_at, ended_at, result, partial, start_layer, num_layers, duration_s, warmup_s, pause_s, material, context JSON, summary JSON, raw_pruned)`
- `job_layers(job_id, layer, started_at, ended_at, duration_s, height, fraction_printed, filament JSON, flow JSON, temps JSON, fm_stats JSON, pwm_stats JSON, load_stats JSON, PK(job_id, layer))`
- `channels(id PK, name UNIQUE)` — z. B. `heater.1.current`, `heater.1.avgPwm`, `fm.0.avgPercentage`, `axis.X.machinePosition`, `board.0.vIn`, `global.mfm_error_count`
- `samples(job_id, channel, ts_ms, value REAL, resolution, block_id, PK(job_id, channel, ts_ms)) WITHOUT ROWID`
- `blocks(id PK, job_id, start_ms, end_ms, triggers JSON)`
- `events(id PK, job_id, ts_ms, type, subtype, layer, x, y, z, positions JSON, workplace, offsets JSON, tool, object_id, index, payload JSON, block_id)`
- `timelapse(job_id PK, status queued|capturing|encoding|done|failed, codec, path, frames, fps, size_bytes, layer_frames JSON, error)`
- `spectra(id PK, job_id, ts_ms, layer, board, axis, sampling_rate, n_samples, freqs JSON, amplitudes JSON, peak_hz, rms, source)`
- `reference_spectra(axis PK, spectrum_id, mode auto|manual, set_at)`

Retention (im Heartbeat, max. 1×/10 min): Rohdaten (`samples`, `blocks`) der Jobs
außerhalb 50 Jobs/90 Tage löschen, `raw_pruned=1`; danach bei DB > 2 GB weiter älteste
Rohdaten löschen; `PRAGMA wal_checkpoint(TRUNCATE)` nach Prune. Zeitraffer-Videos nach
eigener Grenze (§5.11), unabhängig von der DB.

### 5.6 HTTP-API (exakte Pfade, Parameter als Query) — `docs/api.md`

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `status` | Daemon-Status, aktueller Job, DB-Größe, Retention-Stand, Zeitraffer-Encoder |
| GET/POST | `settings` | Einstellungen lesen/schreiben (validiert, Reload ohne Neustart) |
| GET | `jobs?limit&offset&result&material` | Jobliste mit Summary-Auszug (Felder für die CHX-UI siehe §5.10) |
| GET | `job?id` | Kontext + Zusammenfassung |
| GET | `job/layers?id` | Lagenaggregate + Histogrammdaten, inkl. Heizlast |
| GET | `job/events?id&type` | Events |
| GET | `job/samples?id&channels&from&to&resolution` | Zeitreihen (coarse/fine/auto), inkl. abgeleiteter `heater.<n>.load` |
| GET | `job/blocks?id` | Blockliste |
| GET | `job/spectra?id` | Spektren |
| GET | `job/toolpath?id&layer` | Segmente der Lage (x0,y0,x1,y1,z,e,flow_mm3s,object,type) + Lagenindex-Meta; 409 wenn Datei fehlt/CRC abweicht; 202 solange der Lagenindex noch gebaut wird |
| GET | `job/timelapse?id` | Video (`HttpResponseType.File`, DSF liefert die Datei aus); 404 solange nicht fertig |
| GET | `job/timelapse/meta?id` | Status, Codec, fps, Frames, Zuordnung Lage → Frame |
| GET | `job/timelapse/frame?id&layer` | JPEG des Frames der Lage: während des Drucks die aufgenommene Datei, danach aus dem Video extrahiert |
| GET | `job/export?id` | Job als JSON |
| GET | `trends?metric&limit&material` | Kennzahl über Jobs (u. a. `heater_load_mean`, `fm_avg_percentage`, `esteps_suggested`) |
| GET/POST | `spectra/reference` | Referenz lesen / manuell setzen / auf auto zurücksetzen |
| GET | `channels` | Kanalkatalog |
| WS | `live` | Frames `{type: sample|event|layer|job|heater_load, ...}` |

**Weg durch den haproxy des HMI** (`/etc/haproxy/haproxy.cfg`, gelesen am 2026-09-26, Stand 16:02):
Browser erreichen DSF (10.42.0.2) nur über den haproxy auf dem HMI. Er verteilt nach Art der Anfrage:

| Anfrage | Backend | Grenze |
|---|---|---|
| WebSocket (`Upgrade: websocket`), also DWC-Objektmodell und QA `live` | `websocket_backend` | keine Verbindungsgrenze, `timeout tunnel 1h` (DSF pingt alle 30 s) |
| `/machine/code` | `gcode_backend` | keine Verbindungsgrenze, `timeout server 30m` |
| alles andere, also auch jede QA-GET/POST-Anfrage | `backend_servers` | `maxconn 4 maxqueue 2`, `timeout server/queue 10m` |
| dasselbe, wenn dort schon 4 Verbindungen offen sind | `backend_servers_short` | `maxconn 10 maxqueue 100`, **`timeout server 10s`** |

Folgen für QA:
- `live` braucht keine Proxy-Änderung. Der Endpunkt sendet mindestens alle 30 s einen Frame, damit
  die Verbindung auch bei ruhender Maschine nie an `timeout tunnel` stößt.
- Jede andere QA-Anfrage muss in wenigen Sekunden antworten, weil sie bei ausgelasteten 4 Plätzen im
  Backend mit 10 s Timeout landet. Deshalb: Lagenindex der G-Code-Datei beim Jobstart im Hintergrund
  bauen (nicht erst bei `job/toolpath`), Frame-Extraktion über kurzen Keyframe-Abstand schnell halten,
  große Antworten (Video, Export) als Datei über `HttpResponseType.File` von DSF ausliefern lassen.
- Der WS-Client in `src/core/ws.ts` fällt bei Fehlschlag auf Polling von `status` alle 5 s zurück.

### 5.7 G-Code-Parser (qa_gcode.py, qa_slicer.py)

Lagenindex (Byteoffset je Lage, erkannt über Slicer-Kommentare, Fallback Z-Wechsel bei
Extrusion) einmal je `(file, crc32)` beim Jobstart im Hintergrund erzeugen (§5.6), im RAM und als
`/opt/dsf/sd/QualityAssurance/index/<crc>.json` cachen. `job/toolpath` liest nur den
Byteausschnitt der Lage: absolute/relative E und XYZ (G90/G91/M82/M83), G1/G0/G2/G3
(Bögen segmentiert), Feedrate, `M486 S<n>`/`EXCLUDE_OBJECT_START`, `;TYPE:`.
Fluss = ΔE × π(d/2)² / (Länge/F). Filamentdurchmesser aus Job-Kontext.

**Slicer-Einstellungen:** OrcaSlicer/BambuStudio schreiben ihre komplette Konfiguration zwischen
`; CONFIG_BLOCK_START` und `; CONFIG_BLOCK_END` ans Dateiende; DSF wertet `;customInfo` nur im
Kopf aus, und der Webserver des SBC kennt keine HTTP-Range-Anfragen. `qa_slicer.py` übernimmt
den Parser aus `CHX350/dsf/chx350_gcode.py` (letzte 256 KiB, ausgewählte Schlüssel wie
`filament_settings_id`, `filament_type`, `nozzle_diameter`, `required_nozzle_HRC`,
`curr_bed_type`, `print_settings_id`) samt Tests und speichert das Ergebnis beim Jobstart im
Kontext, weil die Datei später gelöscht sein kann. Kopie statt Aufruf des CHX350-Endpunkts,
damit QA nicht vom CHX350-Plugin abhängt. Meltingplot-OrcaSlicer-Dateien enthalten kein `M73`.

### 5.8 Frontend (QA-Seite)

- `src/index.ts`: `registerRoute` aus `@/plugins` (verifiziert, `src/plugins/index.ts`), Icon
  `mdi-clipboard-check-outline`, i18n über `registerPluginMessages(pluginId, messagesPerLocale)`
  (verifiziert), `ensureBackendRunning`, `dwcPluginUnloaded` → `unregisterRoute`.
- **Backend-Erkennung:** läuft = `model.plugins.get("QualityAssurance").pid > 0`, nicht über
  404 oder registrierte Endpunkte (DSF entfernt Endpunkte eines abgestürzten Daemons nicht aus
  `sbc.dsf.httpEndpoints`). `ensureBackendRunning` wie `CHX350/backend.ts`: auf den Plugin-Eintrag
  warten, bei pid ≤ 0 `startSbcPlugin`.
- Kein direkter `plugin.data`-Zugriff außerhalb `host.ts` (`Map` in 3.7).
- Charts: Chart.js 4 via `src/core/charts.ts` (Konfig-Builder wie Vigil), Komponenten nur create/apply/destroy.
- Replay: `ReplayCanvas` (Canvas 2D, Zoom/Pan, Bahn als Polyline mit Farbskala nach Fluss,
  Events als Marker, gemessene Samples als Punkte, Objektfilter), `ReplayControls`
  (Lagenschieber, Play, Geschwindigkeit), Temperatur- und Heizlastkurven der Lage und der
  Zeitraffer-Frame daneben.
- `TimelapseViewer`: Video mit Lagenschieber, Download-Knopf.
- WS-Client in `src/core/ws.ts` mit Reconnect/Backoff; Fallback Polling `status` alle 5 s wenn WS fehlschlägt.
- Im DEV-Server von DWC laden externe Plugins nicht; die UI wird über das gebaute Zip bzw. im
  Vitest mit `dwc-plugin-test-kit` geprüft.

### 5.9 Settings (`settings.json`, Defaults)

```jsonc
{
  "sampleIntervalS": 5, "ringBufferS": 90, "postTriggerS": 30,
  "thresholds": { "temperatureK": 5, "filamentPercentPoints": 15, "vInPercent": 10, "phantomJumpK": 15 },
  "filamentPercentWindowMinS": 5,
  "heaterLoad": { "high": 0.8, "limit": 0.9, "hysteresis": 0.05, "windowS": 60, "minCoverage": 0.75, "reachedToleranceK": 2 },
  "chamber": { "mode": "auto" | "heater" | "sensor", "index": null, "autoSensorName": "SZP coil" },
  "contextGlobals": ["nozzle_type", "nozzle_diameter", "filament_diameter", "bed_surface",
                     "spool_net_weight", "spool_remaining", "spool_tare", "spool_density",
                     "filament_max_flow_rate", "machine_mode"],
  "machineSignals": { "mfm": true },
  "timelapse": { "enabled": true, "snapshotUrl": null, "trigger": "layer", "minIntervalS": 2,
                 "fps": 30, "keyframeInterval": 30, "crf": null, "preset": null, "encoderThreads": 2,
                 "keepFramesOnFailure": true, "retention": { "jobs": 50, "maxBytes": 10737418240 } },
  "accelerometer": { "board": null, "intervalMin": 15, "samples": 1000, "axes": "XYZ", "referenceAutoCount": 5 },
  "retention": { "jobs": 50, "days": 90, "maxDbBytes": 2147483648 },
  "commitIntervalS": 30
}
```
`accelerometer.board` (CAN-Adresse, auf der CHX 350 60 = SZP) wählt das Accelerometer; `null` =
keine Aufnahmen, keine automatische Wahl (Tim 2026-09-27, ersetzt `enabled: "auto"`).
`contextGlobals` ersetzt `batchGlobalVariable` (`filamentBatch` gibt es auf der CHX nicht).
Fehlende Globals werden übersprungen. `snapshotUrl` ist Pflicht für den Zeitraffer: ohne URL nimmt QA
keine Frames auf und meldet das im `status`. `crf`/`preset` `null` = SVT-AV1-Defaults, nach Messung
festlegen (§7). `encoderThreads` (SVT-AV1 `lp`, ergänzt 2026-09-27): ein angehaltener Encoder behält
seinen Speicher während des Drucks; bei 1984×1080 gemessen 0,57 GB mit 2, 0,95 GB mit 4 Threads
(`dsf/qa_timelapse.py`).

### 5.10 Integration in die CHX-UI (neu)

Die Maschine zeigt das CHX350-Layout; die QA-Seite ist nur im klassischen DWC erreichbar
(Service › „Klassisches DWC“). Die CHX-UI ist auf QA vorbereitet, die Anpassungen dort
passieren im DWC-Fork (`src/plugins/CHX350`), nicht im QA-Repo:

- **Verlauf** (`composables/useJobHistory.ts`, „die Naht“ laut Kommentar): Quelle in dieser
  Reihenfolge: QA `jobs` (QA-pid > 0) → CHX350-Backend `history` → Ereignisprotokoll im
  Browser. QA `jobs` liefert je Eintrag mindestens `id`, `file`, `result`
  (`running|finished|cancelled|aborted`), `printTimeS`, `timestamp` (ISO) und `analysable: true`.
  Damit wird „Analyse folgt“ bei älteren Jobs zu „Analyse öffnen“.
- **Analyse** (`composables/useJobAnalysis.ts`): heute aus `job.layers[]` für den laufenden
  bzw. letzten Job. Für ältere Jobs liefert QA `job/layers` dieselben Kanäle:
  Lagendauer, Höhe (kumuliert), Filament je Lage (kumuliert), Temperaturen je Sensor
  **mit Sensorindex und Name** (nicht kompakt wie `job.layers[].temperatures`),
  Kammerkanal nach der Kammerregel, **Heizlast je Düsenheizer** (mean bei Sollwert,
  Zeitanteil ≥ 80 %), MFM-Prozent, Volumenstrom. Die CHX-Seite bildet das auf ihr
  `LayerChannel`-Format ab (key, label, unit, values je Lage, precision, range).
- **Platzhalter der Analyse-Seite:** Bauteilansicht = Replay (`job/toolpath`), Zeitraffer =
  `job/timelapse/*`, Befunde und Prüfbericht = Quality Control (nicht QA).
- **Heizlast-Banner** der CHX-UI bleibt live im Browser (funktioniert ohne QA); QA zeichnet
  dieselbe Größe auf. Konstanten auf beiden Seiten gleich halten (§5.4.1).
- **Jobprüfung vor dem Start** bleibt beim CHX350-Backend (`fileinfo`).

### 5.11 Zeitraffer (neu)

- **Quelle:** JPEG-Snapshot von der URL in `timelapse.snapshotUrl` (konfigurierbar, Entscheidung Tim
  2026-09-26). Auf der CHX 350 ist das vom SBC aus **`http://10.42.0.1/snapshot`** (Tim, am
  2026-09-26 vom SBC aus geprüft: 200, `image/jpeg`, 104 888 Bytes in 0,02 s). Dahinter: der haproxy
  des HMI reicht `/snapshot` an `/0/current` von `hmi-motion.service` (motion, `127.0.0.1:8081`)
  weiter; 1984×1080, ca. 105 KB je Bild.
- **Auslöser:** Lagenwechsel (`job.layer`), Mindestabstand `minIntervalS`. Kein Parken des
  Kopfes (würde den Druck verändern).
- **Während des Drucks:** Frames als JPEG nach `/opt/dsf/sd/QualityAssurance/timelapse/<job>/frames/`,
  Index Lage → Frame (Zeitstempel, Lage). Ein fehlender Snapshot wird im Index vermerkt, kein Abbruch.
  Beispiel laufender Job: 1019 Lagen × 105 KB ≈ 107 MB temporär.
- **Encoding nach Jobende, nur AV1** (Entscheidung Tim 2026-09-26): ffmpeg mit `libsvtav1` in MP4,
  eigener Prozess mit `nice 19` und `ionice -c3`, nur solange kein Job druckt; startet ein Job,
  wird der Encoder angehalten (SIGSTOP) und danach fortgesetzt. Der Pi 5 hat keinen
  Hardware-Encoder, also Software-Encoding. Kurzer Keyframe-Abstand (`keyframeInterval`, Default 30),
  damit ein Einzelbild-Seek schnell ist. Nach dem Encoding `ffprobe`: Framezahl = Index → JPEGs
  löschen; sonst JPEGs behalten (`keepFramesOnFailure`), Event `timelapse_failed`.
- **ffmpeg:** als `sbcPackageDependencies: ["ffmpeg"]` deklariert, der Image-Build installiert es mit
  (§5.12). Debian trixie hat SVT-AV1 und dav1d im ffmpeg: `libavcodec61` (arm64) hängt von
  `libsvtav1enc2` und `libdav1d7` ab (packages.debian.org, 2026-09-26).
- **Ansicht:** Analyse (CHX-UI) und Replay (QA-Seite) zeigen zur gewählten Lage den Frame per Seek
  im `<video>` auf `frame / fps` aus dem Index. Der Kiosk-Browser des HMI ist Chromium (heute
  Chromium 151 auf Raspberry Pi 5, noch Debian 12 bookworm; Ziel laut Tim RPi OS trixie) mit dav1d,
  spielt AV1 also in Software ab. Fallback `job/timelapse/frame?id&layer`: serverseitig mit ffmpeg
  (dav1d) extrahiertes JPEG; während des Drucks die aufgenommene JPEG-Datei.
- **Download:** `job/timelapse?id` liefert das Video (`HttpResponseType.File`).
- **Retention:** eigene Grenze (Default 50 Jobs oder 10 GB, ältestes Video zuerst).

### 5.12 Auslieferung über das Image (neu)

Das Pi-Image hat ein schreibgeschütztes Root (erofs). Seine AppArmor-Richtlinie
`/etc/apparmor.d/opt.dsf.bin.DuetPluginService` enthält im Kindprofil `dsf_plugin_py` je im Image
gebündeltem Plugin einen eigenen Block; ein über Settings › Plugins nachinstalliertes Plugin kann
seinen eigenen Code nicht lesen („must never run code on the machine: this is a CE-marked printer“).
Deshalb:

- Release-Asset `QualityAssurance-<ver>.zip` + sha256 → Eintrag in `layer/mp-dsf.d/plugins.list`
  (`<id> <version> sha256:<hex> <url>`) des Image-Builds (rpi-image-gen, `mp-dsf-plugins`, wie Vigil und CHX350).
- **Image-Build erweitern:** `meltingplot/layer/mp-dsf.d/bin/mp-dsf-plugins` lehnt heute jedes Plugin
  ab, das mehr als dsf-python braucht (`sbcPackageDependencies` und weitere `sbcPythonDependencies`
  sind dort ein Abbruchgrund, Stand rpi-image-gen 78825f4). Für QA muss der Build die deklarierten
  apt-Pakete (ffmpeg) aus den trixie-Quellen ins Image installieren (weitere Python-Pakete braucht
  QA nicht mehr, 2026-09-27). Das ist Arbeit in rpi-image-gen, Auftrag über Tim.
- AppArmor-Block `QualityAssurance` (Muster: Vigil-Block): `r` auf `/opt/dsf/plugins/QualityAssurance.json`,
  `mr` auf `/opt/dsf/plugins/QualityAssurance/**` und `__pycache__`, `rwk` auf
  `/opt/dsf/sd/QualityAssurance/**` (SQLite braucht Locks, WAL- und SHM-Dateien), `r` auf
  `/opt/dsf/sd/gcodes/**`, `rw` auf `/run/dsf/QualityAssurance/**`; mit Accelerometer `rw` auf
  `/opt/dsf/sd/sys/accelerometer/**`; für den Zeitraffer Ausführen von ffmpeg und Netzwerk
  (`network inet stream`) zur Kamera-URL.
- Test mit einem Vorab-Image, gebaut mit der Build-Variablen `IGconf_dsf_plugin_policy=complain` (kein Boot-Parameter; korrigiert 2026-09-27), und
  `journalctl -b --grep 'apparmor="(ALLOWED|DENIED)"'`, danach mit Enforce ohne DENIED.
- Arbeitsauftrag an die rpi-image-gen-Session läuft über Tim; Doku in `docs/image.md`.

## 6. Implementierungsphasen

0. **Verifikation und Gerüst** — offene Punkte aus §7 klären; Repo-Gerüst aus Nachbarn
   übernehmen (`scripts/version.js`, `ci-local.sh` ohne 36-Stage, `ci.yml`, `release.yml`,
   ESLint, Vitest-Config mit `dwc-plugin-test-kit`, `tests/dwc-stubs`); `plugin.json`, `CLAUDE.md`,
   README; GitHub-Repo anlegen (nach Rücksprache). Image-Voraussetzungen als Auftrag formulieren
   (AppArmor-Block, `plugins.list`, apt-Abhängigkeiten im Build).
1. **Daemon-Kern** — Patches, Logging, Connections (inkl. Lock), Subscribe-Loop, Settings,
   DB-Schema + Writer + Durability + Backup/Restore + Retention, Job-Lifecycle (Ergebnis-Flags
   abwarten), Kanäle, 5-s-Tick, Ringpuffer + Blöcke, Events (alle Typen), Sollwert-Events,
   Layer-Tracking + Aggregate, **Heizlast (§5.4.1)**, **MFM-Maschinensignale (§5.4.2)**,
   Kontext-Schnappschuss (inkl. CRC32, Slicer-Block, Globals), Lagenindex im Hintergrund, Job-Zusammenfassung.
   pytest mit gemocktem dsf.
2. **API** — Handler-Registry, alle GET/POST-Endpunkte, WebSocket-Broadcaster, Export;
   API-Vertragstests, inkl. des Vertrags mit der CHX-UI (§5.10).
3. **UI Basis + CHX-Integration** — QA-Seite, Backend-Banner (pid), Jobliste, Jobdetail
   (Charts inkl. Heizlast, Histogramm, Events, Kontext), Settings-Tab, i18n en/de, WS-Live;
   im DWC-Fork: `useJobHistory` und `useJobAnalysis` mit QA als Quelle, Heizlast-Kanal in der Analyse.
4. **Replay** — G-Code-Parser + Lagenindex-Cache, `job/toolpath`, ReplayCanvas mit Flussfärbung,
   Events, Samples, Objektfilter, Temperatur- und Heizlastkurven.
5. **Zeitraffer** — Snapshot bei Lagenwechsel, Frame-Index, Encoder-Worker (AV1),
   `job/timelapse*`, TimelapseViewer, Frame in Analyse und Replay, Retention.
6. **Accelerometer (bedingt)** — nur wenn ein Board einen Sensor meldet: Scheduler, M956, CSV,
   FFT, Spektren, Referenz (auto/manuell), Trends-Tab, Spektrenvergleich.
7. **Abschluss** — `docs/api.md`, `docs/schema.md`, `docs/vigil-candidates.md`, `docs/image.md`,
   CI grün, DWC-3.7-Build mit Manifestprüfung, Release-Workflow, Pin für den Image-Build.

## 7. Vor der Implementierung zu verifizieren (Rule 1: keine geratenen Signaturen)

Erledigt am 2026-09-26:
- ✓ dsf-python: Der Image-Build pinnt **3.7.0b1** (Default `python_version` in
  `meltingplot/layer/mp-dsf.yaml`, `config/duet-pi5.yaml` überschreibt ihn nicht); auf der Maschine
  liegt 3.7.0b1 in den venvs von Vigil und CHX350 (Python 3.13). ffmpeg ist heute nicht installiert. Das Release-Gate
  (`hooks/prebuild05-mp-release-gate`) lässt eine Beta nur in Vorab-Images zu; ein finales Image
  braucht eine finale dsf-python 3.7. In 3.7.0b1: `HttpEndpointType.WebSocket = "webSocket"`,
  `HttpEndpointConnection.is_websocket`, je Endpunkt eigener Thread + Loop, `read_request` ≤ 32 KiB.
- ✓ DWC 3.7 (Fork): `registerRoute`, `unregisterRoute`, `registerPluginMessages` in `src/plugins/index.ts`.
- ✓ RRF `avgPwm` = gleitender Mittelwert über etwa 5 s, 0…1 (Wiki, Gcodes.md M573); PWM auf
  `model.maxPwm` begrenzt (RRF `LocalHeater.cpp`).
- ✓ RRF M956 (Wiki, Gcodes.md, Tab „RRF 3.7 and later“): `P` = logische Nummer aus M955 ohne
  Boardadresse (3.7.0-rc.1 nur P0), `S` Anzahl Samples (Pflicht), `X`/`Y`/`Z` optional (LIS2DW immer
  alle drei), `A0` Pflicht (sofort), `F"name.csv"` optional, Standardordner `0:/sys/accelerometer`;
  nur ein Sensor gleichzeitig. Letzte CSV-Zeile: Datenrate und Overflows, bei Overflows > 0
  verwerfen (Sensors_Accelerometer.md). Dass die Aufnahme parallel zu den folgenden Bewegungen
  läuft, folgt nur aus den Beispielen (`… M956 … G4 P10 G1 X50`), auf Hardware bestätigen.
- ✓ M38 (Wiki: ab 3.6, CRC32 als Hex-String, „Cannot find file“ wenn fehlend). Im SBC-Modus
  berechnet DSF M38 selbst (`MCodeHandler.cs`, `case 38`: erst `FlushAsync` des Kanals, dann CRC32
  als `x8`). QA braucht M38 nicht und nimmt `zlib.crc32`, verglichen wird nur QA-intern.
- ✓ Treiberstatus: Das Wiki beschreibt die Bits von `Driver.status` nicht; die Events
  `driver-error`/`driver-warning` tragen die unteren 16 Bit des Statusworts (Events.md), und
  Statusänderungen von Treibern auf Erweiterungsboards werden nicht von selbst gemeldet
  (CAN_limitations.md). Auf der Maschine: MB6HC-Treiber `status` 65536, alle CAN-Boards 0. QA
  speichert den Rohwert; eine Bit-Dekodierung nur aus dem RRF-Quellcode und als solche markiert.
- ✓ haproxy des HMI gelesen (§5.6); WebSockets haben ein eigenes Backend ohne Grenze.
- ✓ Kamera: motion auf dem HMI (`hmi-motion.service`, `127.0.0.1:8081`), `/snapshot` über haproxy;
  vom SBC aus `http://10.42.0.1/snapshot` erreichbar (200, JPEG, 0,02 s).
- ✓ HMI-Browser: Chromium 151 mit libdav1d auf Raspberry Pi 5 (Debian 12 bookworm) → AV1 in Software.
- ✓ ffmpeg in Debian trixie mit SVT-AV1 (`libsvtav1enc2`) und dav1d (`libdav1d7`).
- ✓ DSF: `sbcAutoRestart` (Neustart nach 2 s, nicht nach gewolltem Stopp); Permissions
  `networkAccess`, `launchProcesses` existieren (`DuetAPI/Utility/SbcPermissions.cs`); registrierte
  Endpunkte bleiben nach Daemon-Absturz stehen.
- ✓ Maschine (Objektmodell): kein Board mit Accelerometer; Filamentmonitor `rotatingMagnet` mit
  `avgPercentage`/`calibrated.mmPerRev`; `chamberHeaterMapping = []`, Kammer = Analogsensor „SZP coil“;
  `bedHeaterMapping = [[0]]`; beide Heizer `model.maxPwm = 1`; `/persistent` 25,7 GB frei.

Offen:
- Wer auf der CHX 350 `snapshotUrl` setzt: einmal in den Einstellungen oder als vorbelegte
  `settings.json` aus dem Image (Default im Plugin bleibt `null`, weil die URL maschinenabhängig ist;
  docs/image.md §4 fragt das beim Image-Build an).
- Image-Build: Installation von `sbcPackageDependencies` (apt) und später weiterer Python-Pakete (§5.12).
- Encodezeit und Videogröße für einen 1000-Lagen-Job mit SVT-AV1 auf dem Pi 5 messen, danach
  `preset`/`crf` festlegen.
- dsf-python-Modelklassen in 3.7.0b1: `RotatingMagnetFilamentMonitor.calibrated` nullable,
  `avg_percentage`, `Layer.filament_usage`, `Extruder.press_adv`, `Board.v_in/v12/mcu_temp`,
  `Driver.status`, `Move.shaping`, `ObjectModel.global`, `Heater.model.max_pwm`.
- ✓ Chart.js wird vom DWC-Builder aus den `node_modules` des Plugins gebündelt (CLAUDE.md, Build seit Phase 3).
- ~~numpy im Plugin-venv~~: entfällt, FFT in reinem Python (2026-09-27).

## 8. Explizit nicht enthalten / bewusst so entschieden

- Keine Notizen, Bewertungen, Befunde, Prüfberichte, Eingabefelder (→ QC). Keine manuelle
  M956-Auslösung per API/Button.
- Fehlgeschlagene M956-Aufnahme: kein Wiederholversuch, nur Event `accelerometer_failed` (nicht gewählt → minimal).
- Keine Events für `filamentPresent`-Wechsel, Werkzeugwechsel, Sollwertabweichung der Heizer,
  Firmware-Meldungen mit Filamentbezug; `fan.requestedValue`, `speedFactor`, `extruder.factor`
  sind Kanäle, keine Sollwert-Events. `heater_load` ist kein Sollwertabweichungs-Event, sondern
  misst die Leistungsreserve.
- Zeitraffer ohne Parken des Kopfes und ohne Eingriff in den Druck.
- Kein DWC 3.6, kein direkter DB-Zugriff für QC, kein Toolpath-Speichern, keine CSV-Exporte.
- Keine Installation über Settings › Plugins (Image-Richtlinie).

## 9. Verifikation

- `pytest tests/ -v` (dsf gemockt, DB-Tests gegen temporäre DB inkl. simuliertem Abbruch
  mitten im Schreiben → Datei bleibt öffnbar; Quarantäne/Restore-Test; Retention-Test;
  Heizlast-Tests: Fenster, Abdeckung, Hysterese, Neustart bei Sollwertwechsel und Pause, Sag
  nach Erreichen zählt weiter; MFM-Signale aus aufgezeichneten Patches; paralleler Zugriff
  zweier Endpunkt-Threads).
- `npm test` (Vitest, echtes Vuetify 4, `[Vue warn]` = Fehler), `npm run lint`,
  API-Vertragstests gegen Handler-Ausgaben, inkl. CHX-Vertrag (§5.10).
- `scripts/ci-local.sh build` → `QualityAssurance-<ver>.zip` mit `plugin.json` im Root,
  `dwc/js/QualityAssurance.*.js`, `dsf/qa-daemon.py`, `dwcVersion` = 3.7.
- **Auf dem Drucker** (die Maschine druckt oft Produktionsjobs: dort nur lesende Prüfungen,
  Eingriffe nur nach Absprache oder auf einem Testgerät): Image mit dem Plugin im Complain-Modus,
  AppArmor-Journal prüfen, dann Enforce. Testdruck; Filamentmonitor-Event provozieren (z. B.
  Monitor-Fenster eng stellen), Pause/Resume, Sollwertänderung per M104; prüfen: Block
  gespeichert, Events mit Koordinaten, Lagenaggregate, Histogramm, Replay der Lage, Export-JSON.
- Heizlast: während eines Jobs das Banner der CHX-UI mit `heater_load`-Events und
  `load_stats` vergleichen (gleiche Stufen zur gleichen Zeit).
- Zeitraffer: Framezahl = Lagen, Encoder läuft nicht während eines Drucks, Video spielt im
  Chromium des HMI und in Firefox, Frame der Lage passt zum Replay, Download funktioniert, ohne
  gesetzte `snapshotUrl` meldet `status` den abgeschalteten Zeitraffer.
- CHX-UI: Verlauf aus QA, „Analyse öffnen“ für einen älteren Job, Heizlast-Kanal in der Analyse.
- Spektren nur mit Accelerometer.
- Stromausfalltest an einem Testgerät: Strom während des Schreibens trennen, danach `quick_check` ok und letzte Events vorhanden.
