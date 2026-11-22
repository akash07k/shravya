# shravya
Next generation TTS engine

## Development

Install Python 3.13 and [uv](https://docs.astral.sh/uv/), then run:

```powershell
uv sync --locked
```

For type checking, provide an NVDA source checkout at `..\nvda` or `nvda` in
this repository. The checkout is used for imports, not included as add-on code.

On Windows, synchronize the locked tools and build the add-on with:

```bat
build-production-addon.bat
```

The helper works from any directory. Alternatively, after synchronizing tools:

```powershell
uv run --no-sync scons
```

The development environment includes NVDA-matched `comtypes` and `pycaw`
for diagnostic development and type checking; the add-on uses NVDA's bundled
copies at runtime.

The package is written to `dist\shravya-<version>.nvda-addon`. The build also
generates English help from this README. The model and runtime are distributed
separately to keep the add-on download size small.

Selecting Shravya before resources are installed will fail. Resource setup is
available from NVDA's Tools menu (press `R` within the menu) and the Shravya
category in NVDA Settings. Choose download from the configured catalog
or select a local runtime ZIP and model ZIP; local installation does not
require network access or a catalog. You can also use this menu to replace
existing resources with local ZIPs. Cancelling or providing an invalid ZIP
leaves the previous selection unchanged, and the ZIPs are never deleted.
Installed packs are stored in the active NVDA configuration directory under
`shravya`, outside the replaceable add-on directory.

Shravya checks for add-on updates on startup and every 24 hours while NVDA is
running, shows release notes in a read-only text field, asks before downloading
the add-on, and uses NVDA's installation confirmation. The check downloads
`shravya-update.toml` from the release; missing, invalid, or mismatched metadata
is reported as a failed check. The metadata must include a target
`resource_catalog` HTTP(S) URL. Empty or absent notes are identified in the
prompt. Required resources are prepared with separate consent before NVDA
installs an add-on update.
Interrupted downloads can resume when the server supports a strong validator;
otherwise they restart. Partial add-on downloads are kept in the active NVDA
configuration for a later retry.
Compatible resource packs remain outside the replaced add-on. Resource-update
checks can offer a changed runtime, model, or both. A model-only update takes
effect on the next model reload. A changed runtime and its accompanying model
remain pending until NVDA restarts; a failed promotion retains the previous
selection. Finish a pending resource or add-on update before preparing another.

### Building separate resource packs

The builders create separate runtime and model ZIPs. They require Windows x64,
Python 3.13, uv, Rust with the MSVC target, Visual Studio C++ Build Tools, and
network access. From the repository root, start the interactive builder:

```powershell
.\build_resources.bat
```

The script synchronizes the locked Python tools and presents numbered choices
for the packs and sources. Choose `latest` to download BlueTTS, polyg2p, and
the model automatically, or `local` to supply source folders and descriptions.
The runtime build compiles the Windows phonemizer. Choose local delivery to
produce ZIPs without a catalog or GitHub details; GitHub delivery also asks
for the repository, release tag, and compatible add-on versions and generates
a catalog. The script does not upload files or install system build
prerequisites. Python dependencies still require network access.

For a `latest` build, the output directory also contains a uniquely named
`sources-...` folder with the downloaded `bluetts`, `polyg2p`, and `model`
source folders needed for the selected packs. The script prints its location.
Keep these folders to use them as inputs for a later `local` build; the local
option otherwise expects folders you provide yourself. A model-only build
saves BlueTTS only if quantization needs it. Saved sources may require several
gigabytes of disk space. Source revisions are recorded in each ZIP's
`build-inputs.json`; use these to describe the folders when building locally.
Local source folders are used in place and are not copied.

To build both packs for local use without prompts:

```powershell
.\build_resources.bat --kind both --output tmp --source latest --delivery local
```

For local source folders, select `--source local` and supply the needed
directories and descriptions. For a previous latest build, these directories
are the corresponding folders inside its printed `sources-...` directory. For example:

```powershell
.\build_resources.bat --kind both --output tmp --source local --delivery local --bluetts-dir C:\sources\BlueTTS --bluetts-description "BlueTTS checkout" --polyg2p-dir C:\sources\polyg2p --polyg2p-description "polyg2p checkout" --model-dir C:\sources\model --model-description "model bundle"
```

For GitHub distribution, use for example:

```powershell
.\build_resources.bat --kind both --output tmp --source latest --delivery github --repository akash07k/shravya --tag resources --addons 0.1.0
```

List only add-on versions compatible with both packs. For an independent
GitHub update, choose `runtime` or `model` and provide the existing catalog
when prompted or with `--existing-catalog <path>`; the unchanged pack's ID
and URL are reused. The tag is `resources` or a three-part version, optionally
prefixed by `v`. Run `.\build_resources.bat --help` for local source arguments.

The configured BlueTTS repository and optional branch are in the `[sources]`
section of `addon\synthDrivers\shravya\config.toml`. Latest builds resolve
concrete upstream revisions and record them in the ZIPs; local builds record
the supplied source descriptions. A model bundle with an int8 estimator does
not require a local BlueTTS source when building only the model.

The underlying builders also remain available directly. Each command
requires a new output file:

```powershell
New-Item -ItemType Directory -Force tmp | Out-Null
uv run --no-sync python -B -m tools.build_resource_runtime tmp\runtime.zip --source-choice latest
uv run --no-sync python -B -m tools.build_resource_model tmp\model.zip --source-choice latest
```

The model builder also accepts a compatible local bundle containing `onnx`,
`frontend\polyg2p_data`, and `voices` directories:

```powershell
uv run --no-sync python -B -m tools.build_resource_model tmp\custom-model.zip --source-choice local --bundle C:\path\to\bundle --source "model source" --bluetts-dir C:\path\to\BlueTTS --bluetts-description "BlueTTS source"
```

Keep the custom source bundle and identify its origin and revision in
`--source`; that description is included in the ZIP. Pack identifiers are
generated automatically. The ZIPs record source revisions and dependency
inputs. If the model does not provide an int8 vector estimator, the builder
generates it using the selected BlueTTS quantizer in an isolated environment.
These builders do not publish files.

### Delivery configuration and GitHub releases

The add-on and release workflow use
`addon\synthDrivers\shravya\config.toml` as the delivery configuration. Its
defaults point to the GitHub catalog and latest-release API. The catalog URL
will not work until the catalog and repository are publicly accessible.
Leave a URL empty to disable its corresponding downloads or checks. The add-on
includes this file when built. For example:

```toml
[delivery]
catalog_url = "https://raw.githubusercontent.com/akash07k/shravya/main/catalogs/current.toml"
release_api = "https://api.github.com/repos/akash07k/shravya/releases/latest"
```

The catalog remains a separate public TOML file because it describes the
published packs and their compatible add-on versions. See
`resource-catalog.example.toml` for its shape.

To publish resources using GitHub Actions, make the repository publicly
accessible, then manually run **build and publish resources** from `main`.
Choose both packs or one pack and explicitly list the compatible add-on
versions. A one-pack build requires an existing `catalogs\current.toml` and
reuses its unchanged pack. The workflow builds from latest sources, runs CPU
synthesis with the selected pair, uploads uniquely named ZIPs to the published
`resources` prerelease, checks that both URLs are public, and commits the
catalog to `main`. It stops on build, smoke-test, upload, permissions, branch
protection, or branch-conflict errors; uploaded assets may remain after a
failure. The `resources` tag does not trigger an add-on release.

For manual publication, build with GitHub delivery and the `resources` tag.
Run `uv run --no-sync python -B -m tools.smoke_resource_pair <runtime.zip>
<model.zip>` with the new and unchanged ZIPs as appropriate. Upload new ZIPs
to a published prerelease without changing their filenames, verify both URLs
are public, and only then publish the generated catalog at its configured
URL. Do not point a catalog at assets in a draft release. Preserve older assets;
list only versions known to be compatible with the selected pair.

To prepare an add-on release, update the version and changelog, ensure the
public catalog includes that exact version, and create its matching tag.
GitHub Actions checks catalog compatibility and public ZIP availability before
creating the draft add-on release. Review and publish the draft yourself.
The add-on does not authenticate to GitHub.

The catalog binds compatible add-on versions and a runtime/model pair to the
`bluev3-v1` interface. Setup checks the catalog and each pack's inventory before
selecting the pair. A failed or cancelled setup leaves the previous selection
unchanged. The catalog is not a signature or integrity guarantee.

The Shravya category in NVDA Settings selects BlueV3 native or Sonic rate,
CPU or NVIDIA CUDA, the inference step count, and a fixed random seed. The
defaults are native rate, CUDA, four steps, and seed 0. The seed makes repeated
speech more consistent; text segmentation and computation differences can
still affect the result. Rate, step, and seed changes apply to new speech;
changing the compute device interrupts speech and reloads the model. NVDA's
pitch setting and capital-pitch changes use Sonic in either rate mode. Detected
CUDA initialization or synthesis failures switch temporarily to CPU without
replaying interrupted speech; native crashes and unrecognized failures may not
be recoverable.

The latency diagnostic is available with Shravya. With Shravya selected,
press `NVDA+shift+f11` to start capture and wait for the
ready message. Press `NVDA+shift+f12` for the controlled phrase, or navigate
and type for live observations. Press `NVDA+shift+f11` again to stop capture
and open a browsable, copyable report. Both gestures can be changed in NVDA's
Input Gestures dialog under Shravya. Capture is unavailable on secure screens;
entering one discards an active capture and reports this after returning.

The report measures the captured keyboard event to a candidate Windows output
onset, not physical speaker or headphone delay. Other applications can affect
the mixed output, so the timings do not prove which sound was caused by a key.
The detector uses a fixed output level; quiet speech may be detected late or
missed. Overlapping, interrupted, or invalid attempts are excluded. Diagnostic
data is kept in memory only; no audio recording is saved.

### Build and release versions

`addonVersion.py` supplies the release version to both the add-on and Python
project metadata. Each successful default package build increments the patch
component in the ignored `.build-version.toml` file.

The counter uses TOML, for example:

```toml
version = "0.1.2"
```

Explicit `version=`, `dev=1`, clean, dry-run, and translation commands do not
advance that counter. Use `uv run --no-sync scons version=0.1.0` to package the
tracked release explicitly. A clean command removes the current default package
without incrementing its version. If a release-version increase makes the local
counter older than the release, remove `.build-version.toml` before the next
default build. Invalid or stale counter state is reported as an error.

The counter belongs to one checkout; it does not coordinate versions across
machines or fresh CI runs. Release tags must be three-part numeric versions,
optionally prefixed with `v`, matching `addonVersion.py`. Tagged CI builds use
that explicit version rather than incrementing the local counter.

Run the unit tests with:

```powershell
uv run --no-sync python -B -m unittest discover -s tests
```

The GitHub Actions workflow builds pull requests and tags, or can be started
manually. Tag builds create a draft GitHub release using `changelog.md`. They
generate `shravya-update.toml` with the configured catalog URL and attach it
alongside the add-on. Set `catalog_url` in the delivery configuration and
prepare the changelog before tagging; a missing or invalid catalog URL fails
the release build.
GNU gettext is required for `uv run scons pot` and for compiling translations;
the build workflow installs it automatically.

## Component licenses

The standard add-on and separate resource packs use the following licenses:

| Component | License |
|---|---|
| Shravya | [GNU AGPL 3.0](https://github.com/akash07k/shravya/blob/main/LICENSE) |
| NVDA AddonTemplate build scaffold | [GNU GPL](https://github.com/akash07k/shravya/blob/main/COPYING.txt) |
| BlueTTS and BlueV3 model | MIT, with the model exceptions listed below |
| BlueV3 vocoder | BigScience OpenRAIL-M |
| BlueV3 preset voices | CC BY 4.0, derived from LibriTTS-R |
| English pronunciation data | CMU Pronouncing Dictionary license (BSD-style) |
| polyg2p and flatbuffers | Apache 2.0 |
| ONNX Runtime and docopt | MIT |
| num2words | GNU LGPL 2.1 or later |
| NumPy | BSD-3-Clause, 0BSD, MIT, Zlib, and CC0-1.0 |
| packaging | Apache 2.0 or BSD-2-Clause |
| Protocol Buffers | BSD-3-Clause |
| NVIDIA cuBLAS, CUDA NVRTC, and cuDNN | NVIDIA proprietary licenses |

The [BlueV3 model inventory](https://huggingface.co/notmax123/BlueV3-onnx/blob/d0a0270e60b96d5066cfe7ebed1f7c2872361c43/LICENSES.md)
provides model and voice attribution. The model pack also includes this
inventory and the OpenRAIL-M license; runtime package metadata and notices
provide additional details. Custom model bundles and manually staged resources
may use different licenses.
