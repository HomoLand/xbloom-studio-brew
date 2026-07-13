import base64
import hashlib
import json
from pathlib import Path

import pytest

import xbloom
from xbloom_ble.recipe import Recipe
from xbloom_ble.tea import TeaRecipe
from xbloom_catalog import (
    CatalogError,
    app_encrypt_form,
    catalog_summary,
    empty_catalog,
    export_entry,
    get_entry,
    import_json_file,
    import_payload,
    list_entries,
    load_catalog,
    load_cloud_config,
    save_catalog,
    sync_cloud,
)
from xbloom_safety import strict_validate


def _coffee(*, table_id=101, cup_type=2, adapted_model=1):
    return {
        "tableId": table_id,
        "theName": "Official Coffee",
        "appPlace": [1, 6],
        "adaptedModel": adapted_model,
        "cupType": cup_type,
        "dose": 15.0,
        "grandWater": 16.0,
        "grinderSize": 60.0,
        "isSetGrinderSize": 1,
        "isEnableBypassWater": 2,
        "rpm": 90,
        "pourList": [
            {
                "theName": "Bloom",
                "volume": 45.0,
                "temperature": 92.0,
                "pattern": 2,
                "pausing": 35,
                "flowRate": 3.0,
                "isEnableVibrationBefore": 2,
                "isEnableVibrationAfter": 1,
            },
            {
                "theName": "Main",
                "volume": 105.0,
                "temperature": 92.0,
                "pattern": 2,
                "pausing": 10,
                "flowRate": 3.2,
                "isEnableVibrationBefore": 2,
                "isEnableVibrationAfter": 2,
            },
            {
                "theName": "Finish",
                "volume": 90.0,
                "temperature": 91.0,
                "pattern": 3,
                "pausing": 0,
                "flowRate": 3.2,
                "isEnableVibrationBefore": 2,
                "isEnableVibrationAfter": 2,
            },
        ],
    }


def _tea():
    return {
        "tableId": 202,
        "theName": "Official Green Tea",
        "appPlace": [3, 6],
        "adaptedModel": 1,
        "cupType": 4,
        "dose": 4,
        "outputMlPerSteep": 120,
        "pourList": [
            {
                "theName": "Steep 1",
                "volume": 90,
                "temperature": 85,
                "pattern": 3,
                "pausing": 20,
                "flowRate": 3.5,
            },
            {
                "theName": "Steep 2",
                "volume": 90,
                "temperature": 85,
                "pattern": 1,
                "pausing": 15,
                "flowRate": 3.5,
            },
        ],
    }


def test_import_app_envelopes_normalises_coffee_and_tea_without_raw_secrets():
    catalog = empty_catalog()
    payload = {
        "resultCode": 0,
        "token": "must-not-be-persisted",
        "list": [_coffee(), _tea()],
    }
    stats = import_payload(catalog, payload, endpoint="tHostRecipe.thtml")

    assert stats == {
        "candidates": 2,
        "added": 2,
        "updated": 0,
        "rejected": 0,
        "rejections": [],
        "total": 2,
    }
    assert catalog_summary(catalog)["coffee"] == 1
    assert catalog_summary(catalog)["tea"] == 1
    assert "must-not-be-persisted" not in json.dumps(catalog)

    coffee = get_entry(catalog, "101")
    assert coffee["origin"] == "xbloom"
    assert coffee["executable"] is True
    assert coffee["slot_compatible"] is True
    assert coffee["recipe"]["pours"][0]["vibration"] == "after"

    tea = get_entry(catalog, "Official Green Tea")
    assert tea["kind"] == "tea"
    assert tea["executable"] is True
    assert tea["slot_compatible"] is False
    assert "dedicated Omni Tea Brewer" in tea["slot_incompatibility"]


def test_easy_mode_snapshot_merges_slot_without_duplicate_recipe():
    catalog = empty_catalog()
    import_payload(catalog, {"list": [_coffee()]})
    stats = import_payload(
        catalog,
        {
            "easyModeDetailVoList": [
                {"position": "A", "scale": 1, "recipeSnapshotVo": _coffee()}
            ]
        },
        endpoint="tuEasyModeList.tuhtml",
    )

    assert stats["updated"] == 1
    assert stats["total"] == 1
    assert get_entry(catalog, "101")["slots"] == [{"position": "A", "scale": True}]


def test_bad_easy_metadata_does_not_abort_recipe_import():
    catalog = empty_catalog()
    stats = import_payload(
        catalog,
        {
            "easyModeDetailVoList": [
                {"position": "Z", "scale": "broken", "recipeSnapshotVo": _coffee()}
            ]
        },
    )
    assert stats["added"] == 1
    entry = get_entry(catalog, "101")
    assert entry["slots"] == []
    assert "unknown Easy slot position" in entry["warnings"][0]


