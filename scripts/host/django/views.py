# scripts/host/django/views.py
from __future__ import annotations

import json
import time

import numpy as np
from django.http import HttpRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from scripts.host.app.controller import get_host_controller
from scripts.host.game.planner_runtime import planner_runtime


def _json_body(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


def _controller_or_503():
    try:
        return get_host_controller(), None
    except RuntimeError as e:
        return None, JsonResponse({"ok": False, "error": str(e)}, status=503)


@method_decorator(csrf_exempt, name="dispatch")
class RegisterVMView(View):
    """
    POST /planner/register-vm
    body: {}
    response:
    {
      "ok": true,
      "vm_id": "vm_1",
      "capacity": 5,
      "status": "registered"
    }
    """

    def post(self, request: HttpRequest):
        controller, err = _controller_or_503()
        if err is not None:
            return err

        vm = controller.register_vm()
        return JsonResponse(
            {
                "ok": True,
                "vm_id": vm.vm_id,
                "capacity": vm.capacity,
                "status": vm.status,
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class GetAssignedAccountsView(View):
    """
    GET /planner/get-assigned-accounts?vm_id=vm_1
    """

    def get(self, request: HttpRequest):
        vm_id = str(request.GET.get("vm_id") or "").strip()
        if not vm_id:
            return JsonResponse({"ok": False, "error": "vm_id is required"}, status=400)

        controller, err = _controller_or_503()
        if err is not None:
            return err

        vm = controller.get_vm(vm_id)
        if vm is None:
            return JsonResponse(
                {"ok": False, "error": f"Unknown vm_id={vm_id}"},
                status=404,
            )

        controller.touch_vm(vm_id)
        accounts = controller.get_vm_accounts_payload(vm_id)

        return JsonResponse(
            {
                "ok": True,
                "vm_id": vm_id,
                "accounts": accounts,
                "count": len(accounts),
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class SubmitFrameRawView(View):
    """
    POST /planner/submit-frame-raw?vm_id=...&hwnd=...&ts_client=...&width=...&height=...&channels=3&dtype=uint8&layout=HWC&color=RGB
    body = raw RGB bytes
    """

    def post(self, request: HttpRequest):
        vm_id = str(request.GET.get("vm_id") or "").strip()
        if not vm_id:
            return JsonResponse({"ok": False, "error": "vm_id is required"}, status=400)

        try:
            hwnd = int(request.GET["hwnd"])
            ts_client = float(request.GET.get("ts_client", time.time()))
            width = int(request.GET["width"])
            height = int(request.GET["height"])
            channels = int(request.GET.get("channels", 3))
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"Bad query params: {e}"}, status=400)

        dtype = str(request.GET.get("dtype", "uint8"))
        layout = str(request.GET.get("layout", "HWC"))
        color = str(request.GET.get("color", "RGB"))

        if dtype != "uint8":
            return JsonResponse({"ok": False, "error": "Only uint8 supported"}, status=400)
        if layout != "HWC":
            return JsonResponse({"ok": False, "error": "Only HWC supported"}, status=400)
        if color != "RGB":
            return JsonResponse({"ok": False, "error": "Only RGB supported"}, status=400)

        controller, err = _controller_or_503()
        if err is not None:
            return err

        vm = controller.get_vm(vm_id)
        if vm is None:
            return JsonResponse(
                {"ok": False, "error": f"Unknown vm_id={vm_id}"},
                status=404,
            )

        controller.touch_vm(vm_id)

        entry = planner_runtime.get_entry(vm_id)
        if entry is None:
            return JsonResponse(
                {"ok": False, "error": f"Planner runtime for vm_id={vm_id} is not registered"},
                status=404,
            )

        raw = request.body
        expected = width * height * channels
        if len(raw) != expected:
            return JsonResponse(
                {"ok": False, "error": f"Invalid raw size: got={len(raw)}, expected={expected}"},
                status=400,
            )

        arr = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, channels))
        frame_id = entry.bridge.store_frame_rgb(
            hwnd=hwnd,
            frame_rgb=arr,
            ts_client=ts_client,
        )

        return JsonResponse(
            {
                "ok": True,
                "vm_id": vm_id,
                "hwnd": hwnd,
                "frame_id": frame_id,
                "shape": [height, width, channels],
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class GetCommandView(View):
    """
    GET /planner/get-command?vm_id=vm_1
    """

    def get(self, request: HttpRequest):
        vm_id = str(request.GET.get("vm_id") or "").strip()
        if not vm_id:
            return JsonResponse({"ok": False, "error": "vm_id is required"}, status=400)

        controller, err = _controller_or_503()
        if err is not None:
            return err

        vm = controller.get_vm(vm_id)
        if vm is None:
            return JsonResponse(
                {"ok": False, "error": f"Unknown vm_id={vm_id}"},
                status=404,
            )

        controller.touch_vm(vm_id)
        cmd = controller.get_next_command(vm_id)

        return JsonResponse(
            {
                "ok": True,
                "command": cmd,
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class AckCommandView(View):
    """
    POST /planner/ack-command

    {
      "vm_id": "vm_1",
      "command_id": 123,
      "status": "done",
      "result": {...}
    }
    """

    def post(self, request: HttpRequest):
        body = _json_body(request)

        vm_id = str(body.get("vm_id") or "").strip()
        if not vm_id:
            return JsonResponse({"ok": False, "error": "vm_id is required"}, status=400)

        try:
            command_id = int(body["command_id"])
        except Exception:
            return JsonResponse({"ok": False, "error": "command_id is required"}, status=400)

        status = str(body.get("status", "done"))
        result = body.get("result")

        controller, err = _controller_or_503()
        if err is not None:
            return err

        vm = controller.get_vm(vm_id)
        if vm is None:
            return JsonResponse(
                {"ok": False, "error": f"Unknown vm_id={vm_id}"},
                status=404,
            )

        controller.touch_vm(vm_id)
        ok = controller.ack_command(
            vm_id=vm_id,
            command_id=command_id,
            status=status,
            result=result if isinstance(result, dict) else {"value": result},
        )

        return JsonResponse({"ok": bool(ok)})


@method_decorator(csrf_exempt, name="dispatch")
class VmLogView(View):
    """
    POST /planner/log

    {
      "vm_id": "vm_1",
      "level": "info",
      "source": "client",
      "event": "launch_started",
      "message": "account launch started",
      "payload": {...}
    }
    """

    def post(self, request: HttpRequest):
        body = _json_body(request)

        vm_id = str(body.get("vm_id") or "").strip()
        if not vm_id:
            return JsonResponse({"ok": False, "error": "vm_id is required"}, status=400)

        level = str(body.get("level", "info"))
        source = str(body.get("source", "client"))
        event = str(body.get("event", "log"))
        message = str(body.get("message", ""))
        payload = body.get("payload")

        controller, err = _controller_or_503()
        if err is not None:
            return err

        vm = controller.get_vm(vm_id)
        if vm is None:
            return JsonResponse(
                {"ok": False, "error": f"Unknown vm_id={vm_id}"},
                status=404,
            )

        controller.touch_vm(vm_id)
        controller.push_vm_log(
            vm_id=vm_id,
            level=level,
            source=source,
            event=event,
            message=message,
            payload=payload if isinstance(payload, dict) else None,
        )

        return JsonResponse({"ok": True})