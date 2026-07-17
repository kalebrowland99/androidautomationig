"""FastAPI dashboard — browser-based Android device lab."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dashboard import account_templates, brand_pools, debug_log, device_service, follow_vision_config, gramaddict_config, post_reel_config, telegram_commands, weditor_service
from dashboard.session_estimate import estimate_session
from dashboard.session_explain import explain_session
from GramAddict.core.account_safety import is_autopost_locked
from GramAddict.core.post_reel_account import load_post_reel_state
from dashboard.debug_tests import (
    DEBUG_GROUP_ORDER,
    DebugCancelled,
    begin_debug_run,
    clear_debug_cancel,
    list_debug_tests,
    raise_if_cancelled,
    run_debug_test,
    uses_production_debug_mode,
)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    debug_log.set_broadcast(_emit_debug_log)
    try:
        await asyncio.to_thread(weditor_service.ensure_running)
    except Exception as exc:
        print(f"Warning: Weditor did not start: {exc}")
    await asyncio.to_thread(telegram_commands.telegram_command_service.start)
    try:
        await _reconcile_device_links(notify=False)
    except Exception as exc:
        print(f"Warning: device link reconcile failed: {exc}")
    yield
    await asyncio.to_thread(telegram_commands.telegram_command_service.stop)
    debug_log.set_broadcast(None)
    await asyncio.to_thread(weditor_service.stop)


app = FastAPI(title="GramAddict Device Lab", version="1.0.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

_ws_clients: set[WebSocket] = set()
_last_devices: list[dict[str, str]] = []
_DEVICE_POLL_SECONDS = 12.0
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _emit_debug_log(serial: str, line: str) -> None:
    loop = _event_loop
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        _broadcast({"type": "debug_log", "serial": serial, "message": line}),
        loop,
    )


async def _device_call(fn, *args, **kwargs):
    """Run blocking adb/uiautomator work off the event loop."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except device_service.DeviceBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _reconcile_links_blocking() -> bool:
    """Full device scan (with hardware ids) + heal phone↔account links. Off-thread."""
    try:
        devices = device_service.get_devices_with_hardware_ids()
    except Exception:
        return False
    return gramaddict_config.reconcile_device_links(devices)


async def _reconcile_device_links(*, notify: bool = True) -> bool:
    """Heal account links when ADB serials change (wireless IP change, replug)."""
    changed = await asyncio.to_thread(_reconcile_links_blocking)
    if changed and notify:
        await _broadcast({"type": "accounts_changed"})
    return changed


def _run_debug_test_locked(serial: str, test_id: str, **kwargs: Any) -> dict[str, Any]:
    device_service.require_allowed_serial(serial)
    device_service.mark_device_busy(serial)
    production_mode = kwargs.pop("production_mode", None)
    if production_mode is None:
        production_mode = uses_production_debug_mode(test_id)
    begin_debug_run(serial, production_mode=production_mode)
    try:
        with device_service.device_operation(serial):
            raise_if_cancelled(serial)
            return run_debug_test(serial, test_id, **kwargs)
    except DebugCancelled:
        return {
            "success": False,
            "cancelled": True,
            "message": "Stopped by user",
            "test_id": test_id,
        }
    finally:
        clear_debug_cancel(serial)
        device_service.mark_device_idle(serial)


def _run_debug_batch_locked(serial: str, test_ids: list[str], **kwargs: Any) -> dict[str, Any]:
    device_service.require_allowed_serial(serial)
    device_service.mark_device_busy(serial)
    kwargs.pop("production_mode", None)
    production_mode = any(uses_production_debug_mode(test_id) for test_id in test_ids)
    begin_debug_run(serial, production_mode=production_mode)
    results: list[dict[str, Any]] = []
    try:
        with device_service.device_operation(serial):
            for test_id in test_ids:
                raise_if_cancelled(serial)
                result = run_debug_test(serial, test_id, **kwargs)
                results.append(result)
                if result.get("cancelled"):
                    return {
                        "success": False,
                        "cancelled": True,
                        "results": results,
                        "failed_at": test_id,
                        **result,
                    }
                if result.get("success") is False:
                    return {
                        "success": False,
                        "results": results,
                        "failed_at": test_id,
                        **result,
                    }
        return {"success": True, "results": results}
    except DebugCancelled:
        return {
            "success": False,
            "cancelled": True,
            "results": results,
            "message": "Stopped by user",
        }
    finally:
        clear_debug_cancel(serial)
        device_service.mark_device_idle(serial)