def test_decoded_mmkv_json_and_catalog_round_trip(tmp_path):
    source = tmp_path / "decoded-mmkv.json"
    source.write_text(
        json.dumps({"member_J15_DiskRecipeList": json.dumps({"101": _coffee()})}),
        encoding="utf-8",
    )
    catalog = empty_catalog()
    stats = import_json_file(catalog, source, source_type="mmkv-json")
    assert stats["added"] == 1

    catalog_path = tmp_path / "private" / "catalog.json"
    save_catalog(catalog, catalog_path)
    loaded = load_catalog(catalog_path)
    assert get_entry(loaded, "101")["sources"][0]["file"] == source.name


def test_exported_catalog_entries_pass_guarded_loaders(tmp_path):
    catalog = empty_catalog()
    import_payload(catalog, {"list": [_coffee(), _tea()]})

    coffee_path = export_entry(get_entry(catalog, "101"), tmp_path / "coffee.yaml")
    coffee = Recipe.from_yaml(coffee_path)
    strict_validate(coffee)
    tea_path = export_entry(get_entry(catalog, "202"), tmp_path / "tea.yaml")
    assert TeaRecipe.from_yaml(tea_path).kind == "tea"


def test_xpod_and_non_studio_entries_remain_reference_only(tmp_path):
    catalog = empty_catalog()
    import_payload(
        catalog,
        {
            "list": [
                {**_coffee(table_id=301, cup_type=1), "podsXid": "private-xid"},
                _coffee(table_id=302, adapted_model=2),
                _coffee(table_id=303, cup_type=3),
            ]
        },
    )

    xpod = get_entry(catalog, "301")
    assert xpod["origin"] == "xpod"
    assert xpod["executable"] is False
    with pytest.raises(CatalogError, match="reference-only"):
        export_entry(xpod, tmp_path / "xpod.yaml")
    assert get_entry(catalog, "302")["executable"] is False
    assert get_entry(catalog, "303")["executable"] is False


def test_fractional_app_dose_is_retained_as_reference_without_truncation():
    catalog = empty_catalog()
    import_payload(
        catalog,
        {"list": [{**_coffee(), "dose": 15.5, "grandWater": 240 / 15.5}]},
    )
    entry = get_entry(catalog, "101")
    assert entry["recipe"]["dose_g"] == 15.5
    assert entry["executable"] is False
    assert "whole grams" in entry["validation_errors"][0]


def test_tea_bypass_is_reference_only_instead_of_silently_dropped():
    catalog = empty_catalog()
    import_payload(
        catalog,
        {
            "list": [
                {
                    **_tea(),
                    "isEnableBypassWater": 1,
                    "bypassVolume": 30,
                    "bypassTemp": 85,
                }
            ]
        },
    )
    entry = get_entry(catalog, "202")
    assert entry["executable"] is False
    assert "cannot represent" in entry["validation_errors"][0]


def test_disabled_app_bypass_values_are_not_accidentally_enabled():
    catalog = empty_catalog()
    import_payload(
        catalog,
        {
            "list": [
                {
                    **_coffee(),
                    "isEnableBypassWater": 2,
                    "bypassVolume": 30,
                    "bypassTemp": 85,
                }
            ]
        },
    )
    entry = get_entry(catalog, "101")
    assert entry["executable"] is True
    assert entry["slot_compatible"] is True
    assert "bypass_ml" not in entry["recipe"]
    assert entry["recipe"]["water_ml"] == 240
    assert "disabled app bypass values were ignored" in entry["warnings"]


def test_enabled_app_bypass_without_volume_is_reference_only():
    catalog = empty_catalog()
    import_payload(
        catalog,
        {"list": [{**_coffee(), "isEnableBypassWater": 1, "bypassVolume": 0}]},
    )
    entry = get_entry(catalog, "101")
    assert entry["executable"] is False
    assert "no positive bypass volume" in entry["validation_errors"][0]


def test_enabled_app_bypass_is_executable_but_not_slot_compatible():
    catalog = empty_catalog()
    import_payload(
        catalog,
        {
            "list": [
                {
                    **_coffee(),
                    "isEnableBypassWater": 1,
                    "bypassVolume": 30,
                    "bypassTemp": 85,
                }
            ]
        },
    )
    entry = get_entry(catalog, "101")
    assert entry["executable"] is True
    assert entry["slot_compatible"] is False
    assert entry["recipe"]["water_ml"] == 270
    assert entry["recipe"]["bypass_ml"] == 30


def test_list_filters_executable_and_slot_compatible_entries():
    catalog = empty_catalog()
    import_payload(catalog, {"list": [_coffee(), _tea()]})
    assert [item["id"] for item in list_entries(catalog, kind="tea")] == ["xbloom:202"]
    assert [item["id"] for item in list_entries(catalog, slot_compatible_only=True)] == [
        "xbloom:101"
    ]
    assert list_entries(catalog, query="official coffee")[0]["table_id"] == 101


def test_import_skips_bad_records_but_reports_rejections():
    catalog = empty_catalog()
    bad = {"tableId": 999, "theName": "Broken", "pourList": [{"volume": 40}]}
    stats = import_payload(catalog, {"list": [bad, _coffee()]})
    assert stats["added"] == 1
    assert stats["rejected"] == 1
    assert stats["rejections"][0]["name"] == "Broken"


def test_app_encryption_is_chunked_rsa_and_hides_plaintext():
    encrypted = app_encrypt_form(
        {"token": "visible-only-before-encryption", "padding": "x" * 200},
        randbytes=lambda length: b"\x01" * length,
    )
    ciphertext = base64.b64decode(encrypted)
    assert len(ciphertext) >= 256
    assert len(ciphertext) % 128 == 0
    assert b"visible-only-before-encryption" not in ciphertext
    assert hashlib.sha256(encrypted.encode("ascii")).hexdigest() == (
        "f577066fb2da960f47aba7c44b8c88f6750ee25d80babe0a3abb7c0c82d6eb2d"
    )


def test_cloud_sync_uses_explicit_config_and_imports_only_normalised_response(tmp_path):
    config_path = tmp_path / "cloud.json"
    base_form = {
        "skey": "skey",
        "phoneType": "android",
        "appVersion": "2.2.2",
        "clientDetail": "test",
        "clientSecretStr": "secret",
        "interfaceVersion": "1",
        "token": "private-token",
        "memberId": 42,
        "clientType": 0,
        "languageType": 0,
    }
    config_path.write_text(
        json.dumps({"region": "international", "base_form": base_form}),
        encoding="utf-8",
    )
    config = load_cloud_config(config_path)
    assert config["base_form"]["pageNumber"] == 1
    assert config["base_form"]["countPerPage"] == 0
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"list": [_coffee()]}).encode("utf-8")

    def opener(request, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        observed["body"] = request.data
        return Response()

    catalog = empty_catalog()
    result = sync_cloud(catalog, config, include=("coffee",), opener=opener)
    assert observed["url"].endswith("/tHostRecipe.thtml")
    assert observed["timeout"] == 20.0
    assert isinstance(json.loads(observed["body"].decode("utf-8")), str)
    assert result["scope"] == "own-account-region-visible"
    assert result["total"] == 1
    assert "private-token" not in json.dumps(catalog)


def test_cloud_config_rejects_wrong_field_types_and_non_studio_sync(tmp_path):
    config_path = tmp_path / "cloud.json"
    base_form = {
        "skey": "skey",
        "phoneType": "Android",
        "appVersion": "2.2.2",
        "clientDetail": "test",
        "clientSecretStr": "secret",
        "interfaceVersion": 19700101,
        "token": "token",
        "memberId": 42,
        "clientType": 0,
        "languageType": 0,
    }
    config_path.write_text(
        json.dumps({"region": "china", "base_form": {**base_form, "token": 123}}),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="non-empty strings"):
        load_cloud_config(config_path)

    with pytest.raises(CatalogError, match="adapted_model=1"):
        sync_cloud(
            empty_catalog(),
            {"region": "china", "adapted_model": 2, "base_form": base_form},
            include=("coffee",),
        )

    with pytest.raises(CatalogError, match="at least one target"):
        sync_cloud(
            empty_catalog(),
            {"region": "china", "adapted_model": 1, "base_form": base_form},
            include=(),
        )


def test_large_integer_ids_do_not_lose_precision():
    catalog = empty_catalog()
    table_id = 9_223_372_036_854_775_000
    import_payload(catalog, {"list": [_coffee(table_id=table_id)]})
    assert get_entry(catalog, str(table_id))["table_id"] == table_id


def test_catalog_cli_import_list_and_export_need_no_ble_runtime(
    monkeypatch, tmp_path, capsys
):
    source = tmp_path / "recipes.json"
    source.write_text(json.dumps({"list": [_coffee()]}), encoding="utf-8")
    catalog_path = tmp_path / "catalog.json"
    output = tmp_path / "coffee.yaml"
    monkeypatch.setattr(xbloom, "reexec_in_local_runtime", lambda: None)

    def unexpected_runtime_check():
        raise AssertionError("catalog command must not require BLE runtime")

    monkeypatch.setattr(xbloom, "require_runtime", unexpected_runtime_check)
    assert xbloom.main(
        [
            "catalog",
            "--catalog-file",
            str(catalog_path),
            "import-json",
            str(source),
        ]
    ) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["added"] == 1

    assert xbloom.main(
        ["catalog", "--catalog-file", str(catalog_path), "list", "--slot-compatible"]
    ) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["entries"][0]["id"] == "xbloom:101"

    assert xbloom.main(
        [
            "catalog",
            "--catalog-file",
            str(catalog_path),
            "export",
            "101",
            str(output),
        ]
    ) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["slot_compatible"] is True
    strict_validate(Recipe.from_yaml(output))


def test_catalog_cli_status_does_not_invent_an_update_time(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(xbloom, "reexec_in_local_runtime", lambda: None)
    path = tmp_path / "missing.json"
    assert xbloom.main(
        ["catalog", "--catalog-file", str(path), "status"]
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["exists"] is False
    assert status["updated_at"] is None
