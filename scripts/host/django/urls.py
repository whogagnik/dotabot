from django.urls import path

from scripts.host.django.views import (
    RegisterVMView,
    GetAssignedAccountsView,
    RegisterHwndsView,
    SubmitFrameRawView,
    GetCommandView,
    AckCommandView,
)

urlpatterns = [
    path("planner/register-vm", RegisterVMView.as_view(), name="planner-register-vm"),
    path("planner/get-assigned-accounts", GetAssignedAccountsView.as_view(), name="planner-get-assigned-accounts"),
    path("planner/register-hwnds", RegisterHwndsView.as_view(), name="planner-register-hwnds"),
    path("planner/submit-frame-raw", SubmitFrameRawView.as_view(), name="planner-submit-frame-raw"),
    path("planner/get-command", GetCommandView.as_view(), name="planner-get-command"),
    path("planner/ack-command", AckCommandView.as_view(), name="planner-ack-command"),
]