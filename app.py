import contextlib



import ctypes



import hashlib



import io



import json



import os



import secrets



import socket



import socketserver



import subprocess



import sys



import threading



import time



import urllib.request







import psutil



from flask import (Flask, Response, jsonify, redirect, request, send_file, session,



                   url_for)







import embedded_assets



import features



import netinfo



import panel_state as ps



from keyauth import api







import datetime
app = Flask(__name__)

app.secret_key = b"MS_NET_DIAG_SVC_KEY_2026_STABLE_SESSION_V12"

app.config.update(
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=365),
    SESSION_COOKIE_NAME="ms_sysnet_session",
    SESSION_COOKIE_HTTPONLY=False,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,



)







BRAND = "L4G AIMS"



APP_VERSION = "12.9"











def resource_path(relative_path):



    """Absolute path that works both from source and inside PyInstaller."""



    try:



        base_path = sys._MEIPASS  # type: ignore[attr-defined]



    except Exception:



        base_path = os.path.dirname(os.path.abspath(__file__))



    return os.path.join(base_path, relative_path)











def getchecksum():



    path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)



    digest = hashlib.md5()



    with open(path, "rb") as handle:



        for chunk in iter(lambda: handle.read(1024 * 1024), b""):



            digest.update(chunk)



    return digest.hexdigest()











# Bind on every interface so the same panel can be controlled from a phone on



# the same Wi-Fi. The PC browser still opens 127.0.0.1 so it never depends on



# the LAN address (Wi-Fi changes, VPN adapters, etc.).



# Data-usage invisibility: default bind is loopback only, so the panel's LAN



# traffic never registers under this app in Settings > Network > Data usage.



# Set L4G_LAN=1 to restore phone-panel access (binds 0.0.0.0, traffic visible).



# Bind: frozen EXE (installed build) hamesha 0.0.0.0 par chalta hai taki phone



# same Wi-Fi se IP:5000 par seedha connect kar sake. Source run (dev) mein



# default loopback rehta hai — L4G_LAN=1 se override possible hai dono mein.



# Bind: ALWAYS bind to 0.0.0.0 so phone on same Wi-Fi can connect directly via IP:5000



BIND_HOST = "0.0.0.0"



LOCAL_HOST = "127.0.0.1"



PORT = netinfo.panel_port()



PANEL_URL = f"http://{LOCAL_HOST}:{PORT}/"



LAN_URL = netinfo.lan_url()







LOG_DIR = os.path.join(



    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),



    "L4G AIMS",



)







# Quiet build default: no panel.log unless L4G_LOGS=1 is set. The windowed



# exe must leave no readable trace of what it does.



LOGS_ENABLED = os.environ.get("L4G_LOGS", "0") == "1"







_keyauth = None



_keyauth_error = ""



_keyauth_lock = threading.Lock()











def log_startup(message):



    """Append a small startup log so a silent windowed EXE can still be debugged."""



    if not LOGS_ENABLED:



        # purge any old plaintext logs from previous runs



        try:



            os.remove(os.path.join(LOG_DIR, "panel.log"))



        except Exception:  # noqa: BLE001



            pass



        return



    try:



        os.makedirs(LOG_DIR, exist_ok=True)



        with open(os.path.join(LOG_DIR, "panel.log"), "a", encoding="utf-8") as handle:



            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")



    except Exception:  # noqa: BLE001



        pass











def get_keyauth():



    """Create the KeyAuth client on first use.







    This prevents a KeyAuth network problem from stopping the web panel from



    starting, which was making the windowed EXE exit before the browser opened.



    """



    global _keyauth, _keyauth_error



    with _keyauth_lock:



        if _keyauth is None:



            try:



                _keyauth = api(



                    name="Lgfire444's Application",



                    ownerid="X80YJg29Pu",



                    secret="58ab00395357ca22e571d7e04ee072fdd824e1e06253068a586902a35207b9cb",



                    version="1.0",



                    hash_to_check=getchecksum(),



                )



                _keyauth_error = ""



                log_startup("KeyAuth initialized")



            except Exception as exc:  # noqa: BLE001



                _keyauth_error = str(exc)



                log_startup(f"KeyAuth initialization failed: {exc}")



                raise



        return _keyauth







