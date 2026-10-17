"""Root supervisor uploaded to each disposable Linux sandbox.

Never imports candidate code. No local execution entry point is exposed.
Requires Linux Landlock ABI >=3 and libseccomp; unsupported images fail closed.
"""

import base64
import ctypes
import ctypes.util
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import selectors
import signal
import stat
import subprocess
import sys
import time


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def environment():
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return "unavailable"
    cpu = next((line.split(":", 1)[1].strip()
                for line in read("/proc/cpuinfo").splitlines()
                if line.startswith(("model name", "Hardware"))), platform.machine())
    return {"machine": platform.machine(), "kernel": platform.release(),
            "python": platform.python_version(), "cpu_model": cpu,
            "cpu_max": read("/sys/fs/cgroup/cpu.max"),
            "memory_max": read("/sys/fs/cgroup/memory.max")}


def landlock(libc, public, scratch):
    # Linux syscall numbers shared by x86_64 and aarch64.
    if platform.machine() not in ("x86_64", "aarch64"):
        raise RuntimeError("unsupported Landlock architecture")
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError("Landlock ABI >=3 required")
    rights = (1 << 15) - 1  # includes REFER and TRUNCATE, requires ABI 3
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    ruleset = Ruleset(rights)
    descriptor = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Landlock ruleset")
    read_execute = (1 << 0) | (1 << 2) | (1 << 3)
    paths = [("/usr", read_execute), ("/lib", read_execute),
             ("/lib64", read_execute), ("/bin", read_execute),
             ("/sbin", read_execute), ("/etc/ld.so.cache", 1 << 2),
             ("/dev/null", (1 << 1) | (1 << 2)),
             ("/dev/urandom", 1 << 2), ("/dev/random", 1 << 2),
             (str(public), read_execute), (str(scratch), rights)]
    try:
        for path, allowed in paths:
            if not os.path.exists(path):
                continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    allowed &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
                rule = Beneath(allowed, fd)
                if libc.syscall(445, descriptor, 1, ctypes.byref(rule), 0) < 0:
                    raise OSError(ctypes.get_errno(), "Landlock path rule")
            finally:
                os.close(fd)
        if libc.syscall(446, descriptor, 0) < 0:
            raise OSError(ctypes.get_errno(), "Landlock restrict")
    finally:
        os.close(descriptor)
    return abi


DENIED_SYSCALLS = (
    "socket", "socketpair", "connect", "accept", "accept4", "bind", "listen",
    "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
    "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_open",
    "pidfd_getfd", "pidfd_send_signal", "kill", "tkill", "tgkill",
    "fork", "vfork", "clone", "clone3", "setsid", "setpgid",
    "unshare", "setns", "mount", "umount2", "pivot_root", "chroot",
    "bpf", "perf_event_open", "userfaultfd", "io_uring_setup",
    "open_by_handle_at", "init_module", "finit_module", "delete_module",
    "reboot", "kexec_load", "keyctl", "add_key", "request_key",
    "shmget", "shmat", "shmctl", "shmdt", "msgget", "msgsnd", "msgrcv", "msgctl",
    "semget", "semop", "semtimedop", "semctl", "mq_open", "mq_unlink",
    "mq_timedsend", "mq_timedreceive", "mq_notify", "mq_getsetattr",
)


def restrict_child(public, scratch, timeout):
    import resource
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    # All candidates are single-process, single-thread stdlib Python for v1.
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(timeout) + 1, math.ceil(timeout) + 1))
    os.setgroups([])
    os.setresgid(65534, 65534, 65534)
    os.setresuid(65534, 65534, 65534)
    os.umask(0o077)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "no_new_privs")
    landlock(libc, public, scratch)
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("seccomp initialization failed")
    try:
        for syscall in DENIED_SYSCALLS:
            number = seccomp.seccomp_syscall_resolve_name(syscall.encode())
            if number >= 0 and seccomp.seccomp_rule_add(
                    context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise RuntimeError("seccomp rule failed")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("seccomp load failed")
    finally:
        seccomp.seccomp_release(context)


def scratch_usage(path):
    """Bounded, no-symlink directory traversal; never follow candidate paths."""
    total = count = 0
    pending = [os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        while pending:
            fd = pending.pop()
            try:
                for name in os.listdir(fd):
                    count += 1
                    if count > 1024:
                        return total, count
                    try:
                        item = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        total += item.st_size
                        if stat.S_ISDIR(item.st_mode):
                            pending.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY |
                                                   os.O_NOFOLLOW, dir_fd=fd))
                    except (FileNotFoundError, NotADirectoryError):
                        continue
            finally:
                os.close(fd)
        return total, count
    finally:
        for fd in pending:
            os.close(fd)


def supervise(request, root):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("supervisor requires a root Linux execution sandbox")
    public, scratch, private = root / "public", root / "scratch", root / "private"
    timeout = request["timeout_seconds"]
    if type(timeout) not in (int, float) or not 0.05 <= timeout <= 15:
        raise ValueError("invalid process deadline")
    source = (public / "solution.py").read_bytes()
    input_bytes = (private / "input.json").read_bytes()
    if len(source) > 262_144 or len(input_bytes) > 16 << 20:
        raise ValueError("source/input size limit")
    api_owner = root.stat()
    if api_owner.st_uid == 65534 or api_owner.st_gid == 65534:
        raise RuntimeError("API and candidate identities must differ")
    os.chown(root, 0, api_owner.st_gid)
    os.chmod(root, 0o755)
    os.chown(private, 0, api_owner.st_gid)
    os.chmod(private, 0o750)
    for file in private.iterdir():
        os.chown(file, 0, api_owner.st_gid)
        os.chmod(file, 0o640)
    os.chown(public, 0, 0)
    os.chmod(public, 0o555)
    for file in public.iterdir():
        os.chown(file, 0, 0)
        os.chmod(file, 0o444)
    os.chown(scratch, 65534, 65534)
    os.chmod(scratch, 0o700)
    # Detect support before launching; child independently enforces the boundary.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError("Landlock ABI >=3 unavailable")
    env = environment()
    output, errors, reason = bytearray(), bytearray(), ""
    start = time.monotonic()
    with (private / "input.json").open("rb") as stdin:
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", str(public / "driver.py"),
             str(public / "solution.py")],
            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=scratch, close_fds=True, start_new_session=True,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                 "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"},
            preexec_fn=lambda: restrict_child(public, scratch, timeout))
        selector = selectors.DefaultSelector()
        for stream, bucket in ((child.stdout, output), (child.stderr, errors)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, bucket)
        try:
            while selector.get_map():
                if time.monotonic() - start >= timeout:
                    reason = "process_timeout"
                size, count = scratch_usage(scratch)
                if size > 64 << 20 or count > 1024:
                    reason = "scratch_limit"
                if len(output) + len(errors) > 1 << 20:
                    reason = "output_limit"
                if reason:
                    os.killpg(child.pid, signal.SIGKILL)
                    break
                for key, _ in selector.select(0.01):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if data:
                        key.data.extend(data)
                    else:
                        selector.unregister(key.fileobj)
            try:
                child.wait(timeout=max(0.001, timeout - (time.monotonic() - start)))
            except subprocess.TimeoutExpired:
                reason = "process_timeout"
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            selector.close()
            child.stdout.close()
            child.stderr.close()
    duration = time.monotonic() - start
    if len(output) + len(errors) > 1 << 20:
        reason = "output_limit"
    # Clear scratch content not needed; the entire sandbox is deleted by controller.
    return {"schema_version": 1, "trial_id": request["trial_id"],
            "source_sha256": sha(source), "input_sha256": sha(input_bytes),
            "runner_sha256": sha(Path(__file__).read_bytes()),
            "driver_sha256": sha((public / "driver.py").read_bytes()),
            "environment": env, "duration_seconds": duration,
            "exit_code": child.returncode, "reason": reason,
            "stdout_b64": base64.b64encode(output[:1 << 20]).decode(),
            "stderr_b64": base64.b64encode(errors[:1 << 20]).decode(),
            "security": {"candidate_uid": 65534, "landlock_abi": abi,
                         "seccomp": True, "single_process": True}}


def main():
    root = Path(sys.argv[1]).resolve()
    request = json.loads((root / "private" / "request.json").read_bytes())
    proof = supervise(request, root)
    path = root / "private" / "proof.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchown(fd, 0, (root / "private").stat().st_gid)
    os.fchmod(fd, 0o640)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(proof))


if __name__ == "__main__":
    main()
