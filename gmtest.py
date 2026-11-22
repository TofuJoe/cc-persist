#!/usr/bin/env python3
import os, pty, select, time, signal, struct, fcntl, termios, re, subprocess
SZ = struct.pack('HHHH', 40, 120, 0, 0)
NAME = "sess-test"

def spawn(args):
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("python3", ["python3", "ccmux.py"] + args)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, SZ)
    return pid, fd

def read_for(fd, secs):
    out = bytearray(); t = time.time() + secs
    while time.time() < t:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try: d = os.read(fd, 65536)
            except OSError: break
            if not d: break
            out.extend(d)
    return bytes(out)

def listing():
    return subprocess.run(["python3", "ccmux.py", "list"],
                          capture_output=True, text=True).stdout.strip()

prog = "i=0; while true; do echo tick $i; i=$((i+1)); sleep 0.3; done"

# 1. create + output
pid, fd = spawn(["run", NAME, "sh", "-c", prog])
o = read_for(fd, 2.0)
ticks = [int(x) for x in re.findall(rb'tick (\d+)', o)]
print("1. create+output          :", "PASS" if ticks else "FAIL", "(%d ticks)" % len(ticks))
print("   list ->", listing())

# 2. detach via Ctrl-\
os.write(fd, b'\x1c'); time.sleep(0.6); read_for(fd, 0.3)
try: os.kill(pid, signal.SIGKILL)
except OSError: pass
l = listing()
print("2. detach (Ctrl-\\)         :", "PASS" if "detached" in l else "FAIL", "->", l)

# 3. let it run while detached, then reattach -> replay should show ticks made while away
time.sleep(1.5)
pid3, fd3 = spawn(["run", NAME])
o3 = read_for(fd3, 1.5)
t3 = [int(x) for x in re.findall(rb'tick (\d+)', o3)]
last = ticks[-1] if ticks else -1
print("3. reattach + replay      :", "PASS" if (t3 and max(t3) > last) else "FAIL",
      "(before=%d, now up to=%s)" % (last, max(t3) if t3 else None))

# 4. CRASH the attached client (SIGKILL) -> program must survive
os.kill(pid3, signal.SIGKILL); os.close(fd3); time.sleep(0.8)
l4 = listing()
print("4. client crash survival  :", "PASS" if "detached" in l4 else "FAIL", "->", l4)

# 5. reattach after crash
pid5, fd5 = spawn(["run", NAME])
o5 = read_for(fd5, 1.0)
print("5. reattach after crash   :", "PASS" if re.search(rb'tick', o5) else "FAIL")

# 6. program exits -> session tears down, socket removed
subprocess.run(["pkill", "-f", "while true; do echo tick"])
os.kill(pid5, signal.SIGKILL); os.close(fd5); time.sleep(0.8)
l6 = listing()
print("6. teardown on prog exit  :", "PASS" if l6 == "" else "FAIL", "->", repr(l6))

# 7. Ctrl-Z suspends the CLIENT as a real shell job (program keeps running), `fg` reattaches
N7 = "sess-test-tstp"
bpid, bfd = pty.fork()
if bpid == 0:
    os.environ["PS1"] = "PROMPT$ "
    os.execvp("bash", ["bash", "--norc", "--noprofile", "-i"])
fcntl.ioctl(bfd, termios.TIOCSWINSZ, SZ); read_for(bfd, 0.5)
os.write(bfd, ("python3 ccmux.py run %s sh -c '%s'\r" % (N7, prog.replace("tick", "tock"))).encode())
o7a = read_for(bfd, 1.5)
os.write(bfd, b"\x1a"); o7b = read_for(bfd, 1.0)
l7 = listing()
stopped = b"Stopped" in o7b and "detached\t" in l7 and N7 in l7
time.sleep(1.0)                                   # program keeps ticking while we're stopped
os.write(bfd, b"fg\r"); o7c = read_for(bfd, 1.5)
before = max([int(x) for x in re.findall(rb'tock (\d+)', o7a)] or [-1])
after = [int(x) for x in re.findall(rb'tock (\d+)', o7c)]
l7c = listing()
ok = stopped and after and max(after) > before + 3 and ("attached\t" in l7c)
print("7. Ctrl-Z suspend + fg    :", "PASS" if ok else "FAIL",
      "(stopped=%s, before=%d, after=%s)" % (stopped, before, max(after) if after else None))
subprocess.run(["pkill", "-f", "while true; do echo tock"])
time.sleep(0.6)
try: os.kill(bpid, signal.SIGKILL)
except OSError: pass
print("   list after cleanup ->", repr(listing()))