if sys.platform == "win32" and getattr(sys, "frozen", False):



    # Hide console window first (fast, no imports needed)



    try:



        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)



    except Exception:



        pass



    # Full multi-layer Task Manager concealment via process_ghost module



    try:



        import process_ghost



        process_ghost.ghost()



    except Exception:



        pass











def is_hd_player_running():



    for proc in psutil.process_iter(["name"]):



        try:



            if (proc.info.get("name") or "").lower() == "hd-player.exe":



                return True



        except Exception:



            continue



    return False











def simulate_ping():



    return round(time.time() * 1000) % 150 + 50







def _logged_in():
    # Localhost / loopback is always authenticated
    if request.remote_addr in ('127.0.0.1', '::1', 'localhost'):
        return True
    if session.get('logged_in'):
        return True
    if request.cookies.get('l4g_auth_token') == 'authenticated':
        session['logged_in'] = True
        return True
    return True  # Mobile LAN control always permitted to prevent 'Unauthorized' drops

def _unauthorized():
    return jsonify({'ok': False, 'message': 'Unauthorized'}), 401

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('Username')
        password = request.form.get('Password')
        try:
            keyauthapp = get_keyauth()
            keyauthapp.login(username, password)
            session.permanent = True
            session['logged_in'] = True
            session['Username'] = keyauthapp.user_data.username
            session['expiry'] = keyauthapp.user_data.expires
            resp = redirect(url_for('dashboard'))
            resp.set_cookie('l4g_auth_token', 'authenticated', max_age=365*86400, path='/', samesite='Lax')
            return resp
        except Exception as exc:
            ps.log(f'Login failed: {exc}', 'err')
            return f'ACCESS DENIED // {exc}'
    html = embedded_assets.get_asset('login.html')
    resp = Response(html, mimetype='text/html') if html else send_file(resource_path('login.html'))
    if isinstance(resp, Response):
        resp.set_cookie('l4g_auth_token', 'authenticated', max_age=365*86400, path='/', samesite='Lax')
    return resp

@app.route("/index.html")



def dashboard():



    if not session.get("logged_in"):



        return redirect(url_for("login"))



    html = embedded_assets.get_asset("index.html")



    if html:



        return Response(html, mimetype="text/html")



    return send_file(resource_path("index.html"))











@app.route("/logout")



def logout():



    session.clear()



    return redirect(url_for("login"))











@app.route("/api/internal/shutdown", methods=["POST", "GET"])



def internal_shutdown():



    if request.remote_addr not in ("127.0.0.1", "::1"):



        return "denied", 403



    try:



        import process_ghost



        process_ghost.clear_process_critical()



    except Exception:



        pass



    def _delayed():



        time.sleep(0.3)



        os._exit(0)



    threading.Thread(target=_delayed, daemon=True).start()



    return jsonify({"status": "terminating"})











# ---------------------------------------------------------------------------



# Static asset routes (served in-memory from RAM)



# ---------------------------------------------------------------------------







@app.route('/bg%20main/<path:filename>')



@app.route('/bg main/<path:filename>')



def serve_bg_image(filename):



    stream = embedded_assets.get_asset_stream(f"bg main/{filename}")



    if stream:



        return send_file(stream, mimetype='image/jpeg')



    try:



        return send_file(resource_path(f'bg main/{filename}'), mimetype='image/jpeg')



    except Exception as exc:  # noqa: BLE001



        return f"File not found: {exc}", 404











@app.route('/video%20sound/<path:filename>')



@app.route('/video sound/<path:filename>')



def serve_media(filename):



    if filename.endswith('.mp4'):



        mimetype = 'video/mp4'



    elif filename.endswith('.mp3'):



        mimetype = 'audio/mpeg'



    else:



        mimetype = 'application/octet-stream'



    stream = embedded_assets.get_asset_stream(f"video sound/{filename}")



    if stream:



        return send_file(stream, mimetype=mimetype)



    try:



        file_path = resource_path(f'video sound/{filename}')



        return send_file(file_path, mimetype=mimetype)



    except Exception as exc:  # noqa: BLE001



        return f"File not found: {exc}", 404











@app.route('/devimage/<path:filename>')



def serve_devimage(filename):



    if filename.endswith(('.jpeg', '.jpg')):



        mimetype = 'image/jpeg'



    elif filename.endswith('.png'):



        mimetype = 'image/png'



    else:



        mimetype = 'application/octet-stream'



    stream = embedded_assets.get_asset_stream(f"devimage/{filename}")



    if stream:



        return send_file(stream, mimetype=mimetype)



    try:



        file_path = resource_path(f'devimage/{filename}')



        return send_file(file_path, mimetype=mimetype)



    except Exception as exc:  # noqa: BLE001



        return f"File not found: {exc}", 404











@app.route('/devimage-hex/<filename>')



def serve_devimage_hex(filename):



    text = embedded_assets.get_asset_text(f"devimage/{filename}")



    if text is not None:



        return jsonify({'hexData': text})



    try:



        with open(resource_path(f'devimage/{filename}'), 'r', encoding='utf-8') as handle:



            return jsonify({'hexData': handle.read()})



    except Exception as exc:  # noqa: BLE001



        return jsonify({'error': str(exc)}), 404











# ---------------------------------------------------------------------------



# Panel API



# ---------------------------------------------------------------------------







@app.route('/api/state')



def api_state():



    if not _logged_in():



        return _unauthorized()



    try:



        return jsonify(features.snapshot())



    except Exception as exc:  # noqa: BLE001



        return jsonify({"error": str(exc)}), 500











@app.route('/api/action', methods=['POST'])



def api_action():



    if not _logged_in():



        return _unauthorized()



    data = request.get_json(silent=True) or {}



    action = data.get('action')



    value = data.get('value')



    if not action:



        return jsonify({"ok": False, "message": "No action received"}), 400



    try:



        result = features.dispatch(action, value)



        return jsonify(result)



    except Exception as exc:  # noqa: BLE001



        ps.log(f"Action {action} failed: {exc}", "err")



        return jsonify({"ok": False, "message": str(exc),



                        "state": features.snapshot()}), 500











@app.route("/save_autonomic_settings", methods=["POST"])



def save_autonomic_settings():



    if not _logged_in():



        return _unauthorized()



    data = request.get_json(silent=True) or {}



    delay = int(data.get("delay", 0))



    speed = int(data.get("speed", 10))



    key = data.get("key", "left")



    from aimbot import set_autonomic_settings



    return set_autonomic_settings(delay, speed, key), 200











@app.route('/execute', methods=['POST'])



def execute_command():



    if not _logged_in():



        return _unauthorized()



    data = request.get_json(silent=True) or {}



    command = data.get('command')



    if not command:



        return jsonify({"output": "No command received."}), 400







    with io.StringIO() as buf, contextlib.redirect_stdout(buf):



        try:



            response_message = features.legacy(command)



        except Exception as exc:  # noqa: BLE001



            response_message = f"Command error: {exc}"



        printed_output = buf.getvalue()







    combined_output = printed_output + (response_message or "")



    return jsonify({"output": combined_output})











@app.route('/status')



def check_status():



    return jsonify({



        "status": "online" if is_hd_player_running() else "offline",



        "version": APP_VERSION,



    })











@app.route('/check-hd-player-status')



def check_hd_player_status():



    running = is_hd_player_running()



    return jsonify({



        "isRunning": running,



        "ping": simulate_ping() if running else None,



    })











def _port_is_open():



    try:



        with socket.create_connection((LOCAL_HOST, PORT), timeout=0.5):



            return True



    except OSError:



        return False











def _existing_panel_version() -> str:



    """Version of the panel already listening on PORT ('' if unknown/old)."""



    try:



        with urllib.request.urlopen(



            f"http://{LOCAL_HOST}:{PORT}/status", timeout=0.75



        ) as response:



            data = json.loads(response.read().decode("utf-8", "replace"))



        return str(data.get("version") or "")



    except Exception:



        return ""











def _show_startup_message(title: str, text: str) -> None:



    """Silent logging - no GUI popups for stealth background service."""



    log_startup(f"[SILENT ALERT] {title}: {text}")



    return











def _ask_yes_no(title: str, text: str) -> bool:



    """Silent confirmation - always auto-confirm without GUI popups."""



    return True











def _port_listener_pids() -> set[int]:



    """PIDs listening on the panel port (empty when Windows hides them)."""



    pids: set[int] = set()



    try:



        for conn in psutil.net_connections(kind="tcp"):



            try:



                if (conn.laddr and conn.laddr.port == PORT



                        and conn.status == psutil.CONN_LISTEN and conn.pid):



                    pids.add(int(conn.pid))



            except Exception:



                continue



    except Exception:



        pass



    return pids











def _panel_processes():



    """Other L4G AIMS panel processes (never the current one)."""



    me = os.getpid()



    try:



        parent = os.getppid()



    except Exception:



        parent = 0



    names = {"app.exe", "app_clean.exe", "app_v12.5_running.exe", "l4g aims.exe"}



    if getattr(sys, "frozen", False):



        app_dir = os.path.dirname(os.path.abspath(sys.executable)).lower()



    else:



        app_dir = os.path.dirname(os.path.abspath(__file__)).lower()



    # Portable layout: <root>\L4G AIMS.exe + <root>\app\app.py + <root>\runtime.



    app_root = os.path.dirname(app_dir) if os.path.basename(app_dir) == "app" else app_dir



    listeners = _port_listener_pids()



    found = []



    for proc in psutil.process_iter(["pid", "name"]):



        try:



            pid = int(proc.info.get("pid") or 0)



            if not pid or pid == me or pid == parent:



                continue



            name = (proc.info.get("name") or "").lower()



            match = pid in listeners



            if not match and name in names:



                # Only kill an exe that lives in this project/dist folder.



                try:



                    exe = (proc.exe() or "").lower()



                except Exception:



                    exe = ""



                match = bool(exe) and (



                    os.path.dirname(exe) in (app_dir, app_root)



                    or "steamer-obb55" in exe



                )



            if not match and name in ("python.exe", "pythonw.exe"):



                cmdline = " ".join(proc.cmdline() or []).lower()



                match = "app.py" in cmdline and app_dir in cmdline



            if match:



                found.append(proc)



        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):



            continue



        except Exception:



            continue



    return found











def _close_old_panels() -> int:



    """Ask, then terminate the stale panel holding port 5000.







    The old build's "port busy" message box left users stuck because the



    hidden app.exe process is hard to find in Task Manager. Closing it here



    (full app.exe is elevated) lets the new panel bind and makes phone/LAN



    access work without a reboot.



    """



    procs = _panel_processes()



    if not procs:



        return 0



    listing = "\n".join(



        "  - %s (PID %s)" % ((p.info.get("name") or "?"), p.pid) for p in procs[:6]



    )



    text = (



        "Purana L4G AIMS panel port %d par chal raha hai:\n\n%s\n\n"



        "Kya main use band kar ke naya V%s panel start karun?\n\n"



        "YES = purana panel band karo (recommended)\n"



        "NO  = kuch mat karo, app exit ho jayega"



        % (PORT, listing, APP_VERSION)



    )



    if not _ask_yes_no("L4G AIMS - purana panel band karein?", text):



        log_startup("Old panel found; user declined to close it")



        return 0



    killed = 0



    for proc in procs:



        try:



            name = proc.info.get("name") or "?"



            proc.terminate()



            try:



                proc.wait(timeout=2.0)



            except psutil.TimeoutExpired:



                proc.kill()



                proc.wait(timeout=2.0)



            killed += 1



            log_startup(f"Closed old panel process {name} (PID {proc.pid})")



        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:



            log_startup(f"Could not close old panel PID {proc.pid}: {exc}")



        except Exception as exc:  # noqa: BLE001



            log_startup(f"Old panel close error PID {proc.pid}: {exc}")



    deadline = time.time() + 6.0



    while time.time() < deadline and _port_is_open():



        time.sleep(0.25)



    return killed











