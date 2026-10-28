"""sap_mem.py - AIM core memory engine ported to Python (ctypes).

Faithful port of:
  * MemoryEngine.cs  (BstkVMM hypervisor guest-physical translation)
  * BlueStacks.cs    (guest Linux task walk, CR3, libil2cpp base)
  * Memory.cs        (typed read/write facade)

Everything talks to HD-Player.exe with OpenProcess + ReadProcessMemory /
WriteProcessMemory. The BlueStacks 5 hypervisor build keeps the guest RAM
behind BstkVMM structures, so guest virtual addresses are translated with
page-table walks and the hypervisor's own caches (range / chunk / page-map).

Only the Python standard library + psutil are used; no external tools.
"""

from __future__ import annotations

import ctypes
import queue
import struct
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

import psutil

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

PROCESS_CREATE_THREAD = 0x0002
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000

PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

WAIT_OBJECT_0 = 0x00000000

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL

kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL

kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t

kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
    wintypes.DWORD, wintypes.DWORD,
]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p

kernel32.VirtualFreeEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
]
kernel32.VirtualFreeEx.restype = wintypes.BOOL

kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
kernel32.CreateRemoteThread.restype = wintypes.HANDLE

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wintypes.BOOL
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.restype = wintypes.BOOL


def find_pid_by_name(process_name: str) -> int:
    """Find a process id by executable name (extension optional)."""
    want = process_name.lower()
    if want.endswith(".exe"):
        want = want[:-4]
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name == want:
                return int(proc.info.get("pid") or 0) or int(proc.pid)
        except Exception:
            continue
    return 0


# ---------------------------------------------------------------------------
# MemoryEngine - port of MemoryEngine.cs
# ---------------------------------------------------------------------------

class _Offsets:
    VmCpuCount = 0x3C
    VmReadCounter = 0x10AFF0
    VmDirectRamWindow = 0x104000
    VmDirectRamTag = 0x106000
    VmRamRangeMode = 0x106019
    VmRangeCache = 0x106050
    VmRangeTreeRoot = 0x106098
    VmMmio2Table = 0x1060B0
    VmChunkCache = 0x106860
    VmChunkTreeRoot = 0x106C60
    VmPageMapCache = 0x106C80
    VmCpuArray = 0x11FE00
    BstkMapChunkRva = 0xDA070
    ExpectedPvcpuDelta = 0x120000
    RangeStart = 0x0
    RangeSize = 0x8
    RangeHostBase = 0x30
    RangeLeft = 0x50
    RangeRight = 0x58
    RangePageDescTable = 0x70
    RangePageDescStride = 0x10
    PageDescState = 0x0
    PageDescInfo = 0x8
    ChunkNodeLeft = 0x0
    ChunkNodeRight = 0x8
    ChunkNodeKey = 0x10
    ChunkHostBase = 0x28
    MmioRangeHostBase = 0x70
    PageMapCacheStride = 0x20
    PageMapCachedTag = 0x0
    PageMapPageDesc = 0x8
    PageMapHostBase = 0x18
    RemoteStubSize = 64


_LONG_MODE_PAGE_FRAME_MASK = 0x000FFFFFFFFFF000
_PAGE_SIZE = 0x1000
_LARGE_PAGE_1GB_MASK = 0x000FFFFFFFE00000
_LARGE_PAGE_2MB_MASK = 0x000FFFFFFFE00000
_USER_POINTER_MIN = 0x10000
_USER_POINTER_MAX = 0x00007FFFFFFFFFFF
_READ_COUNTER_LIMIT = 1000000000000
_DIRECT_STATE_MASK_HIGH = 0x7000000000000
_DIRECT_STATE_MASK_LOW = 0x0FFFFFFFFFF000
_PRESENT_BIT = 1
_PAGE_SIZE_BIT = 1 << 7
_PAGE_MASK = ~0xFFF
_PAGE_OFFSET_MASK = 0xFFF
_CHUNK_CACHE_MASK = 0x3F
_RANGE_CACHE_MASK = 0x7
_PAGE_MAP_CACHE_MASK = 0xFF


def is_likely_user_pointer(value: int) -> bool:
    return _USER_POINTER_MIN <= value < _USER_POINTER_MAX


def is_likely_guest_kernel_pointer(value: int) -> bool:
    return value >= 0xFFFF000000000000


def try_convert_kernel_virtual_to_physical(kernel_va: int) -> Optional[int]:
    """Port of MemoryEngine.TryConvertKernelVirtualToPhysical."""
    if 0xFFFF888000000000 <= kernel_va < 0xFFFFC88000000000:
        return kernel_va - 0xFFFF888000000000
    if 0xFFFF880000000000 <= kernel_va < 0xFFFFC80000000000:
        return kernel_va - 0xFFFF880000000000
    if 0xFFFFFFFF80000000 <= kernel_va < 0xFFFFFFFFFF000000:
        return kernel_va - 0xFFFFFFFF80000000
    return None


