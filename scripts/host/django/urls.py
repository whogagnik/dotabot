from django.urls import path

from scripts.host.django.views import (
    RegisterVMView,
    GetAssignedAccountsView,
    SubmitFrameRawView,
    GetCommandView,
    AckCommandView,
    VmLogView,
)

urlpatterns = [
    path("planner/register-vm", RegisterVMView.as_view(), name="planner-register-vm"),
    path("planner/get-assigned-accounts", GetAssignedAccountsView.as_view(), name="planner-get-assigned-accounts"),
    path("planner/submit-frame-raw", SubmitFrameRawView.as_view(), name="planner-submit-frame-raw"),
    path("planner/get-command", GetCommandView.as_view(), name="planner-get-command"),
    path("planner/ack-command", AckCommandView.as_view(), name="planner-ack-command"),
    path("planner/log", VmLogView.as_view(), name="planner-log"),
]