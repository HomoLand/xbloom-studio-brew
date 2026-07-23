# Private coffee and tea catalog

Use the catalog when the user wants to inspect, compare, or export recipes visible in their own
xBloom app context. Catalog work is independent of BLE: it never scans, connects, changes machine
mode, writes A/B/C, starts the grinder, or dispenses water.

## Scope and evidence

APK 2.2.2 does not contain one static, global recipe database. It obtains Studio/J15 records from
regional services and caches account/device-visible data locally. Therefore “all recipes” means
all records present in an authorized export or returned to the user's own account and region at
that moment. It does not mean every xBloom, roaster, private, deleted, experimental, or other-user
recipe worldwide.

The importer, request envelope, ephemeral login, and normalized endpoint mapping have **D/T**
evidence: they are derived from APK models and have deterministic tests. A read-only compatibility
check against the owner's China-region account succeeded on 2026-07-14 for official coffee, official
tea, combined Studio-created recipes, Product/xPod recipes, and an empty Shared list. That is
**live-service verified**, not **H**: hardware evidence labels apply only to Studio BLE behavior.
Cloud contracts can still change, so authorized JSON/cache import remains a useful offline fallback.

On the same date, two exact owner-approved local recipes (one hot and one flash-brew extraction)
were added to the account, read back through the created endpoint, and then replayed through
`catalog push` as `already-present` with `write_performed=false`. This also exposed the APK's
stage-name sort rule now enforced by the form mapper. These are bounded live-service observations;
release tests still mock every write and never mutate an account.

The normalized catalog is private state, not a redistributable recipe pack. Every entry defaults
to `redistribution: unknown`. Do not commit, publish, or bulk-share catalog contents without
permission from the recipe owner.

## Storage and self-check

The default path is:

```text
~/.xbloom-studio-brew/catalog/catalog.json
```

Override it with `XBLOOM_CATALOG_PATH` or the command-level `--catalog-file`. The writer uses an
atomic replacement and attempts owner-only permissions. It stores normalized recipe fields and
safe provenance only; it does not retain raw responses, account tokens, member IDs, serial
numbers, or request forms.

Check availability without network or BLE:

```text
python <skill-dir>/scripts/xbloom.py doctor
python <skill-dir>/scripts/xbloom.py catalog status
```

`doctor` reports only whether account environment variables and a cloud-config path are present;
it never prints their values. No initial configuration is required for JSON/MMKV import, query,
export, validation, or A/B/C programming. Account sync and remote add are optional and independent
of BLE.

## Import an authorized export

Import an xBloom App/API JSON response or a JSON object containing decoded records:

```text
python <skill-dir>/scripts/xbloom.py catalog import-json <export.json>
```

For an MMKV cache, decode the database to JSON outside this Skill, then import the decoded JSON:

```text
python <skill-dir>/scripts/xbloom.py catalog import-mmkv <decoded-mmkv.json>
```

Raw MMKV binary is deliberately not parsed. The recursive importer recognizes response `list`
containers, recipe maps, JSON strings stored as cache values, and Easy-mode
`easyModeDetailVoList[].recipeSnapshotVo` records. Unsupported or invalid records are counted as
rejections; one bad record does not discard valid records from the same import.

## Query and export

```text
python <skill-dir>/scripts/xbloom.py catalog list
python <skill-dir>/scripts/xbloom.py catalog list --kind coffee --executable
python <skill-dir>/scripts/xbloom.py catalog list --kind tea
python <skill-dir>/scripts/xbloom.py catalog list --slot-compatible
python <skill-dir>/scripts/xbloom.py catalog show <id-or-unambiguous-name>
python <skill-dir>/scripts/xbloom.py catalog export <id> <workspace-recipe.yaml>
```

Export succeeds only when the normalized record passes the same guarded coffee or tea validator
used by the machine workflow. It never exports raw service fields as executable protocol input.

Entry classifications matter:

- Studio/J15 Omni coffee can be executable when every guarded field is present and valid.
- Studio/J15 tea is executable only through the dedicated Omni Tea Brewer workflow.
- xPod-native recipes are retained as first-party reference data, not executable Omni YAML. Adapt
  them explicitly according to `references/web-enrichment.md` and validate the result separately.
- Other/unknown dripper geometry, xBloom Original/J20, or malformed records remain reference-only.
- App records with parameters outside the guarded public schema, including fractional dose or
  grinder values, remain reference-only with the original normalized number; they are never
  truncated or silently rounded into executable YAML.

## Ephemeral own-account sync

For the simplest current-account import, provide the account only through host environment
variables. Never put a password in a command argument, recipe, Skill file, repository, or chat.
Interactive terminals may omit the password variable and use the hidden prompt instead.

```text
XBLOOM_ACCOUNT_EMAIL=<own-account-email>
XBLOOM_ACCOUNT_PASSWORD=<own-account-password>
python <skill-dir>/scripts/xbloom.py catalog login-sync --region china --language zh-cn
```

Choose the actual account tenant: `china` or `international`. The default import reads all five
account recipe categories:

- `coffee`: xBloom/roaster official Studio coffee;
- `tea`: official Omni Tea Brewer recipes;
- `created`: the combined Studio/J15 endpoint containing the user's created coffee and tea;
- `product`: Product/xPod recipes associated with the account;
- `shared`: recipes shared to the account, which may legitimately be empty.

Repeat `--include` to request only selected categories. The login session, token, member ID, and raw
responses remain in process memory and are discarded after the bounded reads. Only normalized
recipe records and safe provenance are written to the private catalog. Account sync never scans or
connects to BLE.

The 2026-07-14 China-tenant compatibility snapshot returned 9 official coffee, 6 official tea,
2 combined created records, 6 Product/xPod records, and 0 shared records. Counts are evidence of the
endpoint mapping, not a promise that another account or a later date will return the same catalog.

## Advanced read-only request-form sync

Keep the account form outside the repository and Skill directory. Set its path with
`XBLOOM_CLOUD_CONFIG` or pass `--config` explicitly. The JSON object has this shape:

```json
{
  "region": "international",
  "adapted_model": 1,
  "base_form": {
    "skey": "COPY_FROM_YOUR_OWN_APP_SESSION",
    "phoneType": "Android",
    "appVersion": "COPY_FROM_THE_APP",
    "clientDetail": "COPY_FROM_THE_APP",
    "clientSecretStr": "COPY_FROM_YOUR_OWN_APP_SESSION",
    "interfaceVersion": 19700101,
    "token": "COPY_FROM_YOUR_OWN_APP_SESSION",
    "memberId": 123,
    "clientType": 0,
    "languageType": 0,
    "pageNumber": 1,
    "countPerPage": 0
  },
  "easy_mode": {
    "sn": "COPY_YOUR_STUDIO_SERIAL",
    "country_id": 123,
    "table_id": 0
  }
}
```

The numeric values above are structural examples, not universal account values. Copy the exact
values and types from the user's own authorized app request; do not invent them, scrape another
account, ask the Agent to reveal them in chat, or commit the file. `region` accepts
`international` or `china`; `adapted_model` must remain `1` for Studio/J15.

Sync the account/region-visible recipe categories:

```text
python <skill-dir>/scripts/xbloom.py catalog sync --config <private-config.json>
```

The default targets are `coffee`, `tea`, `created`, `product`, and `shared`. Request current or
default Easy-mode snapshots only when the `easy_mode` fields are known:

```text
python <skill-dir>/scripts/xbloom.py catalog sync --config <private-config.json> \
  --include easy --include easy-default
```

The implementation reproduces the app's regional URL selection and chunked RSA/PKCS#1-v1.5 JSON
envelope. It performs bounded reads, no automatic retry, no credential logging, and persists only
normalized entries. A service error must be reported as a sync failure; never fall back to an
unapproved endpoint or account.

## Preview and add a local recipe to the account

`catalog push` maps a guarded local coffee or tea YAML/JSON file to the current Android app form.
Preview is offline and is always the default:

```text
python <skill-dir>/scripts/xbloom.py catalog push <recipe.yaml> --region china
```

Review the reported form, lossy-boundary warnings, and fingerprint. Only after the owner explicitly
approves that exact account write may an Agent run:

```text
XBLOOM_ACCOUNT_EMAIL=<own-account-email>
XBLOOM_ACCOUNT_PASSWORD=<own-account-password>
python <skill-dir>/scripts/xbloom.py catalog push <recipe.yaml> --region china \
  --apply --confirm-write own-account-cloud-recipe
```

The write path is deliberately **add-only**. It first reads the user's combined created list:
an identical name and parameter fingerprint returns `already-present` without writing, while the
same name with different parameters is refused rather than overwritten. Share, pin, overwrite,
and profile mutation are not exposed. Release tests mock the add endpoint and never mutate a
live account; every real addition remains an explicitly approved owner action.

Cloud form conversion is stricter than local BLE execution. The Android created-recipe schema has
one global grinder RPM, so a local coffee file with multiple non-zero RPM values is rejected rather
than flattened. The APK reads persisted pours ordered by their stage name, so upload replaces local
display labels with the App's sortable `Bloom`, `Pour 2`, `Pour 3`, ... names; JSON array position
alone is not sufficient to preserve execution order. Tea uses its corresponding canonical labels.
For flash brew, the cloud record stores only the same coffee pour-over program used for hot service:
ice mass, final water, time, and note remain local/manual preparation, so keep the ice requirement
visible in the cloud recipe name. A downloaded concentrated recipe whose name mentions ice is not
necessarily stored incorrectly; it is incomplete for guarded local execution until the user confirms
the ice mass/final target and creates a local `flash-brew` wrapper around the unchanged stages. Tea
uploads contain leaf mass and the programmed 80/90 ml stages; app-display metadata
such as `output_ml_per_steep` is not a machine or cloud stage field. Disabled tea bypass placeholders
are compatibility residue and are never interpreted as an extra pour.