class MemoryEngine:
    """Port of MemoryEngine.cs."""

    last_failure = ""

    def __init__(self, log: Optional[Callable[[str], None]] = None):
        self._log = log or (lambda msg: None)
        self.process_handle = None
        self.process_id = 0
        self.module_base = 0
        self.pvm = 0
        self.pvcpu0 = 0
        self.cpu_count = 0
        self.read_counter = 0
        self.region_size = 0
        self._cache_lock = threading.RLock()
        self._gva_to_physical_page = {}
        self._gva_to_host_page = {}
        self._phys_to_host_page = {}
        # Chunk pointer memo (chunk_id -> chunk struct pointer). The hypervisor's
        # own chunk cache has only 64 slots and the tree walk + remote-thread
        # fallback is slow, so remember what BstkVMM already resolved.
        self._chunk_memo = {}
        self._chunk_memo_fail = {}
        self._chunk_memo_lock = threading.RLock()
        self._chunk_map_lock = threading.RLock()
        # Fast mode: the entity/data thread must never block on the remote-thread
        # chunk fallback (each miss can take up to 1 s and they pile up while the
        # player moves). Unknown chunks are resolved by a background worker.
        self._fast_local = threading.local()
        self._chunk_queue = queue.Queue(maxsize=4096)
        self._chunk_pending = set()
        self._chunk_pending_lock = threading.Lock()
        self._resolver_stop = threading.Event()
        self._resolver_thread = None

    # -- state ---------------------------------------------------------------

    @property
    def is_initialized(self) -> bool:
        return bool(self.process_handle and self.process_id and self.pvm and self.pvcpu0)

    def _fail(self, reason: str) -> None:
        MemoryEngine.last_failure = reason
        self._log("[mem] " + reason)

    def clear_caches(self) -> None:
        with self._cache_lock:
            self._gva_to_physical_page.clear()
            self._gva_to_host_page.clear()
            self._phys_to_host_page.clear()
        with self._chunk_memo_lock:
            self._chunk_memo.clear()
            self._chunk_memo_fail.clear()

    def clear_translations(self) -> None:
        """Drop only VA->host translations, keep the expensive chunk memo.

        The data thread calls this periodically so a BlueStacks guest-page
        remap cannot leave every bone position frozen on an old host page.
        Guest-physical -> host and chunk mappings stay cached, so the re-walk
        is cheap and does not stall the scan for seconds.
        """
        with self._cache_lock:
            self._gva_to_physical_page.clear()
            self._gva_to_host_page.clear()

    def set_fast_mode(self, enabled: bool) -> None:
        """Per-thread switch: queue unknown chunks instead of blocking on them."""
        self._fast_local.enabled = bool(enabled)

    def _fast_mode(self) -> bool:
        return bool(getattr(self._fast_local, "enabled", False))

    def _ensure_resolver(self) -> None:
        if self._resolver_thread is not None and self._resolver_thread.is_alive():
            return
        with self._chunk_pending_lock:
            if self._resolver_thread is not None and self._resolver_thread.is_alive():
                return
            self._resolver_stop.clear()
            self._resolver_thread = threading.Thread(
                target=self._chunk_resolver_loop,
                name="sap-chunk-resolver",
                daemon=True,
            )
            self._resolver_thread.start()

    def _queue_chunk(self, chunk_id: int) -> None:
        self._ensure_resolver()
        with self._chunk_pending_lock:
            if chunk_id in self._chunk_pending or len(self._chunk_pending) >= 2048:
                return
            self._chunk_pending.add(chunk_id)
        try:
            self._chunk_queue.put_nowait(chunk_id)
        except queue.Full:
            with self._chunk_pending_lock:
                self._chunk_pending.discard(chunk_id)

    def _chunk_resolver_loop(self) -> None:
        while not self._resolver_stop.is_set():
            try:
                chunk_id = self._chunk_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if chunk_id is None:
                break
            try:
                if not self.is_initialized:
                    continue
                with self._chunk_map_lock:
                    pointer = self._map_chunk_via_bstkvmm(int(chunk_id))
                if pointer is not None:
                    self._remember_chunk(int(chunk_id), pointer)
                else:
                    with self._chunk_memo_lock:
                        if len(self._chunk_memo_fail) > 2048:
                            self._chunk_memo_fail.clear()
                        self._chunk_memo_fail[int(chunk_id)] = time.time()
            except Exception:
                pass
            finally:
                with self._chunk_pending_lock:
                    self._chunk_pending.discard(chunk_id)
                try:
                    self._chunk_queue.task_done()
                except Exception:
                    pass

    def shutdown(self) -> None:
        self._resolver_stop.set()
        try:
            self._chunk_queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._resolver_thread
        if thread is not None:
            thread.join(timeout=1.5)
        self._resolver_thread = None
        with self._chunk_pending_lock:
            self._chunk_pending.clear()
        while True:
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                break
        self.clear_caches()
        if self.process_handle:
            kernel32.CloseHandle(self.process_handle)
            self.process_handle = None
        self.process_id = 0
        self.module_base = 0
        self.pvm = 0
        self.pvcpu0 = 0
        self.cpu_count = 0
        self.read_counter = 0
        self.region_size = 0

    # -- raw host memory -----------------------------------------------------

    def _read_host_bytes(self, address: int, size: int) -> Optional[bytes]:
        if not self.process_handle or size <= 0:
            return None
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.process_handle, ctypes.c_void_p(address), buf,
            ctypes.c_size_t(size), ctypes.byref(read),
        )
        if not ok or read.value != size:
            return None
        return buf.raw[:size]

    def _write_host_bytes(self, address: int, data: bytes) -> bool:
        if not self.process_handle or not data:
            return False
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            self.process_handle, ctypes.c_void_p(address), buf,
            ctypes.c_size_t(len(data)), ctypes.byref(written),
        )
        return bool(ok) and written.value == len(data)

    def _read_host_value(self, address: int, fmt: str):
        sizes = {"B": 1, "H": 2, "I": 4, "Q": 8, "i": 4, "h": 2, "f": 4}
        size = sizes[fmt]
        data = self._read_host_bytes(address, size)
        if data is None:
            return 0
        return struct.unpack_from("<" + fmt, data)[0]

    def _u32_host(self, address: int) -> int:
        return self._read_host_value(address, "I")

    def _u64_host(self, address: int) -> int:
        return self._read_host_value(address, "Q")

    # -- module / process ----------------------------------------------------

    def _find_module_in_process(self, pid: int, module_name: str):
        """Return (base, size) of a module inside the target process."""
        want = module_name.lower()
        if want.endswith(".exe"):
            want = want[:-4]
        snapshot = kernel32.CreateToolhelp32Snapshot(
            TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return 0, 0
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
            if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
                return 0, 0
            while True:
                name = (entry.szModule or "").lower()
                if name.endswith(".exe"):
                    name = name[:-4]
                if name == want:
                    return int(entry.modBaseAddr or 0), int(entry.modBaseSize or 0)
                if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return 0, 0

    # -- initialization ------------------------------------------------------

    def initialize(self) -> bool:
        self.shutdown()
        MemoryEngine.last_failure = ""

        pid = find_pid_by_name("HD-Player.exe")
        if pid == 0:
            self._fail("BlueStacks (HD-Player.exe) is not running. Start the emulator and load Free Fire first.")
            return False

        self._log("[mem] Found 'HD-Player.exe' PID=%d" % pid)

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
            False, pid,
        )
        if not handle:
            err = ctypes.get_last_error()
            if err == 5:
                self._fail("Access denied opening BlueStacks. Run the panel as Administrator.")
            else:
                self._fail("Could not open HD-Player.exe (Win32 error %d)." % err)
            return False
        self.process_handle = handle
        self.process_id = pid
        self._log("[mem] OpenProcess OK")

        base, size = self._find_module_in_process(pid, "BstkVMM.dll")
        if not base:
            self._fail("BstkVMM.dll not found in HD-Player. This BlueStacks is not the hypervisor build this tool needs (check Hyper-V mode / BlueStacks version).")
            self.shutdown()
            return False
        self.module_base = base
        self._log("[mem] Module base=0x%X size=%d" % (base, size))

        vmi = self._locate_vm_info()
        if vmi is None:
            self._fail("BlueStacks VM structures not found inside BstkVMM. Your BlueStacks build likely differs from the one this engine was built for.")
            self.shutdown()
            return False

        self.pvm, self.pvcpu0, self.cpu_count, self.read_counter, self.region_size = vmi
        self._log("[mem] VM found! pvm=0x%X pvcpu=0x%X cpus=%d" % (self.pvm, self.pvcpu0, self.cpu_count))
        return True

    def _locate_vm_info(self):
        mbi = MEMORY_BASIC_INFORMATION()
        mbi_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)
        current_address = 0
        fallback = None

        while True:
            result = kernel32.VirtualQueryEx(
                self.process_handle, ctypes.c_void_p(current_address),
                ctypes.byref(mbi), ctypes.c_size_t(mbi_size),
            )
            if result != mbi_size:
                break

            protection = int(mbi.Protect) & 0xFF
            readable_writable = protection in (PAGE_READWRITE, PAGE_EXECUTE_READWRITE)

            if int(mbi.State) == MEM_COMMIT and readable_writable and int(mbi.RegionSize) >= 128 * 1024:
                candidate = int(mbi.BaseAddress or 0)

                cpu_count = self._u32_host(candidate + _Offsets.VmCpuCount)
                cpu_count_ok = 1 <= cpu_count <= 8
                cpu_window_ok = _Offsets.VmCpuArray + 8 <= int(mbi.RegionSize)
                cpu0 = self._u64_host(candidate + _Offsets.VmCpuArray)
                cpu0_read_ok = cpu0 != 0
                cpu0_ok = cpu0_read_ok and cpu0 == candidate + _Offsets.ExpectedPvcpuDelta
                read_counter = self._u64_host(candidate + _Offsets.VmReadCounter)
                read_counter_read_ok = read_counter != 0
                read_counter_ok = read_counter_read_ok and read_counter <= _READ_COUNTER_LIMIT

                if cpu_count_ok and cpu_window_ok and cpu0_ok and read_counter_ok:
                    return (candidate, cpu0, cpu_count, read_counter, int(mbi.RegionSize))

                if (fallback is None and cpu_count_ok and cpu_window_ok and cpu0_read_ok
                        and read_counter_ok and int(mbi.RegionSize) == _Offsets.ExpectedPvcpuDelta):
                    fallback = (candidate, cpu0, cpu_count, read_counter, int(mbi.RegionSize))

            next_addr = current_address + int(mbi.RegionSize)
            if next_addr <= current_address:
                break
            current_address = next_addr

        return fallback

    # -- chunk mapping via BstkVMM ------------------------------------------

    def _map_chunk_via_bstkvmm(self, chunk_id: int) -> Optional[int]:
        if not self.module_base or not self.process_id:
            return None

        remote_process = kernel32.OpenProcess(
            PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
            PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
            False, self.process_id,
        )
        if not remote_process:
            return None

        result_size = 16  # ulong + long
        total_size = result_size + _Offsets.RemoteStubSize
        remote_mem = kernel32.VirtualAllocEx(
            remote_process, None, ctypes.c_size_t(total_size),
            MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE,
        )
        if not remote_mem:
            kernel32.CloseHandle(remote_process)
            return None

        try:
            remote_result = int(remote_mem)
            remote_code = remote_result + result_size
            remote_fn = self.module_base + _Offsets.BstkMapChunkRva

            stub = bytearray(_Offsets.RemoteStubSize)
            stub[0:4] = b"\x48\x83\xEC\x28"
            stub[4:6] = b"\x48\xB9"
            struct.pack_into("<Q", stub, 6, self.pvm)
            stub[14] = 0xBA
            struct.pack_into("<I", stub, 15, chunk_id & 0xFFFFFFFF)
            stub[19:21] = b"\x49\xB8"
            struct.pack_into("<Q", stub, 21, remote_result)
            stub[29:31] = b"\x48\xB8"
            struct.pack_into("<Q", stub, 31, remote_fn)
            stub[39:41] = b"\xFF\xD0"
            stub[41:43] = b"\x48\xB9"
            struct.pack_into("<Q", stub, 43, remote_result)
            stub[51:54] = b"\x48\x63\xC0"
            stub[54:58] = b"\x48\x89\x41\x08"
            stub[58:62] = b"\x48\x83\xC4\x28"
            stub[62] = 0xC3

            written = ctypes.c_size_t(0)
            initial = b"\x00" * result_size
            kernel32.WriteProcessMemory(
                remote_process, ctypes.c_void_p(remote_result),
                ctypes.create_string_buffer(initial, result_size),
                ctypes.c_size_t(result_size), ctypes.byref(written))
            kernel32.WriteProcessMemory(
                remote_process, ctypes.c_void_p(remote_code),
                ctypes.create_string_buffer(bytes(stub), len(stub)),
                ctypes.c_size_t(len(stub)), ctypes.byref(written))

            thread_id = wintypes.DWORD(0)
            remote_thread = kernel32.CreateRemoteThread(
                remote_process, None, ctypes.c_size_t(0),
                ctypes.c_void_p(remote_code), None, 0, ctypes.byref(thread_id))
            if not remote_thread:
                return None

            wait_result = kernel32.WaitForSingleObject(remote_thread, 1000)
            kernel32.CloseHandle(remote_thread)
            if wait_result != WAIT_OBJECT_0:
                return None

            buf = ctypes.create_string_buffer(result_size)
            read = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(
                remote_process, ctypes.c_void_p(remote_result), buf,
                ctypes.c_size_t(result_size), ctypes.byref(read))
            if not ok or read.value != result_size:
                return None

            final = buf.raw[:result_size]
            chunk_pointer = struct.unpack_from("<Q", final, 0)[0]
            status = struct.unpack_from("<q", final, 8)[0]
            if status < 0 or chunk_pointer == 0:
                return None
            return chunk_pointer
        finally:
            kernel32.VirtualFreeEx(remote_process, remote_mem, ctypes.c_size_t(0), MEM_RELEASE)
            kernel32.CloseHandle(remote_process)

    def _find_chunk_from_cache(self, chunk_id: int) -> Optional[int]:
        slot = chunk_id & _CHUNK_CACHE_MASK
        slot_address = self.pvm + _Offsets.VmChunkCache + slot * 0x10
        cached_id = self._u32_host(slot_address)
        chunk_pointer = self._u64_host(slot_address + 0x8)
        if cached_id != chunk_id or not is_likely_user_pointer(chunk_pointer):
            return None
        return chunk_pointer

    def _find_chunk_memo(self, chunk_id: int) -> Optional[int]:
        with self._chunk_memo_lock:
            pointer = self._chunk_memo.get(chunk_id)
        if pointer is None:
            return None
        # A recycled chunk struct would break every page that lives in it.
        # BstkVMM keeps bit 0 clear and the pointer inside the VM heap, so a
        # dead pointer fails the host read below and is evicted.
        if not is_likely_user_pointer(pointer):
            with self._chunk_memo_lock:
                self._chunk_memo.pop(chunk_id, None)
            return None
        return pointer

    def _remember_chunk(self, chunk_id: int, pointer: int) -> None:
        if not is_likely_user_pointer(pointer):
            return
        with self._chunk_memo_lock:
            if len(self._chunk_memo) > 4096:
                self._chunk_memo.clear()
            self._chunk_memo[chunk_id] = pointer
            self._chunk_memo_fail.pop(chunk_id, None)

    def _map_chunk_via_bstkvmm_cached(self, chunk_id: int) -> Optional[int]:
        """Remote-thread chunk mapping with a short negative cache.

        This fallback creates a remote thread inside HD-Player and used to be
        hit on every unknown page. During movement the game touches a lot of
        new chunks, so those calls piled up and froze the whole data thread
        (the ESP then stayed glued to the screen until the player stopped).
        """
        now = time.time()
        with self._chunk_memo_lock:
            failed_at = self._chunk_memo_fail.get(chunk_id)
        if failed_at is not None and now - failed_at < 1.0:
            return None
        with self._chunk_map_lock:
            pointer = self._map_chunk_via_bstkvmm(chunk_id)
        if pointer is None:
            with self._chunk_memo_lock:
                if len(self._chunk_memo_fail) > 2048:
                    self._chunk_memo_fail.clear()
                self._chunk_memo_fail[chunk_id] = now
            return None
        self._remember_chunk(chunk_id, pointer)
        return pointer

    def _find_chunk_from_tree(self, chunk_id: int) -> Optional[int]:
        node = self._u64_host(self.pvm + _Offsets.VmChunkTreeRoot)
        while node:
            key = self._u32_host(node + _Offsets.ChunkNodeKey)
            if key == chunk_id:
                return node
            next_offset = _Offsets.ChunkNodeLeft if chunk_id < key else _Offsets.ChunkNodeRight
            node = self._u64_host(node + next_offset)
        return None

    def _find_physical_range(self, guest_phys: int) -> Optional[int]:
        slot = (guest_phys >> 20) & _RANGE_CACHE_MASK
        cache_address = self.pvm + _Offsets.VmRangeCache + slot * 8
        node = self._u64_host(cache_address)
        if node:
            range_start = self._u64_host(node + _Offsets.RangeStart)
            range_size = self._u64_host(node + _Offsets.RangeSize)
            if guest_phys >= range_start and guest_phys - range_start < range_size:
                return node

        node = self._u64_host(self.pvm + _Offsets.VmRangeTreeRoot)
        while node:
            range_start = self._u64_host(node + _Offsets.RangeStart)
            range_size = self._u64_host(node + _Offsets.RangeSize)
            if guest_phys >= range_start and guest_phys - range_start < range_size:
                return node
            next_offset = _Offsets.RangeLeft if guest_phys < range_start else _Offsets.RangeRight
            node = self._u64_host(node + next_offset)
        return None

    def _try_page_map_cache(self, guest_phys: int) -> Optional[int]:
        slot = (guest_phys >> 12) & _PAGE_MAP_CACHE_MASK
        entry_address = self.pvm + _Offsets.VmPageMapCache + slot * _Offsets.PageMapCacheStride
        page_tag = guest_phys & _PAGE_MASK
        cached_tag = self._u64_host(entry_address + _Offsets.PageMapCachedTag)
        host_base = self._u64_host(entry_address + _Offsets.PageMapHostBase)
        if cached_tag == page_tag and is_likely_user_pointer(host_base):
            return host_base | (guest_phys & _PAGE_OFFSET_MASK)
        return None

    def _map_physical_page_to_host_base(self, guest_phys: int) -> Optional[int]:
        range_result = self._find_physical_range(guest_phys)
        if range_result is None:
            return None

        range_start = self._u64_host(range_result + _Offsets.RangeStart)
        page_index = (guest_phys - range_start) >> 12
        page_desc_addr = range_result + _Offsets.RangePageDescTable + page_index * _Offsets.RangePageDescStride

        state = self._u64_host(page_desc_addr + _Offsets.PageDescState)
        info = self._u32_host(page_desc_addr + _Offsets.PageDescInfo)

        page_class = (state >> 51) & 0x7
        read_map_kind = (state >> 48) & 0x3
        ram_range_mode = self._read_host_value(self.pvm + _Offsets.VmRamRangeMode, "B")

        if page_class not in (2, 3) and ram_range_mode != 0:
            range_host_base = self._u64_host(range_result + _Offsets.RangeHostBase)
            if not is_likely_user_pointer(range_host_base):
                return None
            return range_host_base + ((guest_phys - range_start) & _PAGE_MASK)

        if read_map_kind == 0:
            return self.pvm + _Offsets.VmDirectRamWindow

        if page_class in (2, 3):
            mmio_id = info >> 24
            mmio_page = info & 0x00FFFFFF
            mmio_range = self._u64_host(self.pvm + _Offsets.VmMmio2Table + mmio_id * 8)
            if not is_likely_user_pointer(mmio_range):
                return None
            host_base = self._u64_host(mmio_range + _Offsets.MmioRangeHostBase)
            return host_base + (mmio_page << 12)

        chunk_id = info >> 9
        page_in_chunk = info & 0x1FF

        if chunk_id != 0:
            chunk_result = self._find_chunk_memo(chunk_id)
            if chunk_result is None:
                chunk_result = self._find_chunk_from_cache(chunk_id)
            if chunk_result is None:
                chunk_result = self._find_chunk_from_tree(chunk_id)
                if chunk_result is not None:
                    self._remember_chunk(chunk_id, chunk_result)
            if chunk_result is None:
                if self._fast_mode():
                    # Never block the ESP/data thread on the remote-thread
                    # fallback. The background resolver fills the memo and the
                    # next scan (40 ms later) reads this page normally.
                    self._queue_chunk(chunk_id)
                    return None
                chunk_result = self._map_chunk_via_bstkvmm_cached(chunk_id)
            if chunk_result is None:
                return None
            chunk_host_base = self._u64_host(chunk_result + _Offsets.ChunkHostBase)
            return chunk_host_base + (page_in_chunk << 12)

        if info == 0:
            direct_tag = self._u64_host(self.pvm + _Offsets.VmDirectRamTag)
            direct_class = page_class == 4
            direct_state_match = ((state & _DIRECT_STATE_MASK_HIGH) == 0 and
                                  (state & _DIRECT_STATE_MASK_LOW) == direct_tag)
            if direct_class or direct_state_match:
                return self.pvm + _Offsets.VmDirectRamWindow

        return None

    def _map_physical_to_host(self, guest_phys: int) -> Optional[int]:
        guest_phys_page = guest_phys & _PAGE_MASK
        with self._cache_lock:
            cached = self._phys_to_host_page.get(guest_phys_page)
        if cached is not None:
            return cached | (guest_phys & _PAGE_OFFSET_MASK)

        page_map_result = self._try_page_map_cache(guest_phys)
        if page_map_result is not None:
            with self._cache_lock:
                self._phys_to_host_page[guest_phys_page] = page_map_result & _PAGE_MASK
            return page_map_result

        page_base = self._map_physical_page_to_host_base(guest_phys)
        if page_base is None:
            return None

        with self._cache_lock:
            self._phys_to_host_page[guest_phys_page] = page_base
        return page_base | (guest_phys & _PAGE_OFFSET_MASK)

    # -- guest physical ------------------------------------------------------

    def read_guest_physical(self, guest_phys: int, size: int) -> Optional[bytes]:
        result = bytearray()
        copied = 0
        while copied < size:
            current_phys = guest_phys + copied
            chunk_size = min(size - copied, _PAGE_SIZE - (current_phys & _PAGE_OFFSET_MASK))

            host_pointer = self._map_physical_to_host(current_phys)
            if host_pointer is None:
                return None

            data = self._read_host_bytes(host_pointer, chunk_size)
            if data is None:
                with self._cache_lock:
                    self._phys_to_host_page.pop(current_phys & _PAGE_MASK, None)
                retry = self._map_physical_to_host(current_phys)
                if retry is None:
                    return None
                data = self._read_host_bytes(retry, chunk_size)
                if data is None:
                    return None
            result.extend(data)
            copied += chunk_size

        return bytes(result)

    def write_guest_physical(self, guest_phys: int, data: bytes) -> bool:
        size = len(data)
        written = 0
        while written < size:
            current_phys = guest_phys + written
            chunk_size = min(size - written, _PAGE_SIZE - (current_phys & _PAGE_OFFSET_MASK))

            host_pointer = self._map_physical_to_host(current_phys)
            if host_pointer is None:
                return False

            if not self._write_host_bytes(host_pointer, data[written:written + chunk_size]):
                with self._cache_lock:
                    self._phys_to_host_page.pop(current_phys & _PAGE_MASK, None)
                retry = self._map_physical_to_host(current_phys)
                if retry is None:
                    return False
                if not self._write_host_bytes(retry, data[written:written + chunk_size]):
                    return False
            written += chunk_size
        return True

    # -- guest kernel --------------------------------------------------------

    def read_guest_kernel(self, kernel_va: int, size: int) -> Optional[bytes]:
        result = bytearray()
        copied = 0
        while copied < size:
            current_va = kernel_va + copied
            chunk_size = min(size - copied, _PAGE_SIZE - (current_va & _PAGE_OFFSET_MASK))

            current_phys = try_convert_kernel_virtual_to_physical(current_va)
            if current_phys is None:
                return None

            host_pointer = self._map_physical_to_host(current_phys)
            if host_pointer is None:
                return None

            data = self._read_host_bytes(host_pointer, chunk_size)
            if data is None:
                with self._cache_lock:
                    self._phys_to_host_page.pop(current_phys & _PAGE_MASK, None)
                retry = self._map_physical_to_host(current_phys)
                if retry is None:
                    return None
                data = self._read_host_bytes(retry, chunk_size)
                if data is None:
                    return None
            result.extend(data)
            copied += chunk_size
        return bytes(result)

    # -- guest virtual (user) ------------------------------------------------

    def translate_guest_va(self, guest_cr3: int, guest_va: int, use_cache: bool = True) -> Optional[int]:
        canonical_check = guest_va >> 47
        if canonical_check not in (0, 0x1FFFF):
            return None

        cr3_page = guest_cr3 & _LONG_MODE_PAGE_FRAME_MASK
        va_page = guest_va & _PAGE_MASK

        if use_cache:
            with self._cache_lock:
                cached = self._gva_to_physical_page.get((cr3_page, va_page))
            if cached is not None:
                return cached | (guest_va & _PAGE_OFFSET_MASK)

        pml4e_data = self.read_guest_physical(cr3_page + ((guest_va >> 39) & 0x1FF) * 8, 8)
        if pml4e_data is None:
            return None
        pml4e = struct.unpack_from("<Q", pml4e_data)[0]
        if not (pml4e & _PRESENT_BIT):
            return None

        pdpt_base = pml4e & _LONG_MODE_PAGE_FRAME_MASK
        pdpte_data = self.read_guest_physical(pdpt_base + ((guest_va >> 30) & 0x1FF) * 8, 8)
        if pdpte_data is None:
            return None
        pdpte = struct.unpack_from("<Q", pdpte_data)[0]
        if not (pdpte & _PRESENT_BIT):
            return None

        if pdpte & _PAGE_SIZE_BIT:
            resolved = (pdpte & _LARGE_PAGE_1GB_MASK) | (guest_va & 0x3FFFFFFF)
            if use_cache:
                with self._cache_lock:
                    self._gva_to_physical_page[(cr3_page, va_page)] = resolved & _PAGE_MASK
            return resolved

        pd_base = pdpte & _LONG_MODE_PAGE_FRAME_MASK
        pde_data = self.read_guest_physical(pd_base + ((guest_va >> 21) & 0x1FF) * 8, 8)
        if pde_data is None:
            return None
        pde = struct.unpack_from("<Q", pde_data)[0]
        if not (pde & _PRESENT_BIT):
            return None

        if pde & _PAGE_SIZE_BIT:
            resolved = (pde & _LARGE_PAGE_2MB_MASK) | (guest_va & 0x1FFFFF)
            if use_cache:
                with self._cache_lock:
                    self._gva_to_physical_page[(cr3_page, va_page)] = resolved & _PAGE_MASK
            return resolved

        pt_base = pde & _LONG_MODE_PAGE_FRAME_MASK
        pte_data = self.read_guest_physical(pt_base + ((guest_va >> 12) & 0x1FF) * 8, 8)
        if pte_data is None:
            return None
        pte = struct.unpack_from("<Q", pte_data)[0]
        if not (pte & _PRESENT_BIT):
            return None

        resolved_final = (pte & _LONG_MODE_PAGE_FRAME_MASK) | (guest_va & _PAGE_OFFSET_MASK)
        if use_cache:
            with self._cache_lock:
                self._gva_to_physical_page[(cr3_page, va_page)] = resolved_final & _PAGE_MASK
        return resolved_final

    def read_guest_user_fresh(self, guest_cr3: int, guest_va: int, size: int) -> Optional[bytes]:
        """Read guest memory while forcing a fresh page-table walk.

        The normal read path memoizes (VA page -> host page). Long-lived guest
        mappings are stable, but BlueStacks can migrate/compact guest pages
        while the game streams assets (i.e. while the player is moving). When
        that happens the memo keeps serving the *old* host page, so a hot field
        like the camera view matrix silently freezes at its last value (ESP
        boxes stick to the screen). Re-walking for hot, tiny structs is cheap.
        """
        result = bytearray()
        copied = 0
        while copied < size:
            current_va = guest_va + copied
            page_offset = current_va & _PAGE_OFFSET_MASK
            chunk_size = min(size - copied, _PAGE_SIZE - page_offset)

            phys = self.translate_guest_va(guest_cr3, current_va, use_cache=False)
            if phys is None:
                return None

            host_base = self._map_physical_page_to_host_base(phys & _PAGE_MASK)
            if host_base is None:
                return None

            data = self._read_host_bytes(host_base + page_offset, chunk_size)
            if data is None:
                return None

            cr3_page = guest_cr3 & _LONG_MODE_PAGE_FRAME_MASK
            va_page = current_va & _PAGE_MASK
            with self._cache_lock:
                self._gva_to_physical_page[(cr3_page, va_page)] = phys & _PAGE_MASK
                self._gva_to_host_page[(cr3_page, va_page)] = host_base & _PAGE_MASK
                self._phys_to_host_page[phys & _PAGE_MASK] = host_base & _PAGE_MASK

            result.extend(data)
            copied += chunk_size
        return bytes(result)

    def read_guest_user(self, guest_cr3: int, guest_va: int, size: int) -> Optional[bytes]:
        result = bytearray()
        copied = 0
        while copied < size:
            current_va = guest_va + copied
            page_offset = current_va & _PAGE_OFFSET_MASK
            chunk_size = min(size - copied, _PAGE_SIZE - page_offset)
            current_va_page = current_va & _PAGE_MASK
            cr3_page = guest_cr3 & _LONG_MODE_PAGE_FRAME_MASK
            key = (cr3_page, current_va_page)

            with self._cache_lock:
                cached_host_page = self._gva_to_host_page.get(key)
            if cached_host_page is not None:
                data = self._read_host_bytes(cached_host_page + page_offset, chunk_size)
                if data is not None:
                    result.extend(data)
                    copied += chunk_size
                    continue
                with self._cache_lock:
                    self._gva_to_host_page.pop(key, None)

            read_ok = False
            for _attempt in range(2):
                phys = self.translate_guest_va(guest_cr3, current_va)
                if phys is None:
                    with self._cache_lock:
                        self._gva_to_physical_page.pop(key, None)
                    continue

                host = self._map_physical_to_host(phys)
                if host is None:
                    with self._cache_lock:
                        self._gva_to_physical_page.pop(key, None)
                        self._phys_to_host_page.pop(phys & _PAGE_MASK, None)
                    continue

                data = self._read_host_bytes(host, chunk_size)
                if data is not None:
                    with self._cache_lock:
                        self._gva_to_host_page[key] = host & _PAGE_MASK
                    result.extend(data)
                    read_ok = True
                    break

                with self._cache_lock:
                    self._gva_to_host_page.pop(key, None)
                    self._gva_to_physical_page.pop(key, None)
                    self._phys_to_host_page.pop(phys & _PAGE_MASK, None)

            if not read_ok:
                return None
            copied += chunk_size
        return bytes(result)

    def write_guest_user(self, guest_cr3: int, guest_va: int, data: bytes) -> bool:
        size = len(data)
        written = 0
        while written < size:
            current_va = guest_va + written
            page_offset = current_va & _PAGE_OFFSET_MASK
            chunk_size = min(size - written, _PAGE_SIZE - page_offset)

            phys = self.translate_guest_va(guest_cr3, current_va)
            if phys is None:
                return False

            host = self._map_physical_to_host(phys)
            if host is None:
                return False

            if not self._write_host_bytes(host, data[written:written + chunk_size]):
                return False
            written += chunk_size
        return True

    def read_mm_cr3(self, mm_va: int) -> Optional[int]:
        data = self.read_guest_kernel(mm_va + 0x50, 8)
        if data is None:
            return None
        pgd_kernel_va = struct.unpack_from("<Q", data)[0]
        if pgd_kernel_va == 0:
            return None
        return try_convert_kernel_virtual_to_physical(pgd_kernel_va)


