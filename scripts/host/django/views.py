from __future__ import annotations

import json
import time

import numpy as np
from django.http import JsonResponse, HttpRequest
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from scripts.host.main import get_host_controller
from scripts.host.game.planner_runtime import planner_runtime


def _json_body(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


@method_decorator(csrf_exempt, name="dispatch")
class RegisterVMView(View):
    """
    POST /planner/register-vm

    {
      "vm_id": "vm_1",
      "capacity": 5,
      "side": "radiant"
    }
    """

    def post(self, request: HttpRequest):
        body = _json_body(request)

        vm_id = str(body["vm_id"])
        capacity = int(body.get("capacity", 5))
        side = str(body.get("side", "radiant"))

        controller = get_host_controller()
        vm = controller.register_vm(
            vm_id=vm_id,
            capacity=capacity,
            side=side,
        )

        return JsonResponse({
            "ok": True,
            "vm": {
                "vm_id": vm.vm_id,
                "capacity": vm.capacity,
                "side": vm.side,
                "status": vm.status,
            },
        })


@method_decorator(csrf_exempt, name="dispatch")
class GetAssignedAccountsView(View):
    """
    GET /planner/get-assigned-accounts?vm_id=vm_1
    """

    def get(self, request: HttpRequest):
        vm_id = str(request.GET["vm_id"])

        controller = get_host_controller()
        if vm_id not in controller.vms:
            return JsonResponse({
                "ok": False,
                "error": f"Unknown vm_id={vm_id}",
            }, status=404)

        accounts = controller.get_vm_accounts_payload(vm_id)

        return JsonResponse({
            "ok": True,
            "vm_id": vm_id,
            "accounts": accounts,
            "count": len(accounts),
        })


@method_decorator(csrf_exempt, name="dispatch")
class RegisterHwndsView(View):
    """
    POST /planner/register-hwnds

    {
      "vm_id": "vm_1",
      "hwnds": [111, 222, 333, 444, 555],
      "roles": ["unknown", "unknown", "unknown", "unknown", "unknown"],
      "side": "radiant"
    }
    """

    def post(self, request: HttpRequest):
        body = _json_body(request)

        vm_id = str(body["vm_id"])
        hwnds = [int(x) for x in body["hwnds"]]
        roles = [str(x) for x in body.get("roles", ["unknown"] * len(hwnds))]
        side = str(body.get("side", "radiant"))

        if len(hwnds) != len(roles):
            return JsonResponse({
                "ok": False,
                "error": "hwnds and roles must have same length",
            }, status=400)

        controller = get_host_controller()
        if vm_id not in controller.vms:
            return JsonResponse({
                "ok": False,
                "error": f"Unknown vm_id={vm_id}",
            }, status=404)

        controller.register_hwnds(
            vm_id=vm_id,
            hwnds=hwnds,
            roles=roles,
            side=side,
        )

        return JsonResponse({
            "ok": True,
            "vm_id": vm_id,
            "hwnds": hwnds,
            "roles": roles,
            "planner_active": True,
        })


@method_decorator(csrf_exempt, name="dispatch")
class SubmitFrameRawView(View):
    """
    POST /planner/submit-frame-raw?vm_id=...&hwnd=...&ts_client=...&width=...&height=...&channels=3&dtype=uint8&layout=HWC&color=RGB
    body = raw RGB bytes
    """

    def post(self, request: HttpRequest):
        vm_id = str(request.GET["vm_id"])
        hwnd = int(request.GET["hwnd"])
        ts_client = float(request.GET.get("ts_client", time.time()))
        width = int(request.GET["width"])
        height = int(request.GET["height"])
        channels = int(request.GET.get("channels", 3))
        dtype = str(request.GET.get("dtype", "uint8"))
        layout = str(request.GET.get("layout", "HWC"))
        color = str(request.GET.get("color", "RGB"))

        if dtype != "uint8":
            return JsonResponse({"ok": False, "error": "Only uint8 supported"}, status=400)
        if layout != "HWC":
            return JsonResponse({"ok": False, "error": "Only HWC supported"}, status=400)
        if color != "RGB":
            return JsonResponse({"ok": False, "error": "Only RGB supported"}, status=400)

        entry = planner_runtime.get_entry(vm_id)
        if entry is None:
            return JsonResponse({
                "ok": False,
                "error": f"Planner runtime for vm_id={vm_id} is not registered",
            }, status=404)

        raw = request.body
        expected = width * height * channels
        if len(raw) != expected:
            return JsonResponse({
                "ok": False,
                "error": f"Invalid raw size: got={len(raw)}, expected={expected}",
            }, status=400)

        arr = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, channels))
        frame_id = entry.bridge.store_frame_rgb(
            hwnd=hwnd,
            frame_rgb=arr,
            ts_client=ts_client,
        )

        return JsonResponse({
            "ok": True,
            "vm_id": vm_id,
            "hwnd": hwnd,
            "frame_id": frame_id,
            "shape": [height, width, channels],
        })


@method_decorator(csrf_exempt, name="dispatch")
class GetCommandView(View):
    """
    GET /planner/get-command?vm_id=...&hwnd=...
    """

    def get(self, request: HttpRequest):
        vm_id = str(request.GET["vm_id"])
        hwnd = int(request.GET["hwnd"])

        entry = planner_runtime.get_entry(vm_id)
        if entry is None:
            return JsonResponse({
                "ok": False,
                "error": f"Planner runtime for vm_id={vm_id} is not registered",
            }, status=404)

        cmd = entry.bridge.get_next_command(hwnd)

        return JsonResponse({
            "ok": True,
            "command": cmd,
        })


@method_decorator(csrf_exempt, name="dispatch")
class AckCommandView(View):
    """
    POST /planner/ack-command

    {
      "vm_id": "vm_1",
      "hwnd": 123,
      "command_id": 456,
      "status": "done",
      "result": "ok"
    }
    """

    def post(self, request: HttpRequest):
        body = _json_body(request)

        vm_id = str(body["vm_id"])
        hwnd = int(body["hwnd"])
        command_id = int(body["command_id"])

        entry = planner_runtime.get_entry(vm_id)
        if entry is None:
            return JsonResponse({
                "ok": False,
                "error": f"Planner runtime for vm_id={vm_id} is not registered",
            }, status=404)

        ok = entry.bridge.ack_command(
            command_id=command_id,
            hwnd=hwnd,
        )

        return JsonResponse({
            "ok": bool(ok),
        })