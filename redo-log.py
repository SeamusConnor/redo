#!/usr/bin/env python
import errno, fcntl, os, re, struct, sys, termios, time
import options

optspec = """
redo-log [options...] [targets...]
--
r,recursive     show build logs for dependencies too
u,unchanged     show lines for dependencies not needing to be rebuilt
f,follow        keep watching for more lines to be appended (like tail -f)
no-details      only show 'redo' recursion trace, not build output
no-colorize     don't colorize 'redo' log messages
no-status       don't display build summary line in --follow
raw-logs        don't format logs, just send raw output straight to stdout
ack-fd=         (internal use only) print REDO-OK to this fd upon starting
"""
o = options.Options(optspec)
(opt, flags, extra) = o.parse(sys.argv[1:])
targets = extra

import vars_init
vars_init.init(list(targets))

import vars, logs, state

already = set()
queue = []
depth = []
total_lines = 0
status = None


# regexp for matching "redo" lines in the log, which we use for recursion.
# format:
#            redo  path/to/target which might have spaces
#            redo  [unchanged] path/to/target which might have spaces
#            redo  path/to/target which might have spaces (comment)
REDO_LINE_RE = re.compile(r'^@@REDO:([^@]+)@@ (.*)\n$')


def _atoi(s):
    try:
        return int(s)
    except TypeError:
        return 0


def _tty_width():
    s = struct.pack("HHHH", 0, 0, 0, 0)
    try:
        import fcntl, termios
        s = fcntl.ioctl(sys.stderr.fileno(), termios.TIOCGWINSZ, s)
    except (IOError, ImportError):
        return _atoi(os.environ.get('WIDTH')) or 70
    (ysize,xsize,ypix,xpix) = struct.unpack('HHHH', s)
    return xsize or 70


def is_locked(fid):
    return (fid is not None) and not state.Lock(fid=fid).trylock()


def _fix_depth():
    vars.DEPTH = (len(depth) - 1) * '  '


def catlog(t):
    global total_lines, status
    if t in already:
        return
    depth.append(t)
    _fix_depth()
    already.add(t)
    if t == '-':
        f = sys.stdin
        fid = None
        logname = None
    else:
        try:
            sf = state.File(name=t, allow_add=False)
        except KeyError:
            sys.stderr.write('redo-log: %r: not known to redo.\n' % (t,))
            sys.exit(24)
        fid = sf.id
        del sf
        state.rollback()
        logname = state.logname(fid)
        f = None
    delay = 0.01
    was_locked = is_locked(fid)
    line_head = ''
    width = _tty_width()
    while 1:
        if not f:
            try:
                f = open(logname)
            except IOError, e:
                if e.errno == errno.ENOENT:
                    # ignore files without logs
                    pass
                else:
                    raise
        if f:
            # Note: normally includes trailing \n.
            # In 'follow' mode, might get a line with no trailing \n
            # (eg. when ./configure is halfway through a test), which we
            # deal with below.
            line = f.readline()
        else:
            line = None
        if not line and (not opt.follow or not was_locked):
            # file not locked, and no new lines: done
            break
        if not line:
            was_locked = is_locked(fid)
            if opt.follow:
                if opt.status:
                    width = _tty_width()
                    head = 'redo %s ' % ('{:,}'.format(total_lines))
                    tail = ''
                    for n in reversed(depth):
                        remain = width - len(head) - len(tail)
                        # always leave room for a final '... ' prefix
                        if remain < len(n) + 4 + 1 or remain <= 4:
                            if len(n) < 6 or remain < 6 + 1 + 4:
                                tail = '... %s' % tail
                            else:
                                start = len(n) - (remain - 3 - 1)
                                tail = '...%s %s' % (n[start:], tail)
                            break
                        elif n != '-':
                            tail = n + ' ' + tail
                    status = head + tail
                    if len(status) > width:
                        sys.stderr.write('\nOVERSIZE STATUS (%d):\n%r\n' %
                            (len(status), status))
                    assert(len(status) <= width)
                    sys.stdout.flush()
                    sys.stderr.write('\r%-*.*s\r' % (width, width, status))
                time.sleep(min(delay, 1.0))
                delay += 0.01
            continue
        total_lines += 1
        delay = 0.01
        if not line.endswith('\n'):
            line_head += line
            continue
        if line_head:
            line = line_head + line
            line_head = ''
        if status:
            sys.stdout.flush()
            sys.stderr.write('\r%-*.*s\r' % (width, width, ''))
            status = None
        g = re.match(REDO_LINE_RE, line)
        if g:
            # FIXME: print prefix if @@REDO is not at start of line.
            #   logs.PrettyLog does it, but only if we actually call .write().
            words, text = g.groups()
            kind, pid, when = words.split(':')[0:3]
            if kind == 'unchanged':
                if opt.unchanged:
                    if text not in already:
                        logs.write(line.rstrip())
                    if opt.recursive:
                        catlog(text)
            elif kind in ('do', 'waiting'):
                logs.write(line.rstrip())
                if opt.recursive:
                    assert text
                    catlog(text)
            else:
                logs.write(line.rstrip())
        else:
            if opt.details:
                logs.write(line.rstrip())
    if status:
        sys.stdout.flush()
        sys.stderr.write('\r%-*.*s\r' % (width, width, ''))
        status = None
    if line_head:
        # partial line never got terminated
        print line_head
    assert(depth[-1] == t)
    depth.pop(-1)
    _fix_depth()

try:
    if not targets:
        sys.stderr.write('redo-log: give at least one target; maybe "all"?\n')
        sys.exit(1)
    if opt.status < 2 and not os.isatty(2):
        opt.status = False
    if opt.raw_logs:
        logs.setup(file=sys.stdout, pretty=False)
    else:
        logs.setup(file=sys.stdout, pretty=True)
    if opt.ack_fd:
        # Write back to owner, to let them know we started up okay and
        # will be able to see their error output, so it's okay to close
        # their old stderr.
        ack_fd = int(opt.ack_fd)
        assert(ack_fd > 2)
        if os.write(ack_fd, 'REDO-OK\n') != 8:
            raise Exception('write to ack_fd returned wrong length')
        os.close(ack_fd)
    queue += targets
    while queue:
        t = queue.pop(0)
        if t != '-':
            logs.meta('do', t)
        catlog(t)
except KeyboardInterrupt:
    sys.exit(200)
except IOError, e:
    if e.errno == errno.EPIPE:
        pass
    else:
        raise