## Delete a created account recipe

`catalog delete` maps to the official App endpoint `tuRecipeDelete.tuhtml`. Preview is offline by
default and requires either `--table-id` or a local catalog `--id` that resolves to a remote
tableId:

```text
python <skill-dir>/scripts/xbloom.py catalog delete --region china --table-id 12345
python <skill-dir>/scripts/xbloom.py catalog delete --region china --id "My Recipe Name"
```

Only after the owner explicitly approves that exact delete may an Agent run:

```text
XBLOOM_ACCOUNT_EMAIL=<own-account-email>
XBLOOM_ACCOUNT_PASSWORD=<own-account-password>
python <skill-dir>/scripts/xbloom.py catalog delete --region china --table-id 12345 \
  --apply --confirm-delete own-account-cloud-recipe-delete
```

Before writing, the Skill logs in ephemerally, re-reads the member created list, and refuses any
tableId that is not present there. Optional name matching further refuses a mismatch. Delete is
irreversible on the account and does not clear machine A/B/C slots or local YAML files. Official,
product, shared, and other non-owned records must not be targeted.

## Import App brew history into the local journal

The official App stores brew records behind `tuBrewRecordList.tuhtml`. The Skill can import those
records into the local journal so phone-only brews are still reviewable:

```text
XBLOOM_ACCOUNT_EMAIL=<own-account-email>
XBLOOM_ACCOUNT_PASSWORD=<own-account-password>
python <skill-dir>/scripts/xbloom.py catalog history-sync --region china
```

Imported rows are marked `source: app-cloud` and are coarser than local BLE telemetry: recipe name,
dose, brew time, cup type, and create timestamp are preserved; full stage telemetry is not. Local
bridge-owned terminal history is written once into SQLite:

```text
~/.xbloom-studio-brew/state.db   # history_events table (authoritative)
```

Legacy `brew-history.jsonl` is import-only after migration; runtime never appends it.

Inspect or annotate the journal without BLE:

```text
python <skill-dir>/scripts/xbloom.py history status
python <skill-dir>/scripts/xbloom.py history list --limit 20
python <skill-dir>/scripts/xbloom.py history note <event_id> "brighter citrus, a bit thin"
```


## A/B/C compatibility

Catalog membership and machine-slot storage are separate. Use this sequence:

```text
python <skill-dir>/scripts/xbloom.py catalog export <A-id> <A.yaml>
python <skill-dir>/scripts/xbloom.py catalog export <B-id> <B.yaml>
python <skill-dir>/scripts/xbloom.py catalog export <C-id> <C.yaml>
python <skill-dir>/scripts/xbloom.py validate <A.yaml> --slot
python <skill-dir>/scripts/xbloom.py validate <B.yaml> --slot
python <skill-dir>/scripts/xbloom.py validate <C.yaml> --slot
python <skill-dir>/scripts/xbloom.py save-slots <A.yaml> <B.yaml> <C.yaml>
```

The default scale flags are `on on on`. To reproduce an explicitly chosen per-slot setting, append
`--scale on off on` in A/B/C order. Do not infer scale-off from missing imported metadata.

The final command temporarily selects PRO mode, writes command `11510` for A, B, and C as one
atomic set, waits for the saved state, and returns to AUTO mode. It does not start a brew.

Easy/AUTO stores pours, grind, ratio, and the per-slot scale flag. It does not carry command
`8102`, so it cannot store an explicit coffee dose or post-brew bypass. At use time the machine
measures the dose and scales from the stored ratio. Consequently:

- `validate --slot`, one-shot `save-slots`, bridge `save-slots`, and the low-level frame builder
  all reject `bypass_ml` instead of silently losing it;
- tea never goes into A/B/C; use `tea-load`/`tea-start` with the siphon accessory;
- xPod/J20/reference-only records cannot be written directly;
- recipe names, notes, and citations are not machine-slot fields;
- all three slots are required because the verified firmware commits the batch only after C.

An Easy snapshot imported from app data is historical catalog provenance. Do not treat it as live
machine readback: the decoded BLE reports expose mode/order/count state, not complete slot recipe
blobs. To establish current A/B/C contents, use the user's current app/account response or rewrite
all three from validated local YAML.
