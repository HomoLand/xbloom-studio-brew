import base64
import hashlib
import json
from pathlib import Path

import pytest

import xbloom
import xbloom_catalog
from xbloom_ble.recipe import Recipe
from xbloom_ble.tea import TeaRecipe
from xbloom_catalog import (
    APP_RSA_PUBLIC_KEY_B64,
    CLOUD_WRITE_CONFIRM_SENTINEL,
    DEFAULT_ACCOUNT_TARGETS,
    CatalogError,
    app_encrypt_form,
    build_cloud_recipe_form,
    catalog_summary,
    cloud_recipe_preview,
    empty_catalog,
    export_entry,
    get_entry,
    import_json_file,
    import_payload,
    list_entries,
    load_catalog,
    load_cloud_recipe,
    load_cloud_config,
    push_cloud_recipe_with_login,
    save_catalog,
    sync_cloud,
    sync_cloud_with_login,
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


def test_one_ml_app_ratio_rounding_is_derived_from_pours_with_warning():
    catalog = empty_catalog()
    raw = _coffee()
    raw["grandWater"] = 16.5
    raw["pourList"][-1]["volume"] = 97
    import_payload(catalog, {"list": [raw]})
    entry = get_entry(catalog, "101")
    assert sum(pour["ml"] for pour in entry["recipe"]["pours"]) == 247
    assert "ratio" not in entry["recipe"]
    assert entry["executable"] is True
    assert entry["slot_compatible"] is True
    assert any(
        "differs from the integer pour total by 1 ml" in warning
        for warning in entry["warnings"]
    )


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


def test_disabled_tea_bypass_residue_is_ignored_like_the_app():
    catalog = empty_catalog()
    import_payload(
        catalog,
        {
            "list": [
                {
                    **_tea(),
                    "isEnableBypassWater": 2,
                    "bypassVolume": 5,
                    "bypassTemp": 40,
                }
            ]
        },
    )
    entry = get_entry(catalog, "202")
    assert entry["executable"] is True
    assert entry["slot_compatible"] is False
    assert entry["validation_errors"] == []
    assert "disabled app bypass values were ignored" in entry["warnings"]


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
    assert APP_RSA_PUBLIC_KEY_B64 == (
        "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC4LF40GZ72SdhMyl765K/i4nY5"
        "CPcHz2Q1IKWKZ9S79xmK7G8pUhbVf4EZLvnNF1+9IvOFQUKV5Z7ZNNviqSpnql9t"
        "AT+8+J/He0R7pcirvVSxgdr2i9V/C/gmqAEZ5qVTzRnd3uWdFoKzPdEBxP0IporJ1"
        "VBbCv90yBSOhVxO+QIDAQAB"
    )
    encrypted = app_encrypt_form(
        {"token": "visible-only-before-encryption", "padding": "x" * 200},
        randbytes=lambda length: b"\x01" * length,
    )
    ciphertext = base64.b64decode(encrypted)
    assert len(ciphertext) >= 256
    assert len(ciphertext) % 128 == 0
    assert b"visible-only-before-encryption" not in ciphertext
    assert hashlib.sha256(encrypted.encode("ascii")).hexdigest() == (
        "ccd5005bf1516569d08d4c1f47e2e6f3b732c8c7efe9cffcdedfa7c3f225297c"
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


def test_login_sync_uses_app_forms_and_never_returns_or_persists_credentials(monkeypatch):
    email = "account@example.test"
    password = "private-password"
    token = "private-session-token"
    observed = []

    def request(**kwargs):
        observed.append(kwargs)
        if kwargs["endpoint"] == xbloom_catalog.LOGIN_ENDPOINT:
            form = kwargs["form"]
            assert form["email"] == email
            assert form["password"] == password
            assert form["jpushId"] == ""
            assert form["token"] == ""
            assert form["memberId"] == 0
            assert form["interfaceVersion"] == xbloom_catalog.LOGIN_INTERFACE_VERSION
            return {
                "result": "success",
                "resultCode": 0,
                "token": token,
                "member": {"tableId": 42, "email": email},
                "projectToken": "unused-project-token",
                "projectRefreshToken": "unused-refresh-token",
            }
        form = kwargs["form"]
        assert kwargs["endpoint"] == xbloom_catalog.ENDPOINTS["coffee"]
        assert form["token"] == token
        assert form["memberId"] == 42
        assert form["clientSecretStr"] == "fixed-client-id"
        assert form["interfaceVersion"] == xbloom_catalog.CATALOG_INTERFACE_VERSION
        return {"list": [_coffee()]}

    monkeypatch.setattr(xbloom_catalog, "_cloud_request", request)
    catalog = empty_catalog()
    result = sync_cloud_with_login(
        catalog,
        email=email,
        password=password,
        region="international",
        include=("coffee",),
        language_type=3,
        client_secret="fixed-client-id",
    )

    assert [call["endpoint"] for call in observed] == [
        xbloom_catalog.LOGIN_ENDPOINT,
        xbloom_catalog.ENDPOINTS["coffee"],
    ]
    assert result["authenticated"] is True
    assert result["credentials_persisted"] is False
    assert result["session_persisted"] is False
    serialised = json.dumps({"result": result, "catalog": catalog})
    for secret in (email, password, token, "unused-project-token", "fixed-client-id"):
        assert secret not in serialised


def test_login_sync_rejection_is_redacted(monkeypatch):
    email = "account@example.test"
    password = "private-password"

    def rejected(**_kwargs):
        return {
            "result": "fail",
            "resultCode": 40001,
            "info": f"bad account {email} {password}",
        }

    monkeypatch.setattr(xbloom_catalog, "_cloud_request", rejected)
    with pytest.raises(CatalogError, match="resultCode=40001") as raised:
        sync_cloud_with_login(
            empty_catalog(),
            email=email,
            password=password,
            include=("coffee",),
        )
    assert email not in str(raised.value)
    assert password not in str(raised.value)


def test_login_sync_cli_reads_password_only_from_environment(
    monkeypatch, tmp_path, capsys
):
    email = "account@example.test"
    password = "private-password"
    observed = {}

    def sync(catalog, **kwargs):
        observed.update(kwargs)
        return {
            "scope": "own-account-region-visible",
            "region": kwargs["region"],
            "targets": [],
            "total": 0,
            "coffee": 0,
            "tea": 0,
            "executable": 0,
            "slot_compatible": 0,
            "updated_at": catalog["updated_at"],
            "authenticated": True,
            "credentials_persisted": False,
            "session_persisted": False,
        }

    monkeypatch.setattr(xbloom, "reexec_in_local_runtime", lambda: None)
    monkeypatch.setattr(xbloom_catalog, "sync_cloud_with_login", sync)
    monkeypatch.setenv(xbloom.ACCOUNT_EMAIL_ENV, email)
    monkeypatch.setenv(xbloom.ACCOUNT_PASSWORD_ENV, password)
    path = tmp_path / "catalog.json"
    assert xbloom.main(
        [
            "catalog",
            "--catalog-file",
            str(path),
            "login-sync",
            "--region",
            "china",
            "--language",
            "zh-cn",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert observed["email"] == email
    assert observed["password"] == password
    assert observed["language_type"] == 3
    assert email not in output
    assert password not in output
    saved = path.read_text(encoding="utf-8")
    assert email not in saved
    assert password not in saved


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


def test_account_sync_targets_cover_official_created_product_and_empty_shared(
    monkeypatch,
):
    responses = {
        xbloom_catalog.ENDPOINTS["coffee"]: {"list": [_coffee()]},
        xbloom_catalog.ENDPOINTS["tea"]: {"list": [_tea()]},
        xbloom_catalog.ENDPOINTS["created"]: {
            "result": "success",
            "list": [
                {
                    **_coffee(table_id=303),
                    "theName": "My Coffee",
                    "appPlace": [4],
                }
            ],
        },
        xbloom_catalog.ENDPOINTS["product"]: {
            "result": "success",
            "list": [
                {
                    **_coffee(table_id=404, cup_type=1),
                    "theName": "My xPod",
                }
            ],
        },
        xbloom_catalog.ENDPOINTS["shared"]: {"result": "success", "list": []},
    }
    calls = []

    def request(**kwargs):
        calls.append(kwargs["endpoint"])
        return responses[kwargs["endpoint"]]

    monkeypatch.setattr(xbloom_catalog, "_cloud_request", request)
    catalog = empty_catalog()
    result = sync_cloud(
        catalog,
        {
            "region": "china",
            "adapted_model": 1,
            "base_form": {"token": "session", "memberId": 42},
        },
    )

    assert tuple(target["target"] for target in result["targets"]) == DEFAULT_ACCOUNT_TARGETS
    assert calls == [xbloom_catalog.ENDPOINTS[target] for target in DEFAULT_ACCOUNT_TARGETS]
    assert result["total"] == 4
    assert get_entry(catalog, "303")["origin"] == "user-created"
    assert get_entry(catalog, "404")["origin"] == "xpod"
    assert result["targets"][-1]["candidates"] == 0


def test_tea_cloud_form_keeps_finished_output_out_of_programmed_pours():
    recipe = TeaRecipe.from_dict(
        {
            "name": "Two-stage tea",
            "kind": "tea",
            "leaf_g": 4,
            "output_ml_per_steep": 120,
            "pours": [
                {"ml": 90, "temp_c": 90, "pause_s": 20, "flow_ml_s": 3.5},
                {"ml": 80, "temp_c": 90, "pause_s": 15, "flow_ml_s": 3.5},
            ],
        }
    )
    form = build_cloud_recipe_form(recipe)
    pours = json.loads(form["pourDataJSONStr"])

    assert [pour["volume"] for pour in pours] == [90.0, 80.0]
    assert form["grandWater"] == 42.5
    assert form["isEnableBypassWater"] == 2
    assert form["bypassVolume"] == 5.0
    assert "output_ml_per_steep" not in form
    preview = cloud_recipe_preview(recipe)
    assert preview["write_performed"] is False
    assert preview["confirmation_required"] == CLOUD_WRITE_CONFIRM_SENTINEL
    assert any("firmware owns" in warning for warning in preview["warnings"])


def test_coffee_cloud_form_accepts_public_omni_dripper_name():
    mapping = xbloom_catalog.normalise_entry(_coffee())["recipe"]
    mapping["dripper"] = "Omni Dripper 2"

    form = build_cloud_recipe_form(Recipe.from_dict(mapping))

    assert form["cupType"] == 2


def test_flash_cloud_preview_discloses_manual_ice_boundary():
    recipe = Recipe.from_dict(
        {
            "name": "Iced 90 g",
            "kind": "flash-brew",
            "dripper": "Omni Dripper 2",
            "dose_g": 15,
            "grind": 50,
            "ratio": 10,
            "water_ml": 240,
            "hot_water_ml": 150,
            "ice_g": 90,
            "pours": [
                {
                    "ml": 40,
                    "temp_c": 94,
                    "pattern": "spiral",
                    "vibration": "after",
                    "pause_s": 35,
                    "rpm": 100,
                    "flow_ml_s": 3.0,
                },
                {
                    "ml": 60,
                    "temp_c": 93,
                    "pattern": "spiral",
                    "vibration": "none",
                    "pause_s": 5,
                    "rpm": 100,
                    "flow_ml_s": 3.4,
                },
                {
                    "ml": 50,
                    "temp_c": 92,
                    "pattern": "circular",
                    "vibration": "none",
                    "pause_s": 0,
                    "rpm": 100,
                    "flow_ml_s": 3.5,
                },
            ],
        }
    )

    preview = cloud_recipe_preview(recipe)

    assert preview["manual_preparation"] == {
        "ice_g": 90.0,
        "hot_water_ml": 150.0,
        "final_water_ml": 240.0,
    }
    assert any("stores only the hot extraction" in item for item in preview["warnings"])


def test_coffee_cloud_form_refuses_lossy_multiple_rpm_mapping():
    recipe = Recipe.from_dict(
        {
            "name": "Mixed RPM",
            "dose_g": 10,
            "grind": 60,
            "pours": [
                {
                    "ml": 50,
                    "temp_c": 92,
                    "pattern": "spiral",
                    "rpm": 80,
                    "flow_ml_s": 3.2,
                },
                {
                    "ml": 100,
                    "temp_c": 90,
                    "pattern": "circular",
                    "rpm": 100,
                    "flow_ml_s": 3.2,
                },
            ],
        }
    )
    with pytest.raises(CatalogError, match="one global RPM"):
        build_cloud_recipe_form(recipe)


def test_cloud_push_gate_prevents_login_or_write(monkeypatch):
    recipe = Recipe.from_dict(xbloom_catalog.normalise_entry(_coffee())["recipe"])

    def unexpected(**_kwargs):
        raise AssertionError("confirmation must be checked before any request")

    monkeypatch.setattr(xbloom_catalog, "_cloud_request", unexpected)
    with pytest.raises(CatalogError, match="exact confirmation"):
        push_cloud_recipe_with_login(
            recipe,
            email="account@example.test",
            password="private-password",
            region="china",
            confirm_write="",
        )


def test_cloud_push_is_idempotent_and_never_returns_session_secrets(monkeypatch):
    email = "account@example.test"
    password = "private-password"
    token = "private-session-token"
    remote = {**_coffee(), "appPlace": [4]}
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        if kwargs["endpoint"] == xbloom_catalog.LOGIN_ENDPOINT:
            return {
                "result": "success",
                "token": token,
                "member": {"tableId": 42},
            }
        if kwargs["endpoint"] == xbloom_catalog.ENDPOINTS["created"]:
            return {"result": "success", "list": [remote]}
        raise AssertionError("an already-present recipe must not be added again")

    monkeypatch.setattr(xbloom_catalog, "_cloud_request", request)
    recipe = Recipe.from_dict(xbloom_catalog.normalise_entry(remote)["recipe"])
    result = push_cloud_recipe_with_login(
        recipe,
        email=email,
        password=password,
        region="china",
        confirm_write=CLOUD_WRITE_CONFIRM_SENTINEL,
        client_secret="fixed-client-id",
    )

    assert result["status"] == "already-present"
    assert result["write_performed"] is False
    assert [call["endpoint"] for call in calls] == [
        xbloom_catalog.LOGIN_ENDPOINT,
        xbloom_catalog.ENDPOINTS["created"],
    ]
    serialised = json.dumps(result)
    for secret in (email, password, token, "fixed-client-id"):
        assert secret not in serialised


def test_cloud_push_tea_uses_recipe_add_form_but_test_never_writes_live(monkeypatch):
    observed = []

    def request(**kwargs):
        observed.append(kwargs)
        if kwargs["endpoint"] == xbloom_catalog.LOGIN_ENDPOINT:
            return {
                "result": "success",
                "token": "session",
                "member": {"tableId": 42},
            }
        if kwargs["endpoint"] == xbloom_catalog.ENDPOINTS["created"]:
            return {"result": "success", "list": []}
        assert kwargs["endpoint"] == xbloom_catalog.RECIPE_ADD_ENDPOINT
        return {"result": "success", "tableId": 999}

    monkeypatch.setattr(xbloom_catalog, "_cloud_request", request)
    recipe = TeaRecipe.from_dict(
        {
            "name": "Local green tea",
            "kind": "tea",
            "leaf_g": 4,
            "output_ml_per_steep": 120,
            "pours": [
                {"ml": 90, "temp_c": 85, "pause_s": 20, "flow_ml_s": 3.5}
            ],
        }
    )
    result = push_cloud_recipe_with_login(
        recipe,
        email="account@example.test",
        password="private-password",
        region="china",
        confirm_write=CLOUD_WRITE_CONFIRM_SENTINEL,
    )

    write = observed[-1]
    assert write["endpoint"] == xbloom_catalog.RECIPE_ADD_ENDPOINT
    assert write["form"]["interfaceVersion"] == xbloom_catalog.RECIPE_WRITE_INTERFACE_VERSION
    assert write["form"]["creatorId"] == 42
    assert write["form"]["grandWater"] == 22.5
    assert json.loads(write["form"]["pourDataJSONStr"])[0]["volume"] == 90.0
    assert result["status"] == "created"
    assert result["remote_table_id"] == 999
    assert result["write_performed"] is True


def test_catalog_push_defaults_to_offline_preview_without_credentials(
    monkeypatch, tmp_path, capsys
):
    source = tmp_path / "tea.json"
    source.write_text(
        json.dumps(
            {
                "name": "Preview tea",
                "kind": "tea",
                "leaf_g": 4,
                "output_ml_per_steep": 120,
                "pours": [
                    {"ml": 90, "temp_c": 85, "pause_s": 20, "flow_ml_s": 3.5}
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(xbloom, "reexec_in_local_runtime", lambda: None)
    monkeypatch.delenv(xbloom.ACCOUNT_EMAIL_ENV, raising=False)
    monkeypatch.delenv(xbloom.ACCOUNT_PASSWORD_ENV, raising=False)

    assert xbloom.main(["catalog", "push", str(source), "--region", "china"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "preview"
    assert output["write_performed"] is False
    assert output["app_recipe_form"]["theName"] == "Preview tea"
