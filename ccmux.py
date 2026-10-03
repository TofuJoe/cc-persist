#!/usr/bin/env python3
"""
ccmux - a crash-resilient terminal session manager that keeps full-screen-capable
TUIs (notably Claude Code) in their CLASSIC renderer, so the host terminal's native
scrollback (and e.g. a VTE touch-scroll patch) keeps working.

Unlike abduco/dtach, which relay program<->client bytes over a socket (a pattern Claude
Code reacts to by switching into its alternate-screen "fullscreen" renderer), ccmux
keeps the program on a real pty and passes that pty's MASTER fd to the attached foreground
client via SCM_RIGHTS. The client drives the pty directly - exactly like a plain in-process
pty wrapper - so the program sees an ordinary interactive terminal and stays classic. The
detached server merely owns the pty + child so the session survives the client (or the
whole terminal) dying, and can be reattached later.

Commands:
  ccmux run  <name> [cmd args...]   attach to session <name>, or create it running <cmd>
  ccmux list                        list sessions, TSV: state<TAB>created<TAB>name

Keys:
  Ctrl-\\   detach (leave the program running); override with CCMUX_DETACH=\\xNN

Env:
  CCMUX_DIR     session socket dir (default ~/.ccmux)
  CCMUX_DETACH  detach key byte, e.g. \\x1c (default Ctrl-\\)
  CCMUX_BUF     max replay-buffer bytes shown on reattach (default 1048576)
"""
import os, sys, pty, socket, select, struct, fcntl, termios, signal, time, tty, stat

DIR = os.environ.get("CCMUX_DIR") or os.path.join(os.path.expanduser("~"), ".ccmux")
_dk = os.environ.get("CCMUX_DETACH")
if _dk:
    try: DETACH = bytes([int(_dk.replace("\\x", ""), 16)])
    except ValueError: DETACH = b"\x1c"
else:
    DETACH = b"\x1c"                       # Ctrl-\
try: MAXBUF = max(0, int(os.environ.get("CCMUX_BUF") or (1 << 20)))
except ValueError: MAXBUF = 1 << 20


def _sock(name): return os.path.join(DIR, name)

def _ensure_dir():
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    try: os.chmod(DIR, 0o700)
    except OSError: pass

def getsz(fd):
    try:    return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except Exception: return struct.pack("HHHH", 24, 80, 0, 0)

def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        try: chunk = sock.recv(n - len(buf))
        except OSError: break
        if not chunk: break
        buf += chunk
    return buf


# --------------------------------------------------------------------------- server
def _server(lsock, cmd, initsz, path):
    os.setsid()                               # new session...
    if os.fork() != 0:                        # ...double-fork -> reparent to init, no ctty
        os._exit(0)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    master, slave = pty.openpty()
    try: fcntl.ioctl(slave, termios.TIOCSWINSZ, initsz)   # size BEFORE exec (no 0x0 race)
    except Exception: pass

    pid = os.fork()
    if pid == 0:                              # ---- the program ----
        os.setsid()
        try: fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        except Exception: pass
        os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
        if master > 2: os.close(master)
        if slave > 2:  os.close(slave)
        try: os.execvp(cmd[0], cmd)
        except Exception as e:
            os.write(2, ("ccmux: exec failed: %s\r\n" % e).encode()); os._exit(127)
    os.close(slave)

    created = int(time.time())
    lsock.listen(4); lsock.setblocking(False)
    os.set_blocking(master, False)
    conn = None                               # the one attached data client
    ever = False                              # has anyone attached yet?
    ring = bytearray()                        # output produced while detached (replay)

    def done():
        try: return os.waitpid(pid, os.WNOHANG)[0] == pid
        except ChildProcessError: return True

    while True:
        if done():
            break
        watch = [lsock]
        if conn is not None:
            watch.append(conn)
        elif ever:
            watch.append(master)              # drain+buffer only while detached
        try: r, _, _ = select.select(watch, [], [], 0.3)
        except (OSError, ValueError): r = []

        if master in r:                       # absorb output so program never blocks
            try:
                d = os.read(master, 65536)
                if d:
                    ring.extend(d)
                    if len(ring) > MAXBUF: del ring[:len(ring) - MAXBUF]
            except OSError: pass

        if conn is not None and conn in r:    # client sends nothing; readable == it left
            try: d = conn.recv(1)
            except OSError: d = b""
            if not d:
                try: conn.close()
                except OSError: pass
                conn = None

        if lsock in r:
            try:
                c, _ = lsock.accept(); c.settimeout(2.0); cmd1 = c.recv(1)
            except OSError:
                cmd1 = b""
                c = None
            if c is not None:
                if cmd1 == b"S":                              # status probe (for `list`)
                    try: c.sendall(b"ATTACHED %d CREATED %d\n" %
                                   (1 if conn is not None else 0, created))
                    except OSError: pass
                    try: c.close()
                    except OSError: pass
                elif cmd1 == b"A":                            # attach request
                    if conn is not None:                     # single-client: refuse
                        try: c.sendall(b"B")
                        except OSError: pass
                        try: c.close()
                        except OSError: pass
                    else:
                        try:
                            c.settimeout(None)
                            socket.send_fds(c, [b"A"], [master])     # payload + master fd
                            blob = bytes(ring)
                            c.sendall(struct.pack(">I", len(blob)))  # replay length...
                            if blob: c.sendall(blob)                 # ...then replay body
                            ring.clear()
                            try: os.kill(pid, signal.SIGWINCH)       # nudge a repaint
                            except OSError: pass
                            conn = c
                        except OSError:
                            try: c.close()
                            except OSError: pass
                            conn = None
                else:
                    try: c.close()
                    except OSError: pass

    try: os.close(master)
    except OSError: pass
    try: os.unlink(path)
    except OSError: pass
    os._exit(0)


# --------------------------------------------------------------------------- client
def _client(c):
    try: c.sendall(b"A")
    except OSError: sys.exit("ccmux: attach failed")
    try: msg, fds, _, _ = socket.recv_fds(c, 1, 1)
    except OSError: sys.exit("ccmux: attach failed")
    if not fds:
        if msg == b"B": sys.exit("ccmux: session is already attached elsewhere")
        sys.exit("ccmux: attach refused")
    master = fds[0]

    hdr = _recvn(c, 4)                          # replay: length-prefixed output-so-far
    if len(hdr) == 4:
        (ln,) = struct.unpack(">I", hdr)
        blob = _recvn(c, ln) if ln else b""
        if blob:
            try: os.write(1, blob)
            except OSError: pass

    try: fcntl.ioctl(master, termios.TIOCSWINSZ, getsz(0))
    except Exception: pass
    old = None
    try: old = termios.tcgetattr(0); tty.setraw(0)
    except Exception: pass

    def winch(*_):
        try: fcntl.ioctl(master, termios.TIOCSWINSZ, getsz(0))
        except Exception: pass
    signal.signal(signal.SIGWINCH, winch)
    cfd = c.fileno()
    try:
        while True:
            try: r, _, _ = select.select([0, master, cfd], [], [])
            except (OSError, ValueError): break
            if cfd in r:                        # server closed -> program exited/gone
                try:
                    if not c.recv(1): break
                except OSError: break
            if 0 in r:
                try: d = os.read(0, 65536)
                except OSError: d = b""
                if not d: break
                if DETACH in d:                 # detach: forward anything before the key, leave
                    pre = d.split(DETACH, 1)[0]
                    if pre:
                        try: os.write(master, pre)
                        except OSError: pass
                    break
                try: os.write(master, d)
                except OSError: break
            if master in r:
                try: d = os.read(master, 65536)
                except OSError: d = b""
                if not d: break                 # program exited
                try: os.write(1, d)
                except OSError: break
    finally:
        try: signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        except Exception: pass
        if old is not None:
            try: termios.tcsetattr(0, termios.TCSADRAIN, old)
            except Exception: pass
        try: c.close()
        except OSError: pass


# --------------------------------------------------------------------------- commands
def _run(name, cmd):
    _ensure_dir()
    path = _sock(name)
    if os.path.lexists(path):                   # live socket, stale socket, or dangling symlink
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try: s.connect(path); _client(s); return
        except OSError:
            try: os.unlink(path)                # stale socket or dead (e.g. migrated) symlink
            except OSError: pass
    if not cmd:
        sys.exit("ccmux: no session %r and no command to create it" % name)

    lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try: lsock.bind(path)
    except OSError:                             # lost a create race -> attach
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try: s.connect(path); _client(s); return
        except OSError: sys.exit("ccmux: cannot create session %r" % name)
    try: os.chmod(path, 0o600)
    except OSError: pass

    initsz = getsz(0)
    pid = os.fork()
    if pid == 0:
        _server(lsock, cmd, initsz, path)       # never returns
    lsock.close()
    try: os.waitpid(pid, 0)                     # reap the double-fork intermediate
    except OSError: pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(200):
        try: s.connect(path); break
        except OSError: time.sleep(0.02)
    else:
        sys.exit("ccmux: server did not start")
    _client(s)


def _list():
    _ensure_dir()
    try: names = sorted(os.listdir(DIR))
    except OSError: names = []
    rows = []
    for name in names:
        path = _sock(name)
        try:
            if not stat.S_ISSOCK(os.stat(path).st_mode): continue
        except OSError:
            if os.path.islink(path):          # dead symlink (e.g. a migrated session ended)
                try: os.unlink(path)
                except OSError: pass
            continue
        attached, created, alive = 0, 0, False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0); s.connect(path); s.sendall(b"S")
            data = s.recv(256); s.close(); alive = True
            p = data.split()
            if len(p) >= 4: attached, created = int(p[1]), int(p[3])
        except OSError:
            alive = False
        if not alive:                           # dead/stale -> clean up, skip
            try: os.unlink(path)
            except OSError: pass
            continue
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created)) if created else ""
        rows.append("%s\t%s\t%s" % ("attached" if attached else "detached", when, name))
    if rows: sys.stdout.write("\n".join(rows) + "\n")


def main(argv):
    if len(argv) >= 2 and argv[1] == "list": _list(); return
    if len(argv) >= 3 and argv[1] == "run":   _run(argv[2], argv[3:]); return
    sys.stderr.write(__doc__); sys.exit(2)


if __name__ == "__main__":
    main(sys.argv)