# ---------------------------------------------------------------------------
# Memory facade - port of Memory.cs
# ---------------------------------------------------------------------------

class Memory:
    """Typed read/write facade (port of Memory.cs)."""

    def __init__(self, log: Optional[Callable[[str], None]] = None):
        self.engine = MemoryEngine(log)
        self.guest_cr3 = 0

    def setup(self) -> bool:
        return self.engine.initialize()

    def destroy(self) -> None:
        self.engine.shutdown()
        self.guest_cr3 = 0

    def set_cr3(self, value: int) -> None:
        self.guest_cr3 = value

    def clear_cr3(self) -> None:
        self.guest_cr3 = 0

    def clear_caches(self) -> None:
        self.engine.clear_caches()

    def clear_translations(self) -> None:
        self.engine.clear_translations()

    def set_fast_mode(self, enabled: bool) -> None:
        self.engine.set_fast_mode(enabled)

    @property
    def is_valid(self) -> bool:
        return self.engine.is_initialized

    @property
    def active_cr3(self) -> int:
        return self.guest_cr3

    # -- generic -------------------------------------------------------------

    def read(self, address: int, size: int) -> Optional[bytes]:
        if not self.is_valid:
            return None
        try:
            if self.guest_cr3 != 0 and not is_likely_guest_kernel_pointer(address):
                return self.engine.read_guest_user(self.guest_cr3, address, size)
            if is_likely_guest_kernel_pointer(address):
                return self.engine.read_guest_kernel(address, size)
            return self.engine.read_guest_physical(address, size)
        except Exception:
            return None

    def read_fresh(self, address: int, size: int) -> Optional[bytes]:
        """Like read(), but re-walks the guest page table for this address.

        Used for tiny hot structs (camera view matrix) where a stale cached
        translation would freeze the ESP while the player moves.
        """
        if not self.is_valid:
            return None
        try:
            if self.guest_cr3 != 0 and not is_likely_guest_kernel_pointer(address):
                return self.engine.read_guest_user_fresh(self.guest_cr3, address, size)
            return self.read(address, size)
        except Exception:
            return None

    def write(self, address: int, data: bytes) -> bool:
        if not self.is_valid:
            return False
        try:
            if self.guest_cr3 != 0 and not is_likely_guest_kernel_pointer(address):
                return self.engine.write_guest_user(self.guest_cr3, address, data)
            if is_likely_guest_kernel_pointer(address):
                return self.engine.write_guest_physical(address, data)
            return self.engine.write_guest_physical(address, data)
        except Exception:
            return False

    def read_kernel(self, address: int, size: int) -> Optional[bytes]:
        if not self.is_valid:
            return None
        try:
            return self.engine.read_guest_kernel(address, size)
        except Exception:
            return None

    def read_user(self, cr3: int, address: int, size: int) -> Optional[bytes]:
        if not self.is_valid:
            return None
        try:
            return self.engine.read_guest_user(cr3, address, size)
        except Exception:
            return None

    # -- typed helpers -------------------------------------------------------

    def read_u8(self, address: int) -> Optional[int]:
        data = self.read(address, 1)
        return data[0] if data else None

    def read_u16(self, address: int) -> Optional[int]:
        data = self.read(address, 2)
        return struct.unpack_from("<H", data)[0] if data else None

    def read_i16(self, address: int) -> Optional[int]:
        data = self.read(address, 2)
        return struct.unpack_from("<h", data)[0] if data else None

    def read_u32(self, address: int) -> Optional[int]:
        data = self.read(address, 4)
        return struct.unpack_from("<I", data)[0] if data else None

    def read_i32(self, address: int) -> Optional[int]:
        data = self.read(address, 4)
        return struct.unpack_from("<i", data)[0] if data else None

    def read_u64(self, address: int) -> Optional[int]:
        data = self.read(address, 8)
        return struct.unpack_from("<Q", data)[0] if data else None

    def read_f32(self, address: int) -> Optional[float]:
        data = self.read(address, 4)
        return struct.unpack_from("<f", data)[0] if data else None

    def write_u8(self, address: int, value: int) -> bool:
        return self.write(address, struct.pack("<B", value & 0xFF))

    def write_u16(self, address: int, value: int) -> bool:
        return self.write(address, struct.pack("<H", value & 0xFFFF))

    def write_u32(self, address: int, value: int) -> bool:
        return self.write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def write_i32(self, address: int, value: int) -> bool:
        return self.write(address, struct.pack("<i", value))

    def write_u64(self, address: int, value: int) -> bool:
        return self.write(address, struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))

    def write_f32(self, address: int, value: float) -> bool:
        return self.write(address, struct.pack("<f", float(value)))

    def read_string(self, address: int, size: int, unicode: bool = True) -> str:
        if size <= 0:
            return ""
        data = self.read(address, size)
        if not data:
            return ""
        try:
            text = data.decode("utf-16-le" if unicode else "latin-1", "ignore")
        except Exception:
            return ""
        return text.split("\x00", 1)[0]

    # -- kernel typed --------------------------------------------------------

    def read_kernel_u8(self, address: int) -> Optional[int]:
        data = self.read_kernel(address, 1)
        return data[0] if data else None

    def read_kernel_u32(self, address: int) -> Optional[int]:
        data = self.read_kernel(address, 4)
        return struct.unpack_from("<I", data)[0] if data else None

    def read_kernel_u64(self, address: int) -> Optional[int]:
        data = self.read_kernel(address, 8)
        return struct.unpack_from("<Q", data)[0] if data else None