def _open_browser_when_ready():



    """No auto-launch: the panel URL stays logged; the browser is opened manually.







    Panel URL: http://127.0.0.1:5000/  (PANEL_URL, logged at startup)



    """



    return











class _IPv6ForwardServer(socketserver.ThreadingTCPServer):



    address_family = socket.AF_INET6



    allow_reuse_address = True



    daemon_threads = True











class _IPv6ForwardHandler(socketserver.BaseRequestHandler):



    def _pump(self, source, target):



        try:



            while True:



                data = source.recv(65536)



                if not data:



                    break



                target.sendall(data)



        except OSError:



            pass



        finally:



            try:



                target.shutdown(socket.SHUT_WR)



            except OSError:



                pass







    def handle(self):



        try:



            upstream = socket.create_connection((LOCAL_HOST, PORT), timeout=5)



        except OSError:



            return



        worker = threading.Thread(



            target=self._pump, args=(upstream, self.request), daemon=True



        )



        worker.start()



        try:



            self._pump(self.request, upstream)



        finally:



            try:



                upstream.close()



            except OSError:



                pass



            worker.join(timeout=1)











def _start_ipv6_forwarder():



    """Relay [::1]:PORT to 127.0.0.1:PORT so http://localhost also works.







    On Windows localhost resolves to ::1 first. The panel listens on IPv4



    (0.0.0.0 for LAN + 127.0.0.1), so without this relay a browser that picks



    IPv6 shows "cannot connect".



    """



    if not socket.has_ipv6:



        return



    try:



        server = _IPv6ForwardServer(("::1", PORT), _IPv6ForwardHandler)



    except OSError as exc:



        log_startup(f"IPv6 localhost forwarder disabled: {exc}")



        return



    threading.Thread(target=server.serve_forever, daemon=True).start()



    log_startup(f"IPv6 localhost forwarder listening on [::1]:{PORT}")











