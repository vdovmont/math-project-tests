## MathCore JSON tester GUI

Start the local browser interface from the script directory:

```sh
python start.py
```

Code responsibilities:

- `config.py`: shared Python options, grouped by their original source module.
- `start.py`: application startup, storage preparation, HTTP transport, and routing.
- `gui_tester.py`: Tester run controls, profiles, file management, and map data.
- `gui_optimizer.py`: Optimizer profiles and API actions.
- `json_http_tester.py`: MathCore requests, test execution, and comparison reports.
- `json_http_optimizer.py`: Optimizer options and future processing logic.
- `gui.html`: the shared browser interface for both tools.

The processing modules do not start a GUI server. Use `start.py` as the entry point.

Edit defaults, paths, timeouts, and limits in `config.py`, then restart the
application. Saved GUI profiles and explicit CLI options still override their
corresponding defaults. Tester and Optimizer have independent Base URL defaults.

Storage is relative to the scripts, independent of the launch directory:
`Data/tests/references/initial` contains inputs and expected outputs,
`Data/tests/test-results` contains run results, `Data/optimizer` is reserved for
future use, and `Settings` contains GUI profiles and test selections.
The launcher automatically creates these directories on startup and adds the
`tester_` prefix to existing Tester settings filenames without overwriting
conflicts.
Explicit CLI folder overrides still work and remain relative to the launch
directory when supplied as relative paths.

By default, the server binds to `http://0.0.0.0:9010/`. It prints this address
but does not open a browser automatically. The run form allows the MathCore base
URL to be changed. The configured `JSON_DATA_FOLDER` and `TESTS_FOLDER` are
available through a grouped browser whose file-content viewer is read-only.
Tester output is shown live in the page and is also mirrored in the GUI server's
console. Displayed filesystem paths are relative to the directory from which the
GUI was launched. Routine browser API access lines are hidden from that console.

While a test is running, the green **Start test run** button becomes a red
**Stop test run** button. Stopping calls `STOP_REQUEST` for the active query,
terminates the local runner, and removes only the newly created test-run folder.
The run button is directly beside the Live progress heading.

Base URLs can be saved under a separate name using the selector and Save/Delete
buttons below the URL field. They are stored independently in
`Settings/tester_urls.json`. The selector starts blank and returns
to blank whenever the URL is edited or changed by an options profile. A saved
name appears after selecting it directly from the URL list or immediately after
saving the current URL under that name. Selecting the blank entry restores the
default Tester Base URL.
Tester Profiles also remember the selected saved URL, Reference profile, and
Allowed error preset. Previous stores the same selections when a run starts, so
they are restored on the next application launch. If a referenced item was
deleted, its selector falls back to blank or, for references, Initial.

The **Reference profile** selector controls which expected outputs a test uses.
**Initial** is always present, appears last, and cannot be deleted. **New** opens
a dialog for choosing a saved Base URL; when no URLs exist, the dialog explains
that one must be saved first. A new profile starts under the URL name and changes
the **Generate** button to its enabled yellow state while disabling **Start test
run**. Generation sends every enabled input to that saved URL and initially
estimates progress from Initial outputs.

After the first output arrives, the reference folder and profile are renamed to
`URL name-Version-YYYY.MM.DD-HH.MM.SS`. Outputs are stored in that folder under
`Data/tests/references`; completed profiles are ordered newest first. Later test
runs use the selected profile's output values and calculation times. Reference
profiles persist separately in
`Settings/tester_reference_profiles.json`. Deleting a non-Initial
profile also deletes its complete reference folder.

Each saved reference profile records a `different_output_versions` boolean.
After reference generation, output versions are compared with the first version
found until a mismatch is detected or every output has been checked. A mismatch
sets the flag permanently and displays a warning recommending a new profile for
the current test-group selection. Starting a test with a flagged profile requires
acknowledging that warning or aborting the test before it starts.

If a group was disabled during reference generation and is enabled later, the
selected Reference profile reports the missing output, enables **Generate**, and
disables **Start test run**. That run sends only inputs whose enabled groups are
missing outputs; its expected time and progress also cover only those groups.

Each test report identifies the comparison as `Initial (version)`, `vs`, and
`saved URL name (version)`. A generated Reference profile already contains its
version in its name, so no second version is appended to that label. When no
saved URL is selected, the full Base URL is shown. Versions come from the first
available output JSONs. The same comparison appears above every group snippet.

The **JSON files** tab groups files by the shared name before `-input.json`,
`-output.json`, and `-test.json`. Expanding a group shows its **Input** file,
an expandable **References** section containing the **Initial** reference, and an
expandable **Test results** section containing that group's snippet from each
run's `result.txt`. Each snippet is named after its test-results folder. The fourth
**Test results jsons** section contains the complete historical test JSONs in
descending test-folder order.
The separate **Results** tab lists every `result.txt` directly, ordered by its
test-run folder name descending, with a read-only viewer on the right.

Each test-group header has an **Enabled/Disabled** checkbox. Disabled groups stay
visible in the file browser with muted styling but are skipped when a test run
starts. New groups are enabled by default, and at least one group containing an
input file must be enabled to start a run. The selections persist in
`tester_test_selection.json` in `Settings`.

All JSON-file groups start collapsed. Use **Delete test type** to enter test-type
selection mode, then select group headers to remove their input, expected-output,
and matching test files. On the **Results** tab, use **Delete test results** and
select complete timestamped run folders. Click the active delete button again to
review the selection. Both actions show an exact path preview and require
confirmation; deletion is disabled while a test run is active.

Use the green **Add test type** button to select multiple files in one operation.
The GUI automatically groups matching `-input.json` and `-output.json` files by
their shared base name and adds every valid pair independently. A results dialog
lists added test types and reports unsupported filenames, missing or duplicate
pair members, invalid JSON, oversized files, and existing-file conflicts.
Uploading is disabled during a run.

The page has two general tabs: **Tester** and **Optimizer**. Tester contains
**Runner**, **JSON files**, **Results**, and **Map**, with its existing testing behavior.
Optimizer contains **Runner** and **JSON files** and remembers its selected
sub-tab while switching tools. Its Runner has independent saved profiles,
Base URL, and Distance (%) from 0 through 100, including decimals (default 0).
Optimizer profiles are stored in `Settings/.json_http_optimizer_gui_profiles.json`.
Its console is separate from Tester output. Processing is not implemented yet,
so Start and Previous remain disabled. Add files, Remove files, and Refresh are
also disabled for now; future uploads will accept only `*-input.json` files.
The Runner has separate allowed-error percentages for calculation time, total
distance, total value, total time, and jobs. Total time is the sum of every
`totalDuration` value in an output JSON. Each tolerance defaults to zero. Most metrics pass
when its test value is no greater than the expected value plus that percentage;
for example, an expected value of 100 with a 10% allowance passes through 110.
The Base URL and percentage fields are saved only when a test run starts. Previous
options and named profiles share `tester_profiles.json` in the
`Settings` directory. Previous is stored in a dedicated top-level field, separate
from user profile names, so a named profile called `previous` is also allowed.
Allowed error values can also be saved independently with the selector below the
percentage fields. These presets are stored in
`Settings/tester_allowed_error_profiles.json`.
The GUI loads Previous on its next start; if none exists, it loads the hard-coded
defaults. The **Default** and **Previous** buttons switch profiles and are disabled
when the form already matches that profile. Unsaved edits enable both available
profile buttons. When a run starts from a named profile, Previous also remembers
that profile's identity and restores its dropdown selection along with the values.
If the named profile is deleted, Previous retains the values without a named
selection.
The profile dropdown always starts with a blank selection;
actively choosing that blank entry loads the hard-coded defaults, while initial
GUI startup still loads Previous when available. Choosing a saved name loads its
options. **Save** opens a naming dialog for the
current fields. **Delete** first asks which saved profile to remove and then shows
a second confirmation containing that profile's name. Profile names are unique
without regard to letter case.
The comparison report shows the expected value, tolerance limit, raw difference,
percentage difference from the expected value, and a status for every metric.
Percentage differences normally use one decimal place and use three when their
absolute value is below 0.1%; exact zero differences show `0`. A percentage is
shown as `N/A` when a nonzero test value is compared with an expected zero.
Values at or below the expected value show a `✓`;
values above expected but within the allowance show
`PASSED (WITH TOLERANCE)`; values above the allowance show `FAILED`.
The Jobs metric is read from the result comment's `taken/total jobs taken` text
and displays that fraction. More taken jobs is better. Its tolerance threshold is
`ceil(expected taken / (1 + tolerance%))`, and its percentage difference divides
the taken-count difference by the expected taken count (the first number).
When a run creates `result.txt`, the Live progress header displays the overall
`PASSED` or `FAILED` value read from that file. **Open result** then becomes
available and opens the report directly in the read-only JSON files viewer.
The live console also shows weighted overall progress below the elapsed time.
Each enabled group is weighted by the calculation time in its expected-output
file. A leading `Processing N%` state advances progress within the active group;
states beginning with `Task in queue` leave progress unchanged until processing
resumes.

The separate **Map** tab groups files by their shared name before the
`-input.json`, `-output.json`, and `-test.json` suffixes. Expected and historical
test routes can be toggled independently inside each group. Delivery `pointId`
values are mapped to the `lat` and `lon` coordinates in the matching input file.
Map groups start collapsed; refreshing routes clears every route selection and
collapses all groups again.
Each expected/test source has a parent checkbox that toggles all of its routes;
the indented route checkboxes can still be changed individually. Its separate
**Collapse/Expand** button hides or reveals that source's route list without
changing any route selections or map layers. Each test-group header also has a
**Collapse all/Expand all** button that applies the same action to every source
inside that group.
Expected routes use a solid blue line; test routes use distinct dashed colors.
Stops are numbered continuously across every route in an individual result file,
so later routes continue from the preceding route's final stop number.
The interactive map uses Leaflet and OpenStreetMap tiles, so its background map
requires internet access.

To host the GUI at another address, edit `GUI_URL` in
`config.py` or pass `--url`:

```sh
python start.py --url http://127.0.0.1:9001
```

Run the console tester with `python start.py --tester`. It uses the configuration
defaults in the `json_http_tester.py` section of `config.py` and accepts optional overrides:

```sh
python start.py --tester --base-url http://localhost:9000 \
  --json-data-folder ./Data/tests/references/initial --tests-folder ./Data/tests/test-results \
  --timeout 30 --poll-interval 1 \
  --calculation-time-tolerance-percent 0 \
  --total-distance-tolerance-percent 0 \
  --total-value-tolerance-percent 0 \
  --jobs-tolerance-percent 0
```