def _check_serial(serial: str) -> None:
    if not device_service.device_serial_allowed(serial):
        filt = device_service.get_device_filter()
        raise HTTPException(
            status_code=403,
            detail=f"Device {serial} is not enabled. Dashboard only uses serials matching {filt!r}.",
        )


class TapRequest(BaseModel):
    bounds: list[int] = Field(..., min_length=4, max_length=4)


class DebugRunBody(BaseModel):
    vpn_app_name: str = "Shadowrocket"
    target_username: Optional[str] = None
    target_search: Optional[str] = None
    target_post_url: Optional[str] = None
    test_message: Optional[str] = None
    likers_tap_offset_x: Optional[int] = None
    likers_tap_offset_y: Optional[int] = None
    post_reel_posts_count: Optional[int] = None


class DebugRunBatchBody(DebugRunBody):
    test_ids: list[str] = Field(..., min_length=1)


class AccountCreateBody(BaseModel):
    name: str


class AccountConfigBody(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    raw_yaml: Optional[str] = None


class AccountFileBody(BaseModel):
    content: str


class AccountFiltersBody(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)


class AccountTelegramBody(BaseModel):
    telegram: dict[str, Any] = Field(default_factory=dict)


class AccountPostReelBody(BaseModel):
    post_reel: dict[str, Any] = Field(default_factory=dict)


class AccountPostReelPromptsBody(BaseModel):
    prompts: dict[str, str] = Field(default_factory=dict)


class AccountFollowVisionBody(BaseModel):
    follow_vision: dict[str, Any] = Field(default_factory=dict)


class AccountFollowVisionPromptsBody(BaseModel):
    prompts: dict[str, str] = Field(default_factory=dict)


class SessionEstimateBody(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    post_reel: dict[str, Any] = Field(default_factory=dict)
    follow_vision: dict[str, Any] = Field(default_factory=dict)


class BotRunBody(BaseModel):
    device_serial: Optional[str] = None
    vpn_app_name: Optional[str] = None


class AccountDisableBody(BaseModel):
    disabled: bool = True
    reason: str = ""


class AccountNoteBody(BaseModel):
    note: str = ""


class DeviceAccountBody(BaseModel):
    username: str = ""


class SettingTemplateSaveBody(BaseModel):
    name: str


class ApplySettingsBody(BaseModel):
    source_type: str  # account | template
    source_id: str
    include_lists: bool = False


class SettingTemplateRenameBody(BaseModel):
    name: str


class ApplyTemplateBulkBody(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    include_lists: bool = False


class BrandPoolAccountsBody(BaseModel):
    accounts: list[str] = Field(default_factory=list)


class DistributeMediaBody(BaseModel):
    filename: str


class BrandPoolPostingBody(BaseModel):
    enabled: bool


@app.get("/api/brand-pools")
async def api_list_brand_pools() -> dict[str, Any]:
    return {
        "pools": brand_pools.list_brand_pools(),
        "unassigned": brand_pools.list_unassigned_accounts(),
    }


@app.put("/api/brand-pools/{pool_id}")
async def api_set_brand_pool_accounts(pool_id: str, body: BrandPoolAccountsBody) -> dict[str, Any]:
    try:
        return brand_pools.set_pool_accounts(pool_id, body.accounts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/brand-pools/{pool_id}/posting")
async def api_set_brand_pool_posting(
    pool_id: str, body: BrandPoolPostingBody
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            brand_pools.set_pool_posting, pool_id, body.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/brand-pools/{pool_id}/media")
async def api_list_brand_pool_media(pool_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(brand_pools.list_pool_media, pool_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/brand-pools/{pool_id}/media")
async def api_upload_brand_pool_media(
    pool_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="Filename is required")
        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            raise HTTPException(
                status_code=400,
                detail="Only video files are allowed (.mp4, .mov, .m4v, .webm, .mkv)",
            )
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty file")
        if len(data) > 500 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 500 MB)")
        result = await asyncio.to_thread(
            brand_pools.upload_media_to_pool, pool_id, file.filename, data
        )
        result["files"] = (await asyncio.to_thread(brand_pools.list_pool_media, pool_id))["files"]
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/brand-pools/{pool_id}/media/delete")
async def api_delete_brand_pool_media(
    pool_id: str, body: DistributeMediaBody
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            brand_pools.delete_media_from_pool, pool_id, body.filename
        )
        result["files"] = (await asyncio.to_thread(brand_pools.list_pool_media, pool_id))["files"]
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/gramaddict/templates")
async def api_list_setting_templates() -> dict[str, Any]:
    return {"templates": account_templates.list_setting_templates()}


@app.post("/api/gramaddict/accounts/{account_id}/save-template")
async def api_account_save_template(account_id: str, body: SettingTemplateSaveBody) -> dict[str, Any]:
    try:
        gramaddict_config.get_account(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return account_templates.save_setting_template(account_id, body.name)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/gramaddict/templates/{template_id}")
async def api_rename_setting_template(template_id: str, body: SettingTemplateRenameBody) -> dict[str, Any]:
    try:
        return account_templates.rename_setting_template(template_id, body.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/gramaddict/templates/{template_id}")
async def api_delete_setting_template(template_id: str) -> dict[str, Any]:
    try:
        return account_templates.delete_setting_template(template_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/gramaddict/templates/{template_id}/apply")
async def api_apply_template_bulk(template_id: str, body: ApplyTemplateBulkBody) -> dict[str, Any]:
    try:
        return account_templates.apply_template_to_accounts(
            template_id,
            body.account_ids,
            include_lists=body.include_lists,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/gramaddict/accounts/{account_id}/template-status")
async def api_account_template_status(account_id: str) -> dict[str, Any]:
    try:
        gramaddict_config.get_account(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return account_templates.get_account_template_status(account_id)


@app.post("/api/gramaddict/accounts/{account_id}/apply-settings")
async def api_account_apply_settings(account_id: str, body: ApplySettingsBody) -> dict[str, Any]:
    try:
        return account_templates.apply_settings_to_account(
            account_id,
            source_type=body.source_type,
            source_id=body.source_id,
            include_lists=body.include_lists,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    import json

    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    bootstrap = json.dumps({name: len(ids) for name, ids in DEBUG_GROUP_ORDER.items()})
    html = html.replace("__DEBUG_BOOTSTRAP__", bootstrap)
    return HTMLResponse(html)


@app.get("/api/devices/meta")
async def api_devices_meta() -> dict[str, str]:
    return {"device_filter": device_service.get_device_filter()}


@app.get("/api/devices")
async def api_devices(fast: bool = True) -> list[dict[str, str]]:
    try:
        return await _device_call(device_service.get_adb_devices, fast=fast)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/devices/{serial}")
async def api_device_detail(serial: str) -> dict[str, Any]:
    _check_serial(serial)
    devices = await _device_call(device_service.get_adb_devices, fast=True)
    device = next((d for d in devices if d["serial"] == serial), None)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not connected")
    device["mirroring"] = await _device_call(
        device_service.scrcpy_running_for_device, serial
    )
    return device


@app.put("/api/devices/{serial}/account")
async def api_assign_device_account(serial: str, body: DeviceAccountBody) -> dict[str, Any]:
    try:
        return gramaddict_config.assign_account_to_device(serial, body.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/weditor/status")
async def api_weditor_status() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(weditor_service.status)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/devices/{serial}/weditor/url")
async def api_weditor_url(serial: str) -> dict[str, str]:
    _check_serial(serial)
    return {"url": weditor_service.weditor_url(serial)}


@app.post("/api/devices/{serial}/weditor/connect")
async def api_weditor_connect(serial: str) -> dict[str, Any]:
    _check_serial(serial)
    try:
        return await asyncio.to_thread(weditor_service.connect_and_url, serial)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/devices/{serial}/inspector")
async def api_inspector(serial: str) -> dict[str, Any]:
    """Legacy JSON inspector API (used by export dump). Prefer Weditor in the UI."""
    _check_serial(serial)
    try:
        return await _device_call(device_service.get_inspector, serial)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/devices/{serial}/screenshot")
async def api_screenshot(serial: str) -> dict[str, str]:
    _check_serial(serial)
    try:
        return {"image": await _device_call(device_service.get_screenshot, serial)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/devices/{serial}/hierarchy")
async def api_hierarchy(serial: str) -> dict[str, Any]:
    _check_serial(serial)
    try:
        return await _device_call(device_service.get_hierarchy, serial)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/devices/{serial}/dump")
async def api_dump(serial: str) -> dict[str, Any]:
    _check_serial(serial)
    try:
        return await _device_call(device_service.dump_to_disk, serial)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/devices/{serial}/mirror")
async def api_mirror(serial: str) -> dict[str, str]:
    _check_serial(serial)
    try:
        return await _device_call(device_service.start_mirror, serial)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/devices/{serial}/home")
async def api_home(serial: str) -> dict[str, bool]:
    _check_serial(serial)
    try:
        await _device_call(device_service.press_home, serial)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/devices/{serial}/tap")
async def api_tap(serial: str, body: TapRequest) -> dict[str, bool]:
    _check_serial(serial)
    try:
        await _device_call(device_service.tap_bounds, serial, body.bounds)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/debug/tests")
async def api_debug_tests(group: Optional[str] = None) -> list[dict[str, Any]]:
    return list_debug_tests(group)


@app.get("/api/debug/meta")
async def api_debug_meta() -> dict[str, Any]:
    return {
        "groups": list(DEBUG_GROUP_ORDER.keys()),
        "counts": {name: len(ids) for name, ids in DEBUG_GROUP_ORDER.items()},
        "revision": "blogger-post-likers-prod-v1",
    }


@app.post("/api/devices/{serial}/debug/{test_id}")
async def api_run_debug_test(
    serial: str,
    test_id: str,
    body: Optional[DebugRunBody] = None,
) -> dict[str, Any]:
    vpn_app_name = (body.vpn_app_name if body else None) or "Shadowrocket"
    target_username = body.target_username if body else None
    target_search = body.target_search if body else None
    target_post_url = body.target_post_url if body else None
    test_message = body.test_message if body else None
    likers_tap_offset_x = body.likers_tap_offset_x if body else None
    likers_tap_offset_y = body.likers_tap_offset_y if body else None
    post_reel_posts_count = body.post_reel_posts_count if body else None
    _check_serial(serial)
    try:
        return await asyncio.to_thread(
            _run_debug_test_locked,
            serial,
            test_id,
            vpn_app_name=vpn_app_name,
            target_username=target_username,
            target_search=target_search,
            target_post_url=target_post_url,
            test_message=test_message,
            likers_tap_offset_x=likers_tap_offset_x,
            likers_tap_offset_y=likers_tap_offset_y,
            post_reel_posts_count=post_reel_posts_count,
        )
    except HTTPException:
        raise
    except device_service.DeviceBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/devices/{serial}/debug/run-batch")
async def api_run_debug_batch(
    serial: str,
    body: DebugRunBatchBody,
) -> dict[str, Any]:
    _check_serial(serial)
    try:
        return await asyncio.to_thread(
            _run_debug_batch_locked,
            serial,
            body.test_ids,
            vpn_app_name=body.vpn_app_name,
            target_username=body.target_username,
            target_search=body.target_search,
            target_post_url=body.target_post_url,
            test_message=body.test_message,
            likers_tap_offset_x=body.likers_tap_offset_x,
            likers_tap_offset_y=body.likers_tap_offset_y,
            post_reel_posts_count=body.post_reel_posts_count,
        )
    except HTTPException:
        raise
    except device_service.DeviceBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/devices/{serial}/warmup")
async def api_warmup_device(serial: str) -> dict[str, bool]:
    _check_serial(serial)
    try:
        await _device_call(device_service.warmup_device, serial)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/devices/{serial}/debug/cancel")
async def api_cancel_debug_test(serial: str) -> dict[str, bool]:
    _check_serial(serial)
    await asyncio.to_thread(device_service.stop_device_work, serial)
    return {"ok": True}


@app.get("/api/devices/{serial}/debug/status")
async def api_debug_status(serial: str) -> dict[str, bool]:
    _check_serial(serial)
    return {"busy": device_service.is_device_busy(serial)}


@app.get("/api/devices/{serial}/debug/logs")
async def api_debug_logs(serial: str, since: int = 0) -> dict[str, Any]:
    _check_serial(serial)
    lines, next_index = debug_log.get_lines(serial, since)
    return {"lines": lines, "next": next_index}


@app.post("/api/devices/{serial}/disconnect")
async def api_disconnect_device(serial: str) -> dict[str, bool]:
    _check_serial(serial)
    await _device_call(device_service.release_device_session, serial)
    return {"ok": True}


@app.get("/api/gramaddict/schema")
async def api_gramaddict_schema() -> dict[str, Any]:
    return gramaddict_config.get_field_schema()


@app.get("/api/gramaddict/schema/filters")
async def api_gramaddict_filters_schema() -> dict[str, Any]:
    return gramaddict_config.get_filters_schema()


@app.get("/api/gramaddict/schema/telegram")
async def api_gramaddict_telegram_schema() -> dict[str, Any]:
    return gramaddict_config.get_telegram_schema()


@app.get("/api/gramaddict/schema/vpn")
async def api_gramaddict_vpn_help() -> dict[str, str]:
    return {"help": gramaddict_config.get_vpn_help()}


@app.get("/api/gramaddict/schema/files")
async def api_gramaddict_files_schema() -> dict[str, Any]:
    return gramaddict_config.get_list_files_meta()


@app.get("/api/gramaddict/accounts/{account_id}/bundle")
async def api_gramaddict_account_bundle(account_id: str) -> dict[str, Any]:
    try:
        return gramaddict_config.get_account_bundle_status(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/gramaddict/accounts/{account_id}/ensure-files")
async def api_gramaddict_ensure_files(account_id: str) -> dict[str, Any]:
    try:
        return gramaddict_config.ensure_account_template_files(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/gramaddict/accounts")
async def api_gramaddict_accounts() -> list[dict[str, Any]]:
    return gramaddict_config.list_accounts()


@app.post("/api/gramaddict/accounts")
async def api_gramaddict_create_account(body: AccountCreateBody) -> dict[str, Any]:
    try:
        return gramaddict_config.create_account(body.name)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gramaddict/estimate")
async def api_gramaddict_estimate(body: SessionEstimateBody) -> dict[str, Any]:
    cfg = gramaddict_config.config_from_ui(body.config or {})
    return estimate_session(
        cfg, post_reel=body.post_reel or {}, follow_vision=body.follow_vision or {}
    )


@app.post("/api/gramaddict/accounts/{account_id}/explain-session")
async def api_gramaddict_explain_session(
    account_id: str, body: SessionEstimateBody
) -> dict[str, Any]:
    cfg = gramaddict_config.config_from_ui(body.config or {})
    return await run_in_threadpool(
        explain_session,
        cfg,
        post_reel=body.post_reel or {},
        follow_vision=body.follow_vision or {},
        account_id=account_id,
    )


@app.get("/api/gramaddict/accounts/{account_id}")
async def api_gramaddict_get_account(account_id: str) -> dict[str, Any]:
    try:
        return gramaddict_config.get_account(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _sync_template_after_edit(account_id: str) -> None:
    """Best-effort: push an account's saved settings back into its template.

    Runs after any account-tab save so a connected template stays in sync with
    the edits. Never fails the underlying save if the sync hits a problem.
    """
    try:
        account_templates.sync_template_from_account(account_id)
    except Exception as exc:  # noqa: BLE001 - sync is best-effort
        print(f"Warning: template sync failed for {account_id}: {exc}")


@app.put("/api/gramaddict/accounts/{account_id}")
async def api_gramaddict_save_account(
    account_id: str,
    body: AccountConfigBody,
) -> dict[str, Any]:
    try:
        if body.raw_yaml is not None:
            result = gramaddict_config.save_account_raw_yaml(account_id, body.raw_yaml)
        else:
            result = gramaddict_config.save_account_config(account_id, body.config)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/gramaddict/accounts/{account_id}")
async def api_gramaddict_delete_account(account_id: str) -> dict[str, Any]:
    try:
        return gramaddict_config.delete_account(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/gramaddict/accounts-status")
async def api_gramaddict_accounts_status() -> list[dict[str, Any]]:
    return await asyncio.to_thread(gramaddict_config.accounts_status)


@app.get("/api/gramaddict/accounts/{account_id}/status")
async def api_gramaddict_bot_status(account_id: str) -> dict[str, Any]:
    return gramaddict_config.bot_status(account_id)


@app.get("/api/gramaddict/accounts/{account_id}/log")
async def api_gramaddict_bot_log(
    account_id: str, lines: int = 500
) -> dict[str, Any]:
    lines = max(1, min(lines, 5000))
    return await asyncio.to_thread(
        gramaddict_config.read_bot_log, account_id, max_lines=lines
    )


@app.get("/api/gramaddict/accounts/{account_id}/story-likes-log")
async def api_gramaddict_story_likes_log(
    account_id: str, lines: int = 400
) -> dict[str, Any]:
    lines = max(1, min(lines, 2000))
    return await asyncio.to_thread(
        gramaddict_config.read_story_likes_log, account_id, max_lines=lines
    )


@app.get("/api/gramaddict/accounts/{account_id}/rate-limits")
async def api_gramaddict_rate_limits(
    account_id: str, limit: int = 20
) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    return await asyncio.to_thread(
        gramaddict_config.read_rate_limit_history, account_id, max_events=limit
    )


@app.post("/api/gramaddict/accounts/{account_id}/run")
async def api_gramaddict_run_bot(
    account_id: str,
    body: Optional[BotRunBody] = None,
) -> dict[str, Any]:
    device_serial = body.device_serial if body else None
    vpn_app_name = body.vpn_app_name if body else None
    if not device_serial and body is None:
        device_serial = None

    async def _on_log(acct: str, line: str) -> None:
        await _broadcast({"type": "bot_log", "account": acct, "message": line})

    try:
        return await gramaddict_config.start_bot(
            account_id,
            device_serial=device_serial,
            vpn_app_name=vpn_app_name,
            log_callback=_on_log,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/gramaddict/accounts/{account_id}/stop")
async def api_gramaddict_stop_bot(
    account_id: str, force: bool = False
) -> dict[str, Any]:
    # Run off the event loop so concurrent stop requests (e.g. "Stop selected"
    # firing several at once) actually kill in parallel instead of serializing.
    return await asyncio.to_thread(gramaddict_config.stop_bot, account_id, force=force)


@app.post("/api/gramaddict/accounts/{account_id}/disable")
async def api_gramaddict_disable_account(
    account_id: str, body: AccountDisableBody
) -> dict[str, Any]:
    return await asyncio.to_thread(
        gramaddict_config.set_account_disabled,
        account_id,
        body.disabled,
        body.reason,
    )


@app.post("/api/gramaddict/accounts/{account_id}/note")
async def api_gramaddict_set_account_note(
    account_id: str, body: AccountNoteBody
) -> dict[str, Any]:
    return await asyncio.to_thread(
        gramaddict_config.set_account_note, account_id, body.note
    )


@app.get("/api/gramaddict/accounts/{account_id}/filters")
async def api_gramaddict_get_filters(account_id: str) -> dict[str, Any]:
    try:
        return gramaddict_config.get_account_filters(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/gramaddict/accounts/{account_id}/filters")
async def api_gramaddict_save_filters(
    account_id: str,
    body: AccountFiltersBody,
) -> dict[str, Any]:
    try:
        result = gramaddict_config.save_account_filters(account_id, body.filters)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/gramaddict/accounts/{account_id}/telegram")
async def api_gramaddict_get_telegram(account_id: str) -> dict[str, Any]:
    try:
        return gramaddict_config.get_account_telegram(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/gramaddict/accounts/{account_id}/telegram")
async def api_gramaddict_save_telegram(
    account_id: str,
    body: AccountTelegramBody,
) -> dict[str, Any]:
    try:
        result = gramaddict_config.save_account_telegram(account_id, body.telegram)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/gramaddict/schema/follow-vision")
async def api_gramaddict_follow_vision_schema() -> dict[str, Any]:
    return follow_vision_config.get_follow_vision_schema()


@app.get("/api/gramaddict/accounts/{account_id}/follow-vision")
async def api_gramaddict_get_follow_vision(account_id: str) -> dict[str, Any]:
    try:
        return {
            "settings": follow_vision_config.get_account_follow_vision(account_id),
            "prompts": follow_vision_config.get_account_follow_vision_prompts(account_id),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/gramaddict/accounts/{account_id}/follow-vision")
async def api_gramaddict_save_follow_vision(
    account_id: str,
    body: AccountFollowVisionBody,
) -> dict[str, Any]:
    try:
        result = follow_vision_config.save_account_follow_vision(account_id, body.follow_vision)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/gramaddict/accounts/{account_id}/follow-vision/prompts")
async def api_gramaddict_save_follow_vision_prompts(
    account_id: str,
    body: AccountFollowVisionPromptsBody,
) -> dict[str, str]:
    try:
        result = follow_vision_config.save_account_follow_vision_prompts(account_id, body.prompts)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/gramaddict/schema/post-reel")
async def api_gramaddict_post_reel_schema() -> dict[str, Any]:
    return post_reel_config.get_post_reel_schema()


@app.get("/api/gramaddict/accounts/{account_id}/post-reel")
async def api_gramaddict_get_post_reel(account_id: str) -> dict[str, Any]:
    try:
        return {
            "settings": post_reel_config.get_account_post_reel(account_id),
            "prompts": post_reel_config.get_account_post_reel_prompts(account_id),
            "media_dir": str(post_reel_config.media_dir_for_account(account_id)),
            "state": load_post_reel_state(account_id),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/gramaddict/accounts/{account_id}/post-reel")
async def api_gramaddict_save_post_reel(
    account_id: str,
    body: AccountPostReelBody,
) -> dict[str, Any]:
    try:
        result = post_reel_config.save_account_post_reel(account_id, body.post_reel)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/gramaddict/accounts/{account_id}/post-reel/prompts")
async def api_gramaddict_save_post_reel_prompts(
    account_id: str,
    body: AccountPostReelPromptsBody,
) -> dict[str, str]:
    try:
        result = post_reel_config.save_account_post_reel_prompts(account_id, body.prompts)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _reject_autopost_media(account_id: str) -> None:
    folder = gramaddict_config.ACCOUNTS_DIR / account_id
    config_path = folder / "config.yml"
    username = ""
    if config_path.is_file():
        data = gramaddict_config._load_yaml(config_path)
        username = str(data.get("username") or "")
    if is_autopost_locked(account_id, username):
        raise HTTPException(
            status_code=403,
            detail=f"Reel uploads are locked for @{username or account_id} (autopost safety).",
        )


@app.get("/api/gramaddict/accounts/{account_id}/post-reel/media")
async def api_gramaddict_list_post_media(account_id: str) -> dict[str, Any]:
    try:
        return {
            "files": post_reel_config.list_post_media_files(account_id),
            "media_dir": str(post_reel_config.media_dir_for_account(account_id)),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/gramaddict/accounts/{account_id}/post-reel/media")
async def api_gramaddict_upload_post_media(
    account_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    try:
        _reject_autopost_media(account_id)
        if not file.filename:
            raise HTTPException(status_code=400, detail="Filename is required")
        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            raise HTTPException(
                status_code=400,
                detail="Only video files are allowed (.mp4, .mov, .m4v, .webm, .mkv)",
            )
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty file")
        if len(data) > 500 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 500 MB)")
        saved = post_reel_config.save_post_media_upload(account_id, file.filename, data)
        return {"ok": True, "file": saved, "files": post_reel_config.list_post_media_files(account_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/gramaddict/accounts/{account_id}/post-reel/media/{filename}")
async def api_gramaddict_delete_post_media(account_id: str, filename: str) -> dict[str, Any]:
    try:
        _reject_autopost_media(account_id)
        post_reel_config.delete_post_media_file(account_id, filename)
        return {"ok": True, "files": post_reel_config.list_post_media_files(account_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gramaddict/accounts/{account_id}/post-reel/media/distribute")
async def api_gramaddict_distribute_post_media(
    account_id: str,
    body: DistributeMediaBody,
) -> dict[str, Any]:
    try:
        gramaddict_config.get_account(account_id)
        return account_templates.distribute_media_to_connected_accounts(
            account_id, body.filename
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gramaddict/accounts/{account_id}/post-reel/media/delete-connected")
async def api_gramaddict_delete_post_media_connected(
    account_id: str,
    body: DistributeMediaBody,
) -> dict[str, Any]:
    try:
        gramaddict_config.get_account(account_id)
        result = account_templates.delete_media_from_connected_accounts(
            account_id, body.filename
        )
        result["files"] = post_reel_config.list_post_media_files(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/gramaddict/accounts/{account_id}/files")
async def api_gramaddict_list_files(account_id: str) -> list[dict[str, str]]:
    try:
        return gramaddict_config.list_account_files(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/gramaddict/accounts/{account_id}/files/{filename}")
async def api_gramaddict_get_file(account_id: str, filename: str) -> dict[str, str]:
    try:
        return gramaddict_config.get_account_file(account_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/gramaddict/accounts/{account_id}/files/{filename}")
async def api_gramaddict_save_file(
    account_id: str,
    filename: str,
    body: AccountFileBody,
) -> dict[str, str]:
    try:
        result = gramaddict_config.save_account_file(account_id, filename, body.content)
        _sync_template_after_edit(account_id)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dumps/{path:path}")
async def api_dump_file(path: str) -> FileResponse:
    dump_root = (PROJECT_ROOT / "dump").resolve()
    resolved = (dump_root / path).resolve()
    if not str(resolved).startswith(str(dump_root)):
        raise HTTPException(status_code=403, detail="Invalid path")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved)


async def _broadcast(event: dict[str, Any]) -> None:
    dead: list[WebSocket] = []
    payload = json.dumps(event)
    for ws in _ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    global _last_devices
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "connected"}))
        try:
            devices = await _device_call(device_service.get_adb_devices, fast=True)
            _last_devices = devices
            await ws.send_text(json.dumps({"type": "devices", "devices": devices}))
        except Exception as exc:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))

        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=_DEVICE_POLL_SECONDS)
            except asyncio.TimeoutError:
                try:
                    devices = await _device_call(device_service.get_adb_devices, fast=True)
                    serials_changed = {d["serial"] for d in devices} != {
                        d["serial"] for d in _last_devices
                    }
                    if devices != _last_devices:
                        _last_devices = devices
                        await _broadcast({"type": "devices", "devices": devices})
                    # A changed serial set can mean a phone reconnected on a new
                    # wireless IP — heal account links so @names stick to the phone.
                    if serials_changed:
                        healed = await _reconcile_device_links()
                        if healed:
                            refreshed = await _device_call(
                                device_service.get_adb_devices, fast=True
                            )
                            _last_devices = refreshed
                            await _broadcast(
                                {"type": "devices", "devices": refreshed}
                            )
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


def main() -> None:
    import os

    import uvicorn

    feed_count = len(DEBUG_GROUP_ORDER.get("feed", []))
    print(
        f"GramAddict dashboard — debug groups: {', '.join(DEBUG_GROUP_ORDER.keys())} "
        f"(feed={feed_count} steps)"
    )

    reload = os.environ.get("DASHBOARD_RELOAD", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    reload_dirs = [str(BASE_DIR), str(PROJECT_ROOT / "GramAddict")]
    uvicorn.run(
        "dashboard.app:app",
        host="127.0.0.1",
        port=8080,
        reload=reload,
        reload_dirs=reload_dirs if reload else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