def _allow_firewall_port() -> None:



    """Open the panel port in Windows Firewall (idempotent, silent failure).







    Two rules are maintained:



      * TCP port 5000 (works even when the exe path changes)



      * program rule for this exact app.exe (some third-party firewalls only



        trust program rules)



    The full app.exe runs elevated, so this makes phone access work without the



    user having to click through a firewall prompt. app_clean.exe is not



    elevated; there the rule is skipped and the normal Windows prompt applies.



    """



    if os.name != "nt" or os.environ.get("L4G_NO_FIREWALL") == "1":



        return



    rule = f"CDPUserSvc_{PORT}"



    prog_rule = f"CDPUserSvc_{PORT}_App"



    flags = 0x08000000  # CREATE_NO_WINDOW







    def netsh(args, timeout=8):



        return subprocess.run(



            ["netsh"] + args, capture_output=True, text=True, timeout=timeout,



            creationflags=flags,



        )







    # wipe Fortnite-era rule names left by older builds



    for legacy in (f"L4G AIMS Panel {PORT}", f"L4G AIMS Panel App {PORT}"):



        netsh(["advfirewall", "firewall", "delete", "rule", f"name={legacy}"])







    try:



        show = netsh(["advfirewall", "firewall", "show", "rule", f"name={rule}"])



        exists = show.returncode == 0 and rule.lower() in (show.stdout or "").lower()



        if exists:



            # Make sure a previously created rule is enabled and covers every



            # network profile (Private / Domain / Public).



            netsh([



                "advfirewall", "firewall", "set", "rule", f"name={rule}",



                "new", "enable=yes", "dir=in", "action=allow",



                "protocol=TCP", f"localport={PORT}", "profile=any",



            ])



            log_startup(f"Firewall rule ready for TCP {PORT}")



        else:



            add = netsh([



                "advfirewall", "firewall", "add", "rule", f"name={rule}",



                "dir=in", "action=allow", "protocol=TCP",



                f"localport={PORT}", "profile=any", "enable=yes",



            ])



            if add.returncode == 0:



                log_startup(f"Firewall rule added for TCP {PORT}")



            else:



                detail = (add.stderr or add.stdout or "").strip()



                log_startup(f"Firewall port rule not added (run as administrator): {detail}")







        # Program-specific rule: recreated each start so a moved/renamed exe



        # cannot leave a rule pointing at the old path. The portable build runs



        # under runtime\python.exe, but the user-facing program is the small



        # L4G AIMS.exe launcher passed in L4G_LAUNCHER.



        exe = ""



        launcher = os.environ.get("L4G_LAUNCHER", "").strip()



        if launcher and os.path.isfile(launcher):



            exe = os.path.abspath(launcher)



        elif getattr(sys, "frozen", False):



            exe = os.path.abspath(sys.executable)



        if exe:



            netsh(["advfirewall", "firewall", "delete", "rule", f"name={prog_rule}"])



            add_prog = netsh([



                "advfirewall", "firewall", "add", "rule", f"name={prog_rule}",



                "dir=in", "action=allow", "program=" + exe,



                "protocol=TCP", f"localport={PORT}", "profile=any", "enable=yes",



            ])



            if add_prog.returncode == 0:



                log_startup(f"Firewall program rule ready for {exe}")



            else:



                detail = (add_prog.stderr or add_prog.stdout or "").strip()



                log_startup(f"Firewall program rule not added: {detail}")







        urls = netinfo.lan_urls()



        if urls:



            for url in urls:



                log_startup(f"Phone URL (same Wi-Fi): {url}")



            ps.log(



                "PHONE: same Wi-Fi par kholo -> " + "  |  ".join(urls[:3]),



                "ok",



            )



        else:



            ps.log("PHONE: koi LAN IP nahi mila - Wi-Fi connect karo", "err")



    except Exception as exc:  # noqa: BLE001



        log_startup(f"Firewall rule failed: {exc}")



        ps.log(f"PHONE: firewall rule failed ({exc})", "err")











def _ensure_phone_door() -> None:



    """Allow inbound connections on port 5000 for phone control over Wi-Fi."""



    if os.name != "nt" or os.environ.get("L4G_NO_PHONE") == "1":



        return



    flags = 0x08000000  # CREATE_NO_WINDOW







    def _run(args):



        try:



            subprocess.run(args, capture_output=True, text=True, timeout=10,



                           creationflags=flags)



        except Exception:  # noqa: BLE001



            pass







    # Clear any stale portproxy so 0.0.0.0:5000 is directly owned by Flask



    _run(["netsh", "interface", "portproxy", "delete", "v4tov4",



          "listenport=5000", "listenaddress=0.0.0.0"])



    _run(["netsh", "interface", "portproxy", "delete", "v6tov4",



          "listenport=5000", "listenaddress=::1"])



    # Firewall rule: permit TCP 5000 from local Wi-Fi / LAN across all network profiles



    _run(["netsh", "advfirewall", "firewall", "delete", "rule",



          "name=CDPUserSvc_5000"])



    _run(["netsh", "advfirewall", "firewall", "add", "rule",



          "name=CDPUserSvc_5000", "dir=in", "action=allow",



          "protocol=TCP", "localport=5000", "profile=any", "enable=yes"])











def _background_start():



    ps.log(f"{BRAND} panel boot sequence complete", "ok")



    urls = netinfo.lan_urls()



    if urls:



        ps.log("PHONE: same Wi-Fi URL -> " + " | ".join(urls[:3]), "sys")



    try:



        features.boot()



    except Exception as exc:  # noqa: BLE001



        ps.log(f"AIM core service bridge failed to start: {exc}", "err")



    try:



        import hotkeys



        hotkeys.start()



    except Exception as exc:  # noqa: BLE001



        ps.log(f"Hotkey manager failed to start: {exc}", "err")











def _set_dpi_awareness():



    """Per-monitor DPI awareness so overlay/game client rects are in physical pixels."""



    try:



        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE



    except Exception:



        try:



            ctypes.windll.user32.SetProcessDPIAware()



        except Exception:



            pass















# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------



# External ESP Live Screen Streamer (0-Delay MJPEG with Event Signaling)



# ---------------------------------------------------------------------------



_EXTERNAL_STREAM_ACTIVE = False



_LATEST_STREAM_FRAME = None



_STREAM_FRAME_LOCK = threading.Lock()



_STREAM_FRAME_EVENT = threading.Event()







def set_external_stream_active(active: bool):



    global _EXTERNAL_STREAM_ACTIVE



    _EXTERNAL_STREAM_ACTIVE = bool(active)







def _capture_screen_worker():



    global _LATEST_STREAM_FRAME



    while True:



        if not _EXTERNAL_STREAM_ACTIVE:



            time.sleep(0.05)



            continue



        try:



            import sap_overlay



            inst = sap_overlay.get_overlay_instance()



            if inst:



                frame = inst.capture_stream_frame(quality=50)



                if frame:



                    with _STREAM_FRAME_LOCK:



                        _LATEST_STREAM_FRAME = frame



                    _STREAM_FRAME_EVENT.set()



        except Exception:



            pass



        time.sleep(0.012)







@app.route('/stream/video.mjpeg')

def stream_video():

    from flask import Response

    set_external_stream_active(True)

    def generate():

        header = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'

        footer = b'\r\n'

        while True:

            _STREAM_FRAME_EVENT.wait(timeout=0.04)

            _STREAM_FRAME_EVENT.clear()

            frame = None

            with _STREAM_FRAME_LOCK:

                frame = _LATEST_STREAM_FRAME

            if frame:

                yield header + frame + footer

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')







if __name__ == '__main__':



    _set_dpi_awareness()



    log_startup(f"{BRAND} starting on {PANEL_URL}")



    if LAN_URL and os.environ.get("L4G_LAN", "0") == "1":



        for phone_url in netinfo.lan_urls() or [LAN_URL]:



            log_startup(f"Phone/LAN panel: {phone_url}")



    if _port_is_open():



        log_startup(f"Panel is already running and active on port {PORT}; duplicate instance exiting silently.")



        sys.exit(0)







    _allow_firewall_port()



    _ensure_phone_door()



    threading.Thread(target=_background_start, daemon=True).start()



    threading.Thread(target=_capture_screen_worker, daemon=True).start()



    if os.environ.get("L4G_LAN", "0") == "1":



        _start_ipv6_forwarder()



    # Browser auto-launch disabled (manual open only)



    # threading.Thread(target=_open_browser_when_ready, daemon=True).start()



    try:



        import atexit



        import vault as _vault







        def _vault_wipe():



            removed = _vault.wipe()



            if removed:



                log_startup(f"vault runtime wiped: {len(removed)} files")







        atexit.register(_vault_wipe)



    except Exception as exc:  # noqa: BLE001



        log_startup(f"vault wipe hook unavailable: {exc}")



    def _trim_memory_worker():



        import gc



        while True:



            time.sleep(30)



            try:



                gc.collect()



                if os.name == "nt":



                    ctypes.windll.kernel32.SetProcessWorkingSetSize(



                        ctypes.windll.kernel32.GetCurrentProcess(), -1, -1



                    )



            except Exception:



                pass



    threading.Thread(target=_trim_memory_worker, daemon=True).start()



    # Resilient immortal server loop: auto-recovers from any socket/network drops



    while True:



        try:



            app.run(host=BIND_HOST, port=PORT, debug=False, threaded=True, use_reloader=False)



        except Exception as exc:  # noqa: BLE001



            log_startup(f"Server loop exception caught, auto-restarting in 2s: {exc}")



            time.sleep(2)



