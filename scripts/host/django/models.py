from __future__ import annotations

from django.db import models


class VM(models.Model):
    vm_id = models.CharField(max_length=128, unique=True)
    side = models.CharField(max_length=16, default="radiant")
    hwnds_json = models.JSONField(default=list, blank=True)
    roles_json = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "planner_vm"

    def __str__(self) -> str:
        return self.vm_id


class Frame(models.Model):
    vm_id = models.CharField(max_length=128, db_index=True)
    hwnd = models.BigIntegerField(db_index=True)

    image = models.ImageField(upload_to="planner_frames/%Y/%m/%d/")
    ts_client = models.FloatField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "planner_frame"
        indexes = [
            models.Index(fields=["vm_id", "hwnd", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Frame(id={self.id}, vm_id={self.vm_id}, hwnd={self.hwnd})"


class ActiveFrame(models.Model):
    vm_id = models.CharField(max_length=128, db_index=True)
    hwnd = models.BigIntegerField(db_index=True)
    frame = models.OneToOneField(Frame, on_delete=models.CASCADE, related_name="active_link")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "planner_active_frame"
        unique_together = ("vm_id", "hwnd")
        indexes = [
            models.Index(fields=["vm_id", "hwnd"]),
        ]

    def __str__(self) -> str:
        return f"ActiveFrame(vm_id={self.vm_id}, hwnd={self.hwnd}, frame_id={self.frame_id})"


class Command(models.Model):
    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "pending"),
        (STATUS_DONE, "done"),
        (STATUS_FAILED, "failed"),
    ]

    frame = models.ForeignKey(Frame, on_delete=models.CASCADE, related_name="commands")

    vm_id = models.CharField(max_length=128, db_index=True)
    hwnd = models.BigIntegerField(db_index=True)

    type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    result = models.TextField(default="", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    acked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "planner_command"
        indexes = [
            models.Index(fields=["vm_id", "hwnd", "status", "created_at"]),
            models.Index(fields=["frame", "status", "created_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"Command(id={self.id}, frame_id={self.frame_id}, vm_id={self.vm_id}, "
            f"hwnd={self.hwnd}, type={self.type}, status={self.status})"
        )