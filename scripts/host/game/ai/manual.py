"""Optional Windows digit-key adapter, kept outside gameplay policies."""
from dataclasses import dataclass, field


@dataclass
class ManualStateInput:
    cooldown: float = .2
    last_press: float = 0.0
    previous: dict[int, bool] = field(default_factory=lambda: {i: False for i in range(1, 10)})

    def poll(self, now: float, count: int) -> int | None:
        pressed = None
        for digit in range(1, count + 1):
            down = self._down(0x30 + digit)
            if down and not self.previous.get(digit, False) and now - self.last_press >= self.cooldown and pressed is None:
                pressed = digit
            self.previous[digit] = down
        if pressed is not None:
            self.last_press = now
        return pressed

    @staticmethod
    def _down(vk: int) -> bool:
        try:
            import win32api
            return bool(win32api.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False
