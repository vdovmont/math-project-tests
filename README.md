## MathCore JSON tester GUI

Start the local browser interface from the directory containing `json-data` and
`tests` (currently `scripts`):

```sh
cd scripts
python json_http_tester_gui.py
```

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

The **JSON files** tab groups files by the shared name before `-input.json`,
`-output.json`, and `-test.json`. Input and expected output appear first in each
group; test-run files follow in descending test-folder name order.
The same tab also has a separate **Results** group containing every `result.txt`,
ordered by its test-run folder name descending.

Each test-group header has an **Enabled/Disabled** checkbox. Disabled groups stay
visible in the file browser with muted styling but are skipped when a test run
starts. New groups are enabled by default, and at least one group containing an
input file must be enabled to start a run. The selections persist in
`.json_http_tester_gui_test_selection.json` in the launch directory.

All groups start collapsed. Use **Delete test type** to enter test-type selection
mode, then select group headers to remove their input, expected-output, and
matching test files. Use **Delete test results** to collapse the other groups,
open **Results**, and select complete timestamped run folders. Click the active
delete button again to review the selection. Both actions show an exact path
preview and require confirmation; deletion is disabled while a test run is active.

Use the green **Add test type** button to select multiple files in one operation.
The GUI automatically groups matching `-input.json` and `-output.json` files by
their shared base name and adds every valid pair independently. A results dialog
lists added test types and reports unsupported filenames, missing or duplicate
pair members, invalid JSON, oversized files, and existing-file conflicts.
Uploading is disabled during a run.

The page has three tabs in this order: **Runner**, **JSON files**, and **Map**.
The Runner has separate allowed-error percentages for calculation time, total
distance, total value, and jobs. Each defaults to zero. Most metrics pass
when its test value is no greater than the expected value plus that percentage;
for example, an expected value of 100 with a 10% allowance passes through 110.
The Base URL and percentage fields are saved only when a test run starts. Previous
options and named profiles share `.json_http_tester_gui_profiles.json` in the
launch directory. Previous is stored in a dedicated top-level field, separate
from user profile names, so a named profile called `previous` is also allowed.
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

To host the GUI at another address, edit `GUI_URL` at the top of
`json_http_tester_gui.py` or pass `--url`:

```sh
python json_http_tester_gui.py --url http://127.0.0.1:9001
```

The console tester still works without arguments and uses the configuration
constants at the top of its file. It also accepts optional overrides:

```sh
python scripts/json_http_tester.py --base-url http://localhost:9000 \
  --json-data-folder ./json-data --tests-folder ./tests \
  --timeout 30 --poll-interval 1 \
  --calculation-time-tolerance-percent 0 \
  --total-distance-tolerance-percent 0 \
  --total-value-tolerance-percent 0 \
  --jobs-tolerance-percent 0
```
