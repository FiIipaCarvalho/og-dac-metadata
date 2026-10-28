# Prompt: OceanGliders tag adoption across glider data centres

A single prompt that reproduces the dashboard and analyses built on 2026-10-27/28 (synthesised from Filipa Carvalho's requests in that session). Code that implements it: this repository (`harvest/`, `make_standalone.py`).

---

Build an interactive dashboard, for the OceanGliders steering team and glider data-centre managers, showing how far glider data across data centres meet OceanGliders' requirements (OG1.0 format, `network` and `site` tags from the BOON metadata guidelines, an approved data centre), and use it as a launchpad for organising data management across data assembly centres (DACs), with a federated OceanGliders ERDDAP in mind. Frame everything as readiness, not compliance: the `network`/`site` requirement was only agreed in October 2026.

## Data: harvest every glider deployment from six data centres, from the root of each

1. **IOOS Glider DAC** (https://gliders.ioos.us/erddap): `allDatasets`, then `info/<datasetID>/index.csv` for every dataset. Keep all global attributes, the `platform` variable's `wmo_id` and `type`, the variable list, and `flag_values`/`flag_meanings` of every flag variable.
2. **VOTO** (https://erddap.observations.voiceoftheocean.org/erddap): the `nrt_`, `delayed_`, `OG_nrt_` and `OG_complete_` datasets; same fields.
3. **BODC** (https://linkedsystems.uk/erddap): datasets with glider/Slocum/Seaglider in the title, plus the OG1 file collections `Public_OG1_Data_001` (real time) and `Public_OG1_Data_001_Recovery` (recovery), one EGO deployment JSON per folder (the files can't be read by byte range).
4. **OTN** (https://erddap.oceantrack.org/erddap): Trajectory/TrajectoryProfile datasets.
5. **Coriolis, the OceanGliders GDAC** (https://data-oceangliders.ifremer.fr/): `glider_traj_index.txt` (time coverage, WMO, glider model, owner per file), each deployment folder's JSON, and each NetCDF header read by HTTP byte range.
6. **IMOS ANFOG** (S3 `imos-data`, prefix `IMOS/ANFOG/`): one header per deployment folder (FV01 preferred) by byte range.

Gotchas to handle: some ERDDAPs publish a global attribute `id` that is not the datasetID (store the datasetID separately); netCDF4 isn't thread-safe (read files in processes); check a file's signature before choosing the classic or HDF5 reader (a wrong guess sits through long timeouts); EGO files have no time-coverage attributes and often fill `TIME` but not `JULD`; the Coriolis index `time_coverage_start` has 1970/2000 placeholders and earlier-deployment starts (use `deployment_start_date`, or the folder date when they disagree by more than 60 days); keep Coriolis to a few parallel requests.

## One row per deployment per data centre, with

- **Tag:** `network` starts with "OceanGliders"; mission and region from its `>` parts; **site** from `site` or `oceanGliders_site`.
- **Format:** OG1.0, EGO, IOOS DAC (incl. older Unidata/COARDS files), IMOS, Other; plus OG1 available on any file.
- **Data mode column:** from `data_mode`, else naming, with recovered datasets as P (never D from a name).
- **Stream:** real time (telemetered, often decimated) vs recovered (full resolution). Recovered: IOOS `-delayed`, VOTO `delayed_`/`OG_complete_`, OTN `_delayed`, BODC recovery collection, Coriolis data mode P (verify by sampling resolution of a P vs R file), IMOS mission folders. "Delayed" in names means recovered, not delayed-mode QC.
- **QC level per stream** (highest stated): Delayed mode (manually calibrated / QC'd in delayed mode, data_mode D, IMOS "Delayed Mode"); Automatic QC (automatic, QARTOD, Argo-method, EGO QC-tests manual DOI 10.13155/51485 or EGO processing chain, data_mode A); No QC (no QC, raw, Level 0, "as is", IMOS FV00); else Not stated; BODC folder-only deployments Not read. Check "no QC" wording before "delayed mode", because IOOS writes "Delayed mode dataset" for recovered raw data.
- **QC flags:** present (flag attributes or `_qc` names); CF-described (every flag variable has `flag_values` and an equal-length `flag_meanings`); scheme by majority of flag variables (IOC/QARTOD 1,2,3,4,9; Argo-style 0-9; SeaDataNet L20 with A/B/Q; Other; Not described).
- **Platform:** Slocum, Seaglider, SeaExplorer, Spray or Unknown from attributes, title/summary text and the Coriolis index; drop wave gliders everywhere.
- **Glider-days:** end minus start; leave out spans under 0 or over 400 days.
- **WMO ID**, **active since** two years before the snapshot, **institution** normalised (merge spelling variants and acronyms, keep the raw string), and a **match group** for the same deployment on more than one data centre (same WMO ID, start dates within 3 days).

## Dashboard

- Each data centre has its own colour throughout. Tagged records are drawn in that colour, untagged in grey; categories are told apart by **pattern** in the data centre's colour (solid for the target category, then stripes, dots, fine lines, faded, outline), never by shades of one colour.
- **Tiles**, in three labelled rows: deployments (records, unique, glider-days); OceanGliders network tag (tagged deployments, share, glider-days share, data centres with tags "N of M", institutions tagging "N of M"); OG1.0 format (the same five figures for OG1.0).
- **Deployments by data centre** (tagged solid, untagged faded).
- **By start year**, with a Highlight switch (Tagged | OG1.0), a Records | Glider-days switch and three views: highlighted vs all (grey all, highlighted part coloured by data centre), All by data centre, highlighted only.
- **Where the tags point:** mission > region.
- **Format**, **real-time and recovered data by QC level** (two rows per data centre), **QC flags** (present/CF and scheme) and **platforms**, all with the same three-way switch.
- **Largest institutions** split by data centre.
- **Readiness matrix** by institution and data centre (network tag, site tag, recovered data, delayed-mode QC, WMO ID, active, OG1.0, QC flags, CF flags, glider-days), sortable and searchable, with a Gradient | Traffic light switch (red under 50%, amber 50-80% with a strong amber, green 80% and over).
- **Definitions** for every criterion and caveat.
- **Assumptions and limitations** of the analysis (snapshot scope, metadata not data, free-text QC classification, naming-based streams, deployment keys, matching as an upper bound, shallow CF and format checks, glider-days from metadata, unread BODC files, text-matched platform and institution, AI-built and spot-checked), and **How this dashboard was made** with this prompt, both linked from the top of the page.
- **Findings report** at the end: a lede, data-centre profile table (traffic light), "where data centres behave differently" table (stream separation, meaning of "delayed", QC level statements, flag schemes, format, tags, WMO ID, glider model, time coverage, institution naming, access, processing software), a guidance card per data centre ("missing or incomplete" and "does differently", with its key numbers), recommendations for a federated OceanGliders ERDDAP (one dataset per deployment and stream with R/P/D data mode; controlled QC level; one CF-described flag scheme; required global discovery attributes validated against the BOON vocabulary; ERDDAP-to-ERDDAP harvesting; a shared deployment ID; validation at ingestion), a worked duplication example (NOC on BODC and Coriolis), and a table of the spot checks made by opening files.

## Verify before publishing

Spot-check at least one deployment per data centre against its raw metadata; check every claim in the guidance against the data (counts, shares, who holds the tags); state what was checked by opening files; and keep every number on the page computed from the data.
