# og-dac-metadata

Harvests glider deployment metadata from six glider data assembly centres (DACs) and compares them against OceanGliders' data requirements: the `network` and `site` tags from the [BOON metadata guidelines](https://github.com/OceanGlidersCommunity/BOON), OG1.0 format, real-time vs recovered data, QC level, QC flags, WMO IDs, glider-days and platform. Built in October 2026 for the OceanGliders steering team and IUGC 2026, as a starting point for organising data management across DACs and a federated OceanGliders ERDDAP.

It describes **readiness, not compliance**: the `network`/`site` requirement was only agreed in October 2026.

- **Dashboard (snapshot 2026-10-27/28):** `docs/index.html`, published with GitHub Pages; the same page in `dashboard/index.html` runs as a claude.ai Dashboard artifact.
- **Data snapshot:** `data/glider_deployments.csv` (one row per deployment per data centre, 4,370 rows) and `data/spot_checks.json` (checks made by opening files).
- **Prompt that reproduces it:** [PROMPT.md](PROMPT.md).

## Data centres

| Data centre | What is read | Script |
|---|---|---|
| IOOS Glider DAC | ERDDAP `allDatasets` + `info/<id>/index.csv` for every dataset | `harvest_erddap.py IOOS` |
| VOTO | ERDDAP `nrt_`, `delayed_`, `OG_nrt_`, `OG_complete_` datasets | `harvest_erddap.py VOTO` |
| BODC | ERDDAP glider datasets | `harvest_erddap.py BODC` |
| BODC file collections | `Public_OG1_Data_001` (real time), `Public_OG1_Data_001_Recovery` (recovery): one deployment JSON per folder | `harvest_bodc_files.py` |
| OTN | ERDDAP Trajectory/TrajectoryProfile datasets | `harvest_erddap.py OTN` |
| Coriolis (OceanGliders GDAC) | `glider_traj_index.txt`, deployment JSON, NetCDF headers by byte range | `harvest_coriolis.py` |
| IMOS ANFOG | S3 listing of `IMOS/ANFOG/`, one NetCDF header per deployment by byte range | `harvest_imos.py` |

## Run

```bash
cd harvest
W=../work && mkdir -p $W
for s in IOOS VOTO BODC OTN; do python3 harvest_erddap.py $s --work $W & done; wait
python3 harvest_bodc_files.py --work $W
python3 -W ignore harvest_imos.py --work $W
python3 -W ignore harvest_coriolis.py --work $W
python3 build.py --work $W
cp $W/glider_deployments.csv ../data/ && cd .. && python3 make_standalone.py
```

Needs Python 3.10+ with `netCDF4`, `h5py`, `numpy` and `cftime`. Harvesters are resumable (they skip what is already in their `.jsonl`). Runtime (October 2026): ERDDAPs 5 to 15 min each in parallel, IMOS about 45 min, Coriolis about 20 min with 4 processes (keep it gentle; more parallel requests slowed the server to timeouts).

## How fields are derived (`harvest/build.py`)

| Column | Rule |
|---|---|
| `tagged`, `mission`, `region`, `network` | `network` starts with "OceanGliders" |
| `site` | `site` or `oceanGliders_site` present |
| `format`, `og1_available` | OG1.0, EGO, IOOS DAC, IMOS, Other from Conventions, format_version, data_type |
| `nrt`, `recovered` | recovered: IOOS `-delayed`, VOTO `delayed_`/`OG_complete_`, OTN `_delayed`, BODC recovery collection, Coriolis data mode P (full resolution, checked), IMOS mission folders; the rest real time |
| `nrt_qc`, `rec_qc`, `dm_any` | highest QC level stated per stream: Delayed mode, Automatic QC, No QC, Not stated (Not read for unopened BODC files); "no QC" wording checked before "delayed mode" |
| `data_mode` | `data_mode` attribute, else naming (recovered = P) |
| `qc_present`, `qc_cf`, `qc_scheme` | flag variables; CF = `flag_values` and equal-length `flag_meanings`; scheme by majority of flag variables |
| `platform` | Slocum, Seaglider, SeaExplorer, Spray, Unknown; wave gliders dropped |
| `glider_days` | end minus start; blank if unknown, `bad` if under 0 or over 400 days |
| `institution` | normalised (aliases in `build.py`); raw in `institution_raw` |
| `group`, `also_in` | same deployment on more than one data centre: same WMO ID, start dates within 3 days |