# ---------------------------------------------------------------------------
# BlueStacks - port of BlueStacks.cs
# ---------------------------------------------------------------------------

class _TaskOffset:
    Mm = 0x4C0
    Tasks = 0x470
    Pid = 0x570
    Tgid = 0x574
    Comm = 0x720


_INIT_TASK = 0xFFFFFFFF822147C0

LIST_HEAD_NEXT = 0x0
LIST_HEAD_PREV = 0x8

MM_MMAP = 0x0
MM_ARG_START = 0x138
MM_ARG_END = 0x140

VMA_VM_START = 0x0
VMA_VM_NEXT = 0x10
VMA_VM_FILE = 0xA0

DENTRY_DNAME = 0x20
DENTRY_DNAME_LEN = 0x24
DENTRY_DNAME_NAME = 0x28
DENTRY_DINAME = 0x38

PACKAGE_NAMES = ("com.garena.game.ffi", "com.dts.freefireth", "com.dts.freefiremax")


class _Task:
    __slots__ = ("task_addr", "mm", "pid", "tgid", "comm", "cmdline")

    def __init__(self):
        self.task_addr = 0
        self.mm = 0
        self.pid = 0
        self.tgid = 0
        self.comm = ""
        self.cmdline = ""


def _sanitize_string(text: str) -> str:
    return "".join(ch for ch in text if 32 <= ord(ch) < 127)


