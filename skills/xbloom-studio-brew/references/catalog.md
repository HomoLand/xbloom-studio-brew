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

The importer and request envelope have **D/T** evidence: they are derived from APK models and have
deterministic tests. Live service authentication and response compatibility are not **H** evidence
in this project. Prefer an authorized JSON/cache export whenever possible; treat cloud sync as a
read-only interoperability path that may need updating when the service or app changes.

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

`doctor` reports whether a cloud-config path exists, but never reads or prints its secret values.
No initial configuration is required for JSON/MMKV import, query, export, validation, or A/B/C
programming. Cloud sync alone requires separate account configuration.

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

## Optional read-only cloud sync

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

Sync the account/region-visible host coffee and tea lists:

```text
python <skill-dir>/scripts/xbloom.py catalog sync --config <private-config.json>
```

The default targets are `coffee` (`tHostRecipe.thtml`) and `tea` (`tuTeaRecipe.tuhtml`). Request
current or default Easy-mode snapshots only when the `easy_mode` fields are known:

```text
python <skill-dir>/scripts/xbloom.py catalog sync --config <private-config.json> \
  --include easy --include easy-default
```

The implementation reproduces the app's regional URL selection and chunked RSA/PKCS#1-v1.5 JSON
envelope. It performs bounded reads, no automatic retry, no credential logging, and persists only
normalized entries. A service error must be reported as a sync failure; never fall back to an
unapproved endpoint or account.

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
