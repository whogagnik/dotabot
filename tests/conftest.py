import contextlib
import ctypes
import sys
import types


class DummyCallable:
    def __call__(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        return DummyCallable()


class DummyModule(types.ModuleType):
    def __getattr__(self, name):
        return DummyCallable()


class DummyTensor:
    def __init__(self, *args, **kwargs):
        pass

    def to(self, *args, **kwargs):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return []

    def squeeze(self, *args, **kwargs):
        return self

    def detach(self):
        return self

    def astype(self, *args, **kwargs):
        return self


class DummyNoGrad:
    def __call__(self, func=None):
        if func is None:
            return self
        return func

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _ensure_module(name: str, module: types.ModuleType):
    if name not in sys.modules:
        sys.modules[name] = module


def _build_numpy_stub() -> types.ModuleType:
    module = types.ModuleType("numpy")
    module.ndarray = object
    module.float32 = float
    module.float64 = float
    module.int32 = int
    module.int64 = int
    module.uint8 = int
    module.zeros = lambda *args, **kwargs: []
    module.ones = lambda *args, **kwargs: []
    module.array = lambda *args, **kwargs: []
    module.asarray = lambda *args, **kwargs: []
    module.clip = lambda *args, **kwargs: []
    module.where = lambda *args, **kwargs: ([], [])
    module.linspace = lambda *args, **kwargs: []
    module.rot90 = lambda *args, **kwargs: []
    module.maximum = lambda *args, **kwargs: []
    module.maximum_reduce = lambda *args, **kwargs: []
    module.allclose = lambda *args, **kwargs: False
    module.exp = lambda *args, **kwargs: []
    module.mean = lambda *args, **kwargs: 0.0
    module.random = types.SimpleNamespace(seed=lambda *args, **kwargs: None)
    module.linalg = types.SimpleNamespace(norm=lambda *args, **kwargs: 0.0)
    return module


def _build_torch_stub() -> types.ModuleType:
    module = types.ModuleType("torch")
    module.__path__ = []
    module.Tensor = DummyTensor
    module.float32 = float
    module.int64 = int
    module.device = lambda *args, **kwargs: None
    module.from_numpy = lambda *args, **kwargs: DummyTensor()
    module.sigmoid = lambda *args, **kwargs: DummyTensor()
    module.no_grad = lambda *args, **kwargs: DummyNoGrad()

    nn = types.ModuleType("torch.nn")
    nn.Module = object
    nn.functional = DummyModule("torch.nn.functional")
    module.nn = nn

    optim = types.ModuleType("torch.optim")
    optim.Optimizer = object
    module.optim = optim

    utils = types.ModuleType("torch.utils")
    data = types.ModuleType("torch.utils.data")
    data.Dataset = object
    data.DataLoader = object
    data.random_split = lambda *args, **kwargs: []
    utils.data = data
    module.utils = utils

    return module


def _build_cv2_stub() -> types.ModuleType:
    module = types.ModuleType("cv2")
    module.COLOR_RGB2BGR = 0
    module.COLOR_BGR2RGB = 0
    module.INTER_LINEAR = 0
    module.cvtColor = lambda img, code=None: img
    module.resize = lambda img, size=None, interpolation=None: img
    module.threshold = lambda *args, **kwargs: (None, None)
    module.findContours = lambda *args, **kwargs: ([], None)
    module.drawContours = lambda *args, **kwargs: None
    module.morphologyEx = lambda *args, **kwargs: None
    module.erode = lambda *args, **kwargs: None
    module.dilate = lambda *args, **kwargs: None
    module.GaussianBlur = lambda *args, **kwargs: None
    return module


def _build_tk_stub() -> types.ModuleType:
    module = types.ModuleType("tkinter")
    module.Tk = object
    module.Text = object
    module.Frame = object
    module.Label = object
    module.Entry = object
    module.Button = object
    module.Scrollbar = object
    module.Spinbox = object
    module.StringVar = object
    module.IntVar = object
    module.END = "end"
    ttk = types.ModuleType("tkinter.ttk")
    ttk.Treeview = object
    ttk.Combobox = object
    ttk.Style = object
    module.ttk = ttk
    filedialog = types.ModuleType("tkinter.filedialog")
    messagebox = types.ModuleType("tkinter.messagebox")
    module.filedialog = filedialog
    module.messagebox = messagebox
    _ensure_module("tkinter.ttk", ttk)
    _ensure_module("tkinter.filedialog", filedialog)
    _ensure_module("tkinter.messagebox", messagebox)
    return module


def _build_requests_stub() -> types.ModuleType:
    module = types.ModuleType("requests")

    class DummySession:
        def __init__(self):
            self.headers = {}

        def post(self, *args, **kwargs):
            return DummyModule("requests.Response")

    module.Session = DummySession
    module.Response = DummyModule("requests.Response")
    return module


def _install_stubs():
    ctypes.WinDLL = lambda *args, **kwargs: DummyModule("ctypes.WinDLL")
    ctypes.windll = DummyModule("ctypes.windll")
    _ensure_module("numpy", _build_numpy_stub())
    torch_module = _build_torch_stub()
    _ensure_module("torch", torch_module)
    _ensure_module("torch.nn", torch_module.nn)
    _ensure_module("torch.nn.functional", torch_module.nn.functional)
    _ensure_module("torch.optim", torch_module.optim)
    _ensure_module("torch.utils", torch_module.utils)
    _ensure_module("torch.utils.data", torch_module.utils.data)
    _ensure_module("cv2", _build_cv2_stub())
    _ensure_module("pyautogui", DummyModule("pyautogui"))
    _ensure_module("win32gui", DummyModule("win32gui"))
    _ensure_module("win32api", DummyModule("win32api"))
    _ensure_module("win32con", DummyModule("win32con"))
    _ensure_module("win32process", DummyModule("win32process"))
    _ensure_module("dxcam", DummyModule("dxcam"))
    _ensure_module("psutil", DummyModule("psutil"))
    _ensure_module("requests", _build_requests_stub())
    steam_module = DummyModule("steam")
    steam_module.__path__ = []
    _ensure_module("steam", steam_module)
    _ensure_module("steam.guard", DummyModule("steam.guard"))
    _ensure_module("steam.protobufs", DummyModule("steam.protobufs"))
    _ensure_module("pytesseract", DummyModule("pytesseract"))
    _ensure_module("easyocr", DummyModule("easyocr"))
    _ensure_module("tqdm", DummyModule("tqdm"))
    pyzbar_module = DummyModule("pyzbar")
    pyzbar_module.__path__ = []
    _ensure_module("pyzbar", pyzbar_module)
    _ensure_module("pyzbar.pyzbar", DummyModule("pyzbar.pyzbar"))
    _ensure_module("PIL", DummyModule("PIL"))
    _ensure_module("PIL.Image", DummyModule("PIL.Image"))
    _ensure_module("PIL.ImageGrab", DummyModule("PIL.ImageGrab"))
    _ensure_module("tkinter", _build_tk_stub())


_install_stubs()
