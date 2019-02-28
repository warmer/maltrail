#!/usr/bin/env python

"""
Copyright (c) 2014-2019 Miroslav Stampar (@stamparm)
See the file 'LICENSE' for copying permission
"""

import os
import signal
import socket
import SocketServer
import sys
import threading
import time
import traceback

from core.common import check_whitelisted
from core.common import check_sudo
from core.enums import TRAIL
from core.settings import CEF_FORMAT
from core.settings import config
from core.settings import CONDENSE_ON_INFO_KEYWORDS
from core.settings import CONDENSED_EVENTS_FLUSH_PERIOD
from core.settings import DEFAULT_ERROR_LOG_PERMISSIONS
from core.settings import DEFAULT_EVENT_LOG_PERMISSIONS
from core.settings import HOSTNAME
from core.settings import NAME
from core.settings import TIME_FORMAT
from core.settings import TRAILS_FILE
from core.settings import VERSION
from core.ignore import ignore_event

_condensed_events = {}
_condensing_thread = None
_condensing_lock = threading.Lock()
_thread_data = threading.local()

def create_log_directory():
    if not os.path.isdir(config.LOG_DIR):
        if not config.DISABLE_CHECK_SUDO and check_sudo() is False:
            exit("[!] please rerun with sudo/Administrator privileges")
        os.makedirs(config.LOG_DIR, 0755)
    print("[i] using '%s' for log storage" % config.LOG_DIR)

def get_event_log_handle(sec, flags=os.O_APPEND | os.O_CREAT | os.O_WRONLY, reuse=True):
    retval = None
    localtime = time.localtime(sec)

    _ = os.path.join(config.LOG_DIR, "%d-%02d-%02d.log" % (localtime.tm_year, localtime.tm_mon, localtime.tm_mday))

    if not reuse:
        if not os.path.exists(_):
            open(_, "w+").close()
            os.chmod(_, DEFAULT_EVENT_LOG_PERMISSIONS)

        retval = os.open(_, flags)
    else:
        if _ != getattr(_thread_data, "event_log_path", None):
            if getattr(_thread_data, "event_log_handle", None):
                try:
                    os.close(_thread_data.event_log_handle)
                except OSError:
                    pass

            if not os.path.exists(_):
                open(_, "w+").close()
                os.chmod(_, DEFAULT_EVENT_LOG_PERMISSIONS)

            _thread_data.event_log_path = _
            _thread_data.event_log_handle = os.open(_thread_data.event_log_path, flags)

        retval = _thread_data.event_log_handle

    return retval

def get_error_log_handle(flags=os.O_APPEND | os.O_CREAT | os.O_WRONLY):
    if not hasattr(_thread_data, "error_log_handle"):
        _ = os.path.join(config.LOG_DIR, "error.log")
        if not os.path.exists(_):
            open(_, "w+").close()
            os.chmod(_, DEFAULT_ERROR_LOG_PERMISSIONS)
        _thread_data.error_log_path = _
        _thread_data.error_log_handle = os.open(_thread_data.error_log_path, flags)
    return _thread_data.error_log_handle

def safe_value(value):
    retval = str(value or '-')
    if any(_ in retval for _ in (' ', '"')):
        retval = "\"%s\"" % retval.replace('"', '""')
    return retval

def flush_condensed_events():
    while True:
        time.sleep(CONDENSED_EVENTS_FLUSH_PERIOD)

        with _condensing_lock:
            for key in _condensed_events:
                condensed = False
                events = _condensed_events[key]

                first_event = events[0]
                condensed_event = [_ for _ in first_event]

                for i in xrange(1, len(events)):
                    current_event = events[i]
                    for j in xrange(3, 7):  # src_port, dst_ip, dst_port, proto
                        if current_event[j] != condensed_event[j]:
                            condensed = True
                            if not isinstance(condensed_event[j], set):
                                condensed_event[j] = set((condensed_event[j],))
                            condensed_event[j].add(current_event[j])

                if condensed:
                    for i in xrange(len(condensed_event)):
                        if isinstance(condensed_event[i], set):
                            condensed_event[i] = ','.join(str(_) for _ in sorted(condensed_event[i]))

                log_event(condensed_event, skip_condensing=True)

            _condensed_events.clear()

def log_event(event_tuple, packet=None, skip_write=False, skip_condensing=False):
    global _condensing_thread

    if _condensing_thread is None:
        _condensing_thread = threading.Thread(target=flush_condensed_events)
        _condensing_thread.daemon = True
        _condensing_thread.start()

    try:
        sec, usec, src_ip, src_port, dst_ip, dst_port, proto, trail_type, trail, info, reference = event_tuple
        if ignore_event(event_tuple):
            return
        
        if not (any(check_whitelisted(_) for _ in (src_ip, dst_ip)) and trail_type != TRAIL.DNS):  # DNS requests/responses can't be whitelisted based on src_ip/dst_ip
            if not skip_write:
                localtime = "%s.%06d" % (time.strftime(TIME_FORMAT, time.localtime(int(sec))), usec)

                if not skip_condensing:
                    if any(_ in info for _ in CONDENSE_ON_INFO_KEYWORDS):
                        with _condensing_lock:
                            key = (src_ip, trail)
                            if key not in _condensed_events:
                                _condensed_events[key] = []
                            _condensed_events[key].append(event_tuple)

                        return

                current_bucket = sec / config.PROCESS_COUNT
                if getattr(_thread_data, "log_bucket", None) != current_bucket:  # log throttling
                    _thread_data.log_bucket = current_bucket
                    _thread_data.log_trails = set()
                else:
                    if any(_ in _thread_data.log_trails for _ in ((src_ip, trail), (dst_ip, trail))):
                        return
                    else:
                        _thread_data.log_trails.add((src_ip, trail))
                        _thread_data.log_trails.add((dst_ip, trail))

                event = "%s %s %s\n" % (safe_value(localtime), safe_value(config.SENSOR_NAME), " ".join(safe_value(_) for _ in event_tuple[2:]))
                if not config.DISABLE_LOCAL_LOG_STORAGE:
                    handle = get_event_log_handle(sec)
                    os.write(handle, event)

                if config.LOG_SERVER:
                    remote_host, remote_port = config.LOG_SERVER.split(':')
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.sendto("%s %s" % (sec, event), (remote_host, int(remote_port)))

                if config.SYSLOG_SERVER:
                    extension = "src=%s spt=%s dst=%s dpt=%s trail=%s ref=%s" % (src_ip, src_port, dst_ip, dst_port, trail, reference)
                    _ = CEF_FORMAT.format(syslog_time=time.strftime("%b %d %H:%M:%S", time.localtime(int(sec))), host=HOSTNAME, device_vendor=NAME, device_product="sensor", device_version=VERSION, signature_id=time.strftime("%Y-%m-%d", time.localtime(os.path.getctime(TRAILS_FILE))), name=info, severity=0, extension=extension)
                    remote_host, remote_port = config.SYSLOG_SERVER.split(':')
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.sendto(_, (remote_host, int(remote_port)))

                if config.DISABLE_LOCAL_LOG_STORAGE and not any(config.LOG_SERVER, config.SYSLOG_SERVER) or config.console:
                    sys.stderr.write(event)
                    sys.stderr.flush()

            if config.plugin_functions:
                for _ in config.plugin_functions:
                    _(event_tuple, packet)
    except (OSError, IOError):
        if config.SHOW_DEBUG:
            traceback.print_exc()

def log_error(msg):
    try:
        handle = get_error_log_handle()
        os.write(handle, "%s %s\n" % (time.strftime(TIME_FORMAT, time.localtime()), msg))
    except (OSError, IOError):
        if config.SHOW_DEBUG:
            traceback.print_exc()

def start_logd(address=None, port=None, join=False):
    class ThreadingUDPServer(SocketServer.ThreadingMixIn, SocketServer.UDPServer):
        pass

    class UDPHandler(SocketServer.BaseRequestHandler):
        def handle(self):
            try:
                data, _ = self.request
                sec, event = data.split(" ", 1)
                handle = get_event_log_handle(int(sec), reuse=False)
                os.write(handle, event)
                os.close(handle)
            except:
                if config.SHOW_DEBUG:
                    traceback.print_exc()

    server = ThreadingUDPServer((address, port), UDPHandler)

    print "[i] running UDP server at '%s:%d'" % (server.server_address[0], server.server_address[1])

    if join:
        server.serve_forever()
    else:
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()

def set_sigterm_handler():
    def handler(signum, frame):
        log_error("SIGTERM")
        raise SystemExit

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)

if __name__ != "__main__":
    set_sigterm_handler()