## Assumptions and limitations

- **A snapshot of six data centres.** Read on 2026-10-27/28 from IOOS, VOTO, BODC, OTN, Coriolis and IMOS. Glider data held only elsewhere (other national or institutional servers, EMODnet) are not counted, so totals describe these six, not the global fleet.
- **Metadata, not data.** Everything comes from what the files and catalogues state (global attributes, flag attributes, names, indexes). Work that was done but not recorded is invisible: a centre may apply delayed-mode QC that its files don't mention, and the dashboard will show "automatic" or "not stated".
- **QC level is read from free text.** Most data centres describe QC in free-text `processing_level`, classified by keywords (rules under Definitions). Unusual wording can be misclassified; QC flag values themselves were checked in only one file.
- **Real time vs recovered follows each centre's naming.** Dataset names, folders and data-mode letters were taken at face value. Coriolis data mode P was checked as full resolution on two Slocum deployments (Ifremer, SOCIB) and assumed for all P files; IMOS mission (FV01) folders are taken as recovered from their "Delayed Mode" label.
- **What counts as a deployment.** Records are grouped per data centre by its own key (IOOS dataset without `-delayed`, VOTO glider and mission, BODC and OTN deployment number, Coriolis and IMOS folder). A deployment split across several keys, or one key holding several deployments, is miscounted.
- **Unique deployments are an upper bound.** Copies on different centres are matched only on WMO ID and start date within 3 days. IMOS records no WMO IDs and OTN few, and wrong start dates break matches, so some duplicates remain.
- **Tagged means the attribute is there.** A record counts as tagged if `network` starts with "OceanGliders"; values outside the BOON vocabulary still count. Format is read from Conventions and format_version strings; files were not validated against the OG1.0 specification.
- **CF check is shallow.** "CF-described" means every flag variable has `flag_values` and a `flag_meanings` list of the same length; meanings were not checked against a vocabulary, nor flags against the data.
- **Glider-days come from metadata time ranges.** They can include pre- or post-deployment test data; spans under 0 or over 400 days are left out, and deployments without an end time (BODC folders without ERDDAP datasets, some Coriolis files) have none. The total sums unique deployments, taking the longest span in each match group.
- **BODC file collections were not opened.** BODC's recovery and older real-time OG1 files can't be read by byte range, so their tags and QC are unknown ("Not read") unless an ERDDAP dataset exists for the same deployment; they are assumed to be OG1 from the collection name.
- **Platform and institution are text matching.** Glider type is matched in attributes and titles (IOOS has no glider-model attribute, so many IOOS records are "Unknown"); wave gliders were dropped the same way. Institution names were merged by a hand-made alias list (raw names kept in the data); groupings such as "Multi-institution partnership" and separate OOI arrays are judgment calls.
- **"Active since Oct 2024"** uses the end date, or the start date where no end date is known.
- **Built with an AI assistant.** Harvest, classification and page were produced by Claude for Filipa Carvalho and spot-checked against raw metadata and files (table below); not independently reviewed. The code is public at github.com/FiIipaCarvalho/og-dac-metadata so the analysis can be re-run and checked.

## Gotchas found while building it

- Some ERDDAPs publish a global attribute `id` that is not the datasetID; key on the datasetID.
- netCDF4 is not thread-safe: read files in separate processes.
- Check a remote file's signature before choosing a reader: netCDF4's byte-range open of an HDF5 file can sit through long timeouts before failing.
- EGO files have no time-coverage attributes and often leave `TIME` as fill values while `JULD` is filled; the Coriolis index `time_coverage_start` holds 1970/2000 placeholders.
- BODC's recovery files can't be read by byte range (HTTP 416).
- "Delayed" in IOOS, VOTO and OTN dataset names means recovered data, not delayed-mode QC; IOOS `processing_level` often says "Delayed mode dataset" next to "No quality control provided".
- Processing software is not recorded in any standard attribute.

Built with Claude (Anthropic) for Filipa Carvalho (National Oceanography Centre, OceanGliders co-chair); spot-checked, not independently reviewed.