class BlueStacks:
    """Port of BlueStacks.cs - finds the Free Fire task and module bases."""

    last_failure = ""

    def __init__(self, memory: Memory, log: Optional[Callable[[str], None]] = None):
        self.memory = memory
        self._log = log or (lambda msg: None)
        self.package_name = ""
        self.task = _Task()
        self.guest_cr3 = 0

    def _fail(self, reason: str) -> None:
        BlueStacks.last_failure = reason
        self._log("[bst] " + reason)

    # -- setup ---------------------------------------------------------------

    def setup(self, package: str, progress: Optional[Callable[[int, int, str], None]] = None) -> bool:
        self.package_name = package
        self.task = _Task()
        self.guest_cr3 = 0
        BlueStacks.last_failure = ""

        self.memory.destroy()

        for attempt in range(1, 21):
            if progress:
                progress(attempt, 20, package)
            self.memory.clear_cr3()

            if self.memory.setup():
                task = self._find_task(package)
                if task is not None:
                    self.task = task
                    cr3 = self.memory.engine.read_mm_cr3(task.mm)
                    if cr3:
                        self.guest_cr3 = cr3
                        self.memory.set_cr3(cr3)
                        return True
                    self._fail("Found the game task but could not read its page table (CR3). Game may still be loading - retry once it is in the lobby.")
                else:
                    self._fail("Connected to the BlueStacks VM, but '%s' was not found inside it. Is the game running in this instance?" % package)
            elif not MemoryEngine.last_failure:
                self._fail("Memory engine init failed for an unknown reason.")
            else:
                self._fail(MemoryEngine.last_failure)
                low = MemoryEngine.last_failure.lower()
                if "denied" in low or "administrator" in low:
                    # Elevation problems never fix themselves by retrying.
                    self.memory.destroy()
                    break

            self.memory.destroy()
            if attempt < 20:
                time.sleep(0.5)

        self.package_name = ""
        self.task = _Task()
        self.guest_cr3 = 0
        return False

    def setup_any(self, progress: Optional[Callable[[int, str], None]] = None) -> Optional[str]:
        last_reason = ""
        for package in PACKAGE_NAMES:
            def _cb(_attempt, _total, pkg=package):
                if progress:
                    progress(_attempt, pkg)

            if self.setup(package, lambda a, t, pkg: _cb(a, pkg)):
                return package
            last_reason = BlueStacks.last_failure
            if ("HD-Player" in last_reason or "BstkVMM" in last_reason
                    or "Administrator" in last_reason or "VM structures" in last_reason):
                BlueStacks.last_failure = last_reason
                return None
        BlueStacks.last_failure = last_reason
        return None

    def destroy(self) -> None:
        self.package_name = ""
        self.task = _Task()
        self.guest_cr3 = 0
        self.memory.destroy()

    # -- module base ---------------------------------------------------------

    def get_module_base(self, module_name: str, map_index: int = 1) -> int:
        if self.task.mm == 0 or self.guest_cr3 == 0 or map_index == 0:
            return 0

        current_vma = self.memory.read_kernel_u64(self.task.mm + MM_MMAP)
        if not current_vma:
            return 0

        seen = set()
        module_bases = []
        matched_file = 0

        while current_vma and len(seen) < 8192:
            if current_vma in seen:
                break
            seen.add(current_vma)

            vm_start = self.memory.read_kernel_u64(current_vma + VMA_VM_START)
            vm_next = self.memory.read_kernel_u64(current_vma + VMA_VM_NEXT)
            vm_file = self.memory.read_kernel_u64(current_vma + VMA_VM_FILE)
            if vm_start is None or vm_next is None or vm_file is None:
                break

            if vm_file != 0:
                file_name = self._read_file_name(vm_file)
                file_match = file_name.lower() == module_name.lower() if file_name else False

                if file_match and matched_file == 0:
                    matched_file = vm_file

                if file_match or (matched_file != 0 and vm_file == matched_file):
                    if vm_start not in module_bases:
                        module_bases.append(vm_start)

            if vm_next == 0 or vm_next == current_vma:
                break
            current_vma = vm_next

        if len(module_bases) < map_index:
            return 0
        return module_bases[map_index - 1]

    # -- task walking --------------------------------------------------------

    def _read_task(self, task_addr: int) -> Optional[_Task]:
        task = _Task()
        task.task_addr = task_addr

        task.mm = self.memory.read_kernel_u64(task_addr + _TaskOffset.Mm)
        task.pid = self.memory.read_kernel_u32(task_addr + _TaskOffset.Pid)
        task.tgid = self.memory.read_kernel_u32(task_addr + _TaskOffset.Tgid)
        if task.mm is None or task.pid is None or task.tgid is None:
            return None

        task.comm = self._read_task_comm(task_addr)
        if not task.comm:
            return None

        if task.mm != 0 and is_likely_guest_kernel_pointer(task.mm):
            task_cr3 = self.memory.engine.read_mm_cr3(task.mm)
            if task_cr3:
                task.cmdline = self._read_task_cmdline(task.mm, task_cr3)
        return task

    def _read_task_comm(self, task_addr: int) -> str:
        data = self.memory.read_kernel(task_addr + _TaskOffset.Comm, 16)
        if not data:
            return ""
        return _sanitize_string(data.decode("latin-1", "ignore").rstrip("\x00"))

    def _read_task_cmdline(self, mm: int, guest_cr3: int) -> str:
        arg_start = self.memory.read_kernel_u64(mm + MM_ARG_START)
        arg_end = self.memory.read_kernel_u64(mm + MM_ARG_END)
        if not arg_start or not arg_end or arg_end <= arg_start:
            return ""
        span = arg_end - arg_start
        if span > 0x2000:
            span = 0x2000
        if span == 0:
            return ""
        data = self.memory.read_user(guest_cr3, arg_start, int(span))
        if not data:
            return ""
        return _sanitize_string(data.decode("latin-1", "ignore").rstrip("\x00"))

    def _read_kernel_string(self, address: int, max_length: int) -> str:
        if address == 0 or max_length == 0:
            return ""
        data = self.memory.read_kernel(address, max_length)
        if not data:
            return ""
        return _sanitize_string(data.decode("utf-8", "ignore").rstrip("\x00"))

    def _read_dentry_name(self, dentry: int) -> str:
        buf = self.memory.read_kernel(dentry, 128)
        if not buf:
            return ""

        d_name_len = struct.unpack_from("<I", buf, DENTRY_DNAME_LEN)[0]
        d_name_name = struct.unpack_from("<Q", buf, DENTRY_DNAME_NAME)[0]

        if 0 < d_name_len <= 256 and d_name_name != 0:
            name = self._read_kernel_string(d_name_name, d_name_len)
            if name:
                return name

        iname = buf[DENTRY_DINAME:DENTRY_DINAME + 32]
        return _sanitize_string(iname.decode("latin-1", "ignore").rstrip("\x00"))

    def _read_file_name(self, file_addr: int) -> str:
        buf = self.memory.read_kernel(file_addr, 128)
        if not buf:
            return ""
        dentry = struct.unpack_from("<Q", buf, 0x18)[0]
        if dentry == 0:
            return ""
        return self._read_dentry_name(dentry)

    def _score_task(self, task: _Task) -> int:
        if not self.package_name:
            return 1
        score = 0
        pkg = self.package_name.lower()

        if task.cmdline:
            cmd = task.cmdline.lower()
            if cmd == pkg:
                score = max(score, 100)
            elif pkg in cmd:
                score = max(score, 90)
            elif cmd in pkg:
                score = max(score, 80)

        comm = (task.comm or "").lower()
        if comm == pkg:
            score = max(score, 60)
        elif comm and (pkg.startswith(comm) or comm.startswith(pkg[:10]) or any(part in comm for part in ["game.ffi", "garena", "freefire"])):
            score = max(score, 50)

        if task.pid == task.tgid and score > 0:
            score += 1
        return score

    def _find_task(self, package_name: str) -> Optional[_Task]:
        init_tasks_head = _INIT_TASK + _TaskOffset.Tasks

        init_tasks_next = self.memory.read_kernel_u64(init_tasks_head + LIST_HEAD_NEXT)
        init_tasks_prev = self.memory.read_kernel_u64(init_tasks_head + LIST_HEAD_PREV)
        if init_tasks_next is None or init_tasks_prev is None:
            return None

        seen_heads = []
        best_task = None
        best_score = 0

        def walk(start_head: int, reverse: bool):
            nonlocal best_task, best_score
            current_head = start_head
            while current_head and current_head != init_tasks_head and len(seen_heads) < 4096:
                if current_head < _TaskOffset.Tasks:
                    break
                if current_head in seen_heads:
                    break
                seen_heads.append(current_head)

                task_addr = current_head - _TaskOffset.Tasks
                task = self._read_task(task_addr)
                if task is not None and task.mm != 0 and is_likely_guest_kernel_pointer(task.mm):
                    score = self._score_task(task)
                    if score > best_score:
                        best_score = score
                        best_task = task

                link_offset = LIST_HEAD_PREV if reverse else LIST_HEAD_NEXT
                next_head = self.memory.read_kernel_u64(current_head + link_offset)
                if not next_head or next_head == current_head:
                    break
                current_head = next_head

        walk(init_tasks_next, False)
        walk(init_tasks_prev, True)

        if best_score == 0 or best_task is None or best_task.mm == 0:
            return None
        return best_task
