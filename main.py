import os, json, asyncio, subprocess, re, typing, time, signal, shutil, hashlib, uuid, platform, httpx
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import URLError, HTTPError
from fastapi import FastAPI, BackgroundTasks, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
from contextlib import asynccontextmanager # <--- Garante este import

# --- CONFIGURAÇÕES DE CAMINHOS ---
# Agora apontamos SEMPRE para as pastas dos volumes (/app e /www)
# Assim, se editares no ZimaOS, a alteração é aplicada.
WWW_PATH = "/www"
CONFIG_DIR = "/config"
CONFIG_FILE = os.path.join(CONFIG_DIR, "tasks.json")
RCLONE_CONFIG = os.path.join(CONFIG_DIR, "rclone.conf")
LOGS_DIR = os.path.join(CONFIG_DIR, "logs")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
LICENSE_FILE = os.path.join(CONFIG_DIR, "license.json")
BISYNC_WORKDIR = os.path.join(CONFIG_DIR, "bisync")
HWID_SALT = "syncpulse_secret_salt_2026"

for p in [LOGS_DIR, BISYNC_WORKDIR, "/config"]:
    if not os.path.exists(p): os.makedirs(p, exist_ok=True)

def get_secure_hwid():
    """Devolve um fingerprint hash; nunca expõe o identificador bruto à API."""
    machine_id = os.getenv("SYNCPULSE_HWID", "").strip()
    if not machine_id:
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path, "r", encoding="utf-8") as source:
                    machine_id = source.read().strip()
                if machine_id:
                    break
            except OSError:
                pass
    raw = "|".join([machine_id, str(uuid.getnode()), platform.node()])
    return hashlib.sha256(f"{HWID_SALT}|{raw}".encode("utf-8")).hexdigest()

def load_settings():
    """Carrega as definições do ficheiro garantindo que todas as chaves existem."""
    defaults = {
        "auto_simulate": True, "terms_accepted": False,
        "license_email": "", "license_key": ""
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data) # Junta o que está no disco com os padrões
        except:
            pass
    return defaults

def load_license():
    try:
        with open(LICENSE_FILE, "r", encoding="utf-8") as source:
            data = json.load(source)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def load_tasks():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f: return json.load(f)
        except: return []
    return []

def get_initial_state():
    """Inicializa o estado global lendo a licença do disco de forma rigorosa."""
    s = load_settings()
    lic = load_license() # Esta função lê o /config/license.json
    hwid_atual = get_secure_hwid()

    # Validação Híbrida:
    # 1. O ficheiro tem de ter "active": true (como no teu print)
    # 2. O HWID gravado tem de ser IGUAL ao HWID atual do hardware
    is_valid = lic.get("active") is True and lic.get("hwid") == hwid_atual

    return {
        "running": {}, "logs": {}, "active_files": {}, "finished_files": {},
        "all_files": {}, "skipped_files": {}, "stats": {}, "failed_files": {},
        "file_sizes": {},
        "auto_simulate": s.get("auto_simulate", True),
        "terms_accepted": s.get("terms_accepted", False),
        # Enviamos as duas variantes para garantir que o Frontend e o Backend se entendem
        "licensed": is_valid,
        "license_active": is_valid, 
        "license_info": {
            "email": lic.get("email", ""),
            "key": lic.get("key", ""),
            "device_name": lic.get("device_name", ""),
            "activated_at": lic.get("activated_at", ""),
            "plan": lic.get("plan", 1)
        },
        "hwid": hwid_atual
    }

# Única definição de STATE no topo do ficheiro
STATE = get_initial_state()
PROCESSES = {}
TASK_LOCKS = {}
WATCHERS = {}
REALTIME_HANDLES = {}
APP_LOOP = None
REALTIME_DEBOUNCE_SECONDS = 2.0
REMOTE_POLL_SECONDS = 30
CLOUD_STATE_CACHE = {}
HEALTH_CACHE = []
    
# --- 1. LÓGICA DE AUTO-INSTALAÇÃO (BOOTSTRAP) ---
# Deve correr logo no início
def bootstrap():
    src_app, src_www = "/app_dist", "/www_dist"
    dst_app, dst_www, dst_config = "/app", "/www", "/config"

    try:
        # 1. Garantir que as pastas de destino existem
        for p in [dst_app, dst_www, dst_config]:
            os.makedirs(p, exist_ok=True)

        # 2. Copiar/Atualizar CÓDIGO (/app) - SEMPRE sobrecreve
        print(">>> A atualizar motor (main.py) no ZimaOS...")
        for item in os.listdir(src_app):
            # Ignora a pasta www dentro da pasta app para evitar duplicação
            if item == "www":
                continue
            
            s, d = os.path.join(src_app, item), os.path.join(dst_app, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)

        # 3. Copiar/Atualizar FRONTEND (/www) - SEMPRE sobrecreve
        print(">>> A atualizar interface (index.html) no ZimaOS...")
        if os.path.exists(src_www):
            shutil.copytree(src_www, dst_www, dirs_exist_ok=True)
            
        # 4. Forçar permissões para evitar erros de acesso
        os.system(f"chmod -R 777 {dst_app} {dst_www} {dst_config}")
        
        print(">>> Bootstrap: Ficheiros sincronizados com a versão do PC.")

    except Exception as e:
        print(f">>> Erro crítico no Bootstrap: {e}")

# Executa o bootstrap logo no arranque
bootstrap()

# --- 2. CONFIGURAÇÕES E AGENDADOR ---
app_scheduler = AsyncIOScheduler()
# (Mantém as tuas variáveis de caminhos como WWW_PATH, etc.)

async def silent_license_check():
    """Valida a licença no Railway em background sem interromper o utilizador."""
    await asyncio.sleep(10) # Aguarda 10 segundos após o boot para não pesar
    
    if not STATE.get("licensed"):
        return

    print(">>> [BACKGROUND] A validar licença com o servidor central...")
    email = STATE["license_info"].get("email")
    key = STATE["license_info"].get("key")
    hwid = STATE.get("hwid")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{AUTH_SERVER_URL}/api/licenses/activate",
                json={"email": email, "license_key": key, "hwid": hwid},
                timeout=10.0
            )

        if response.status_code == 200:
            res_data = response.json()
            if res_data.get("valid"):
                # Licença confirmada! Atualizamos o ficheiro local com a data do check
                STATE["license_info"]["last_check"] = str(datetime.now())
                with open(LICENSE_FILE, "w") as f:
                    json.dump(STATE["license_info"], f)
                print(">>> [BACKGROUND] Licença confirmada e atualizada.")
            else:
                # O servidor diz que a licença já não é válida (ex: refund ou remoção de slot)
                print(">>> [BACKGROUND] Licença revogada pelo servidor!")
                await revoke_license_local()
        else:
            # Servidor offline ou erro 500: mantemos o utilizador ativo (Modo Híbrido/Offline)
            print(f">>> [BACKGROUND] Servidor central indisponível ({response.status_code}).")

    except Exception as e:
        print(f">>> [BACKGROUND] Sem ligação à internet para validar: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global APP_LOOP
    APP_LOOP = asyncio.get_running_loop()
    
    # 1. Inicia TUDO o que for local imediatamente (Sincronização arranca já)
    if STATE.get("licensed"):
        print(">>> [STARTUP] Licença local detectada. A iniciar motores...")
        sync_realtime_watchers(load_tasks())
        sync_scheduled_tasks(load_tasks())
        asyncio.create_task(poll_realtime_download_tasks())
    
    asyncio.create_task(update_health_cache())
    
    # 2. Configura o polling periódico (se licenciado)
    if STATE.get("licensed"):
        app_scheduler.add_job(
            poll_realtime_download_tasks, 'interval', seconds=REMOTE_POLL_SECONDS,
            id='remote-realtime-poll', replace_existing=True,
            max_instances=1, coalesce=True
        )

    # 3. LANÇA A VERIFICAÇÃO HÍBRIDA EM SEGUNDO PLANO
    asyncio.create_task(silent_license_check())

    app_scheduler.start()
    
    yield # App operacional
    
    if app_scheduler.running:
        app_scheduler.shutdown(wait=False)

# --- 4. AGORA SIM, CRIAR A INSTÂNCIA DA APP ---
app = FastAPI(lifespan=lifespan)

class ConnectionManager:
    def __init__(self): self.active_connections = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections: self.active_connections.remove(websocket)
    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try: await connection.send_json(message)
            except: pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "init", "tasks": load_tasks(), "state": STATE})
        while True: await websocket.receive_text()
    except: manager.disconnect(websocket)

@app.post("/api/license/activate")
async def activate_license_local(request: Request):
    """
    Ativação Manual: Comunica com o Railway, regista o HWID e 
    desbloqueia a App imediatamente.
    """
    try:
        data = await request.json()
        email = str(data.get("email") or "").strip().lower()
        key = str(data.get("key") or "").strip()
        # Se o utilizador não der um nome, usamos o nome do sistema (Zimatest/ZimaBoard)
        device_name = str(data.get("device_name") or socket.gethostname()).strip()[:120]
        
        if not email or not key:
            return JSONResponse(status_code=400, content={"message": "E-mail e Chave são obrigatórios."})

        hwid = get_secure_hwid()

        # 1. CHAMADA AO SERVIDOR CENTRAL (RAILWAY)
        print(f">>> [LICENSE] A tentar ativar: {email} em {device_name}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{AUTH_SERVER_URL}/api/licenses/activate",
                json={
                    "email": email,
                    "license_key": key,
                    "hwid": hwid,
                    "device_name": device_name
                },
                timeout=15.0
            )

        res_data = response.json()

        # 2. SE O SERVIDOR RECUSAR
        if response.status_code != 200 or not res_data.get("valid"):
            return JSONResponse(
                status_code=response.status_code,
                content={"message": res_data.get("message", "Licença inválida ou limite atingido.")}
            )

        # 3. SE O SERVIDOR ACEITAR: PREPARAR DADOS PARA O DISCO
        # Adicionamos 'last_check' para o modo híbrido saber quando foi a última validação online
        license_data = {
            "email": email,
            "key": key,
            "hwid": hwid,
            "device_name": device_name,
            "plan": res_data.get("plan", 1),
            "activated_at": datetime.now().isoformat(),
            "last_check": datetime.now().isoformat() 
        }

        # 4. PERSISTÊNCIA FÍSICA (Garante que sobrevive ao Reboot)
        with open(LICENSE_FILE, "w", encoding="utf-8") as f:
            json.dump(license_data, f)
            f.flush()
            os.fsync(f.fileno()) # Força o ZimaOS a gravar no disco real

        # 5. ATUALIZAÇÃO DO ESTADO EM MEMÓRIA (Desbloqueio instantâneo)
        STATE["licensed"] = True
        STATE["license_info"] = license_data

        # 6. LIGAR MOTORES DE SINCRONIZAÇÃO (Agora que temos licença)
        sync_realtime_watchers(load_tasks())
        sync_scheduled_tasks(load_tasks())
        if not app_scheduler.get_job('remote-realtime-poll'):
            app_scheduler.add_job(
                poll_realtime_download_tasks, 'interval', seconds=REMOTE_POLL_SECONDS,
                id='remote-realtime-poll', replace_existing=True
            )

        # Avisar o frontend via WebSocket para remover os cadeados
        await manager.broadcast({"type": "update", "state": STATE})

        return {
            "status": "ok", 
            "message": "SyncPulse Pro Ativado com sucesso!", 
            "plan": license_data["plan"]
        }

    except Exception as e:
        print(f">>> [LICENSE] Erro crítico na ativação: {e}")
        return JSONResponse(
            status_code=500, 
            content={"message": "Erro ao contactar servidor de ativação."}
        )

async def revoke_license_local():
    """Bloqueia a app imediatamente se a validação falhar."""
    STATE["licensed"] = False
    if os.path.exists(LICENSE_FILE):
        os.remove(LICENSE_FILE)
    
    # Parar os motores de sincronização para respeitar o bloqueio
    for tid in list(WATCHERS):
        stop_realtime_watcher(tid)
    
    if app_scheduler.get_job('remote-realtime-poll'):
        app_scheduler.remove_job('remote-realtime-poll')
        
    await manager.broadcast({"type": "update", "state": STATE})
    print(">>> [LICENSE] Bloqueio Pro aplicado após verificação negativa.")

# --- 3. DEFINIR O LIFESPAN (DEVE VIR ANTES DA APP) ---


# Importação Watchdog
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    
app_scheduler = AsyncIOScheduler() # Renomeado para evitar confusão se necessário 






# --- LICENCIAMENTO ---------------------------------------------------------
# O Railway é a fonte de verdade para licenças e limite de dispositivos.
# A aplicação guarda localmente apenas uma ativação já validada para que não
# seja necessário consultar a API a cada sincronização.
TEST_LICENSE_EMAIL = "syncpulsegeral@gmail.com"
TEST_LICENSE_KEY = "SYNC-TEST-2026-UNLOCK"
AUTH_SERVER_URL = os.getenv(
    "SYNCPULSE_AUTH_SERVER_URL", "https://syncpulse-auth-production.up.railway.app"
).rstrip("/")
LICENSE_API_URL = f"{AUTH_SERVER_URL}/api/licenses/activate"
HWID_SALT = os.getenv("SYNCPULSE_HWID_SALT", "syncpulse-hwid-v1")



def get_device_name():
    """Nome legível do dispositivo para a gestão no portal de licenças."""
    configured_name = os.getenv("SYNCPULSE_DEVICE_NAME", "").strip()
    if configured_name:
        return configured_name[:120]
    host_name = (platform.node() or "").strip()
    # Em Docker, o hostname costuma ser um ID hexadecimal pouco legível.
    suffix = host_name[:6].upper() if host_name else "LOCAL"
    return f"Dispositivo SyncPulse ({suffix})"



def save_license(data):
    with open(LICENSE_FILE, "w", encoding="utf-8") as target:
        json.dump(data, target, indent=2, ensure_ascii=False)
        target.flush()
        os.fsync(target.fileno())

def validate_license_with_api(email, license_key, device_name=None):
    """Consulta a API/BD de licenças; a BD decide e regista os limites 1/3/5."""
    payload = json.dumps({
        "email": str(email or "").strip().lower(),
        "license_key": str(license_key or "").strip(),
        "hwid": get_secure_hwid(),
        "device_name": device_name or get_device_name()
    }).encode("utf-8")
    request = UrlRequest(
        LICENSE_API_URL, data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST"
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            return {"valid": False, "message": "Resposta inválida do servidor de licenças."}
        return {"valid": result.get("valid") is True, "message": result.get("message", "Licença inválida."), "plan": result.get("plan"), "code": result.get("code")}
    except HTTPError as error:
        try:
            result = json.loads(error.read().decode("utf-8"))
            return {"valid": False, "message": result.get("message", "Licença recusada."), "code": result.get("code")}
        except Exception:
            return {"valid": False, "message": "Licença recusada pelo servidor."}
    except (URLError, TimeoutError, ValueError) as error:
        print(f"Erro ao validar licença na API: {error}")
        return {"valid": False, "message": "Não foi possível contactar o servidor de licenças."}

def is_license_active():
    license_data = load_license()
    return license_data.get("active") is True and license_data.get("hwid") == get_secure_hwid()

def stop_all_realtime_watchers():
    for tid in list(WATCHERS):
        stop_realtime_watcher(tid)
    for handle in REALTIME_HANDLES.values():
        handle.cancel()
    REALTIME_HANDLES.clear()

SCHEDULE_JOB_PREFIX = "scheduled-sync-"
SCHEDULE_INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

def sync_scheduled_tasks(tasks):
    """Recria os jobs APScheduler para as tarefas configuradas como Agendado."""
    for job in app_scheduler.get_jobs():
        if job.id.startswith(SCHEDULE_JOB_PREFIX):
            app_scheduler.remove_job(job.id)

    if not is_license_active():
        return

    for task in tasks:
        if task.get("trigger") != "sched":
            continue
        tid = str(task.get("id", ""))
        interval = task.get("interval", "1h")
        if not tid:
            continue
        job_id = f"{SCHEDULE_JOB_PREFIX}{tid}"
        task_copy = dict(task)
        if interval == "daily":
            try:
                hour, minute = map(int, str(task.get("daily_time") or "03:00").split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except ValueError:
                print(f"Horário diário inválido para a tarefa {tid}.")
                continue
            app_scheduler.add_job(
                rclone_worker, "cron", hour=hour, minute=minute,
                id=job_id, args=[task_copy, False], replace_existing=True,
                max_instances=1, coalesce=True
            )
        elif interval in SCHEDULE_INTERVALS:
            app_scheduler.add_job(
                rclone_worker, "interval", minutes=SCHEDULE_INTERVALS[interval],
                id=job_id, args=[task_copy, False], replace_existing=True,
                max_instances=1, coalesce=True
            )
        else:
            print(f"Intervalo inválido para a tarefa {tid}: {interval}")

async def refresh_automation_services():
    """Aplica imediatamente uma alteração de licença aos processos automáticos."""
    if is_license_active():
        sync_realtime_watchers(load_tasks())
        sync_scheduled_tasks(load_tasks())
        asyncio.create_task(poll_realtime_download_tasks())
        if app_scheduler.running and not app_scheduler.get_job("remote-realtime-poll"):
            app_scheduler.add_job(
                poll_realtime_download_tasks, 'interval', seconds=REMOTE_POLL_SECONDS,
                id='remote-realtime-poll', max_instances=1, coalesce=True
            )
    else:
        stop_all_realtime_watchers()
        sync_scheduled_tasks([])
        if app_scheduler.get_job("remote-realtime-poll"):
            app_scheduler.remove_job("remote-realtime-poll")
        # Se a licença for removida durante uma cópia, termina-a também.
        for tid, proc in list(PROCESSES.items()):
            try:
                proc.terminate()
                STATE["running"][tid] = "idle"
                STATE["active_files"][tid] = []
            except ProcessLookupError:
                pass

      


import shutil

def bootstrap_folders():
    # Pastas onde o código está guardado internamente na imagem
    SOURCE_APP = "/app_dist"
    SOURCE_WWW = "/www_dist"
    
    # Destinos (Volumes montados no ZimaOS)
    TARGET_APP = "/app"
    TARGET_WWW = "/www"
    TARGET_CONFIG = "/config"

    # Criar pasta config se não existir
    os.makedirs(TARGET_CONFIG, exist_ok=True)

    # Se a pasta /app estiver vazia (ou sem o main.py), copia os ficheiros
    if not os.path.exists(os.path.join(TARGET_APP, "main.py")):
        print("A inicializar pasta /app no host...")
        os.makedirs(TARGET_APP, exist_ok=True)
        for item in os.listdir(SOURCE_APP):
            s = os.path.join(SOURCE_APP, item)
            d = os.path.join(TARGET_APP, item)
            if os.path.isfile(s): shutil.copy2(s, d)

    # Isto vai atualizar o frontend em todos os arranques:
        print(">>> A atualizar frontend no volume do ZimaOS...")
        shutil.copytree(src_www, dst_www, dirs_exist_ok=True)
        os.system(f"chmod -R 777 {dst_www}")

# EXECUTAR O BOOTSTRAP ANTES DE QUALQUER OUTRA COISA
bootstrap_folders()



# --- WATCHDOG / TAREFAS EM TEMPO REAL ---

if HAS_WATCHDOG:
    class RealtimeTaskHandler(FileSystemEventHandler):
        def __init__(self, task):
            super().__init__()
            self.task = dict(task)

        def on_any_event(self, event):
            if event.is_directory or event.event_type not in {"created", "modified", "deleted", "moved"}:
                return
            schedule_realtime_sync(self.task)

def schedule_realtime_sync(task):
    """Recebe eventos da thread do watchdog e agenda-os no loop FastAPI."""
    if not is_license_active() or not APP_LOOP or APP_LOOP.is_closed():
        return
    APP_LOOP.call_soon_threadsafe(_debounce_realtime_sync, dict(task))

def _debounce_realtime_sync(task):
    tid = str(task["id"])
    previous_handle = REALTIME_HANDLES.pop(tid, None)
    if previous_handle:
        previous_handle.cancel()
    REALTIME_HANDLES[tid] = APP_LOOP.call_later(
        REALTIME_DEBOUNCE_SECONDS,
        lambda: asyncio.create_task(_run_realtime_sync(task))
    )

async def _run_realtime_sync(task):
    if not is_license_active():
        return
    tid = str(task["id"])
    REALTIME_HANDLES.pop(tid, None)
    task_lock = TASK_LOCKS.get(tid)
    if task_lock and task_lock.locked():
        # Não perder alterações recebidas enquanto outra sincronização termina.
        _debounce_realtime_sync(task)
        return
    await rclone_worker(task)

def stop_realtime_watcher(tid):
    watcher = WATCHERS.pop(tid, None)
    if not watcher:
        return
    observer = watcher["observer"]
    observer.stop()
    observer.join(timeout=2)

def sync_realtime_watchers(tasks):
    """Recria os observers de acordo com as tarefas configuradas em Tempo Real."""
    if not is_license_active():
        stop_all_realtime_watchers()
        return
    # Uma tarefa editada deve criar uma nova referência remota no próximo polling.
    CLOUD_STATE_CACHE.clear()
    if not HAS_WATCHDOG:
        print("Watchdog não está instalado; Tempo Real Local→Cloud está indisponível.")
        return

    for tid in list(WATCHERS):
        stop_realtime_watcher(tid)

    for task in tasks:
        # Cloud→Local é monitorizado por polling remoto, para não criar ciclos
        # quando os próprios downloads alteram a pasta local.
        if task.get("trigger") != "real" or task.get("type") == "download":
            continue
        tid = str(task.get("id", ""))
        local_path = str(task.get("local", "")).strip()
        if not tid or not os.path.isdir(local_path):
            print(f"Não foi possível vigiar a tarefa {tid}: pasta local inválida ({local_path}).")
            continue
        try:
            observer = Observer()
            observer.schedule(RealtimeTaskHandler(task), local_path, recursive=True)
            observer.start()
            WATCHERS[tid] = {"observer": observer, "path": os.path.abspath(local_path)}
            print(f"Watchdog ativo para a tarefa {tid}: {local_path}")
        except Exception as e:
            print(f"Erro ao iniciar watchdog para a tarefa {tid}: {e}")

async def get_remote_snapshot(remote):
    """Obtém uma assinatura estável dos ficheiros de uma cloud sem os transferir."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "--config", RCLONE_CONFIG, "lsjson", "--recursive", "--files-only", remote,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            print(f"Timeout ao verificar a cloud {remote}.")
            return None

        if proc.returncode != 0:
            print(f"Erro ao verificar a cloud {remote}: {stderr.decode('utf-8', errors='ignore').strip()}")
            return None

        entries = json.loads(stdout.decode("utf-8", errors="ignore"))
        if not isinstance(entries, list):
            return None
        return tuple(sorted(
            (
                str(entry.get("Path", "")),
                entry.get("Size"),
                entry.get("ModTime"),
                json.dumps(entry.get("Hashes", {}), sort_keys=True)
            )
            for entry in entries
            if isinstance(entry, dict) and not entry.get("IsDir", False)
        ))
    except Exception as e:
        print(f"Erro ao criar inventário remoto de {remote}: {e}")
        return None

async def poll_remote_task(task):
    if not is_license_active():
        return
    """Deteta alterações na cloud de uma tarefa Cloud→Local e agenda a sincronização."""
    tid = str(task["id"])
    snapshot = await get_remote_snapshot(task["remote"])
    if snapshot is None:
        return

    previous_snapshot = CLOUD_STATE_CACHE.get(tid)
    CLOUD_STATE_CACHE[tid] = snapshot
    if previous_snapshot is None:
        print(f"Polling remoto ativo para a tarefa {tid}.")
    elif previous_snapshot != snapshot:
        print(f"Alteração remota detetada na tarefa {tid}; a sincronização será iniciada.")
        schedule_realtime_sync(task)

async def poll_realtime_download_tasks():
    if not is_license_active():
        return
    # Agora incluímos tanto o tipo "download" como o "bisync"
    tasks = [
        task for task in load_tasks()
        if task.get("trigger") == "real" and task.get("type") in ["download", "bisync"]
    ]
    if tasks:
        await asyncio.gather(*(poll_remote_task(task) for task in tasks), return_exceptions=True)

# --- AUXILIARES ---



def save_tasks(tasks):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:
        print(f"Erro ao gravar tarefas: {e}")
        return False



def save_settings(data):
    """Grava as definições e força a escrita física no disco."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno()) 
    except Exception as e:
        print(f"Erro ao gravar settings: {e}")



def clean_log_line(text):
    if not text: return ""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    if '\r' in text: text = text.split('\r')[-1]
    return text.strip()

def parse_size_to_bytes(size_str):
    if not size_str: return 0
    s = str(size_str).lower().replace('i', '').strip()
    units = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    match = re.search(r"([\d.]+)\s*([a-zA-Z]?)", s)
    if not match: return 0
    val, unit = match.groups()
    return int(float(val) * units.get(unit, 1))

def format_bytes(n):
    if not n or n == 0: return "0.00 B"
    n_float = float(n)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n_float < 1024: return f"{n_float:.2f} {unit}"
        n_float /= 1024
    return f"{n_float:.2f} PB"

def match_rclone_file(log_path, candidates):
    """Devolve um ficheiro conhecido apenas quando a correspondência é inequívoca."""
    normalized_log_path = str(log_path).replace("\\", "/").strip().strip("/").lower()
    if not normalized_log_path:
        return None

    candidate_list = list(candidates)
    exact_matches = [
        f for f in candidate_list
        if f.replace("\\", "/").strip().strip("/").lower() == normalized_log_path
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    suffix_matches = []
    for f in candidate_list:
        normalized_file = f.replace("\\", "/").strip().strip("/").lower()
        if (
            normalized_log_path.endswith("/" + normalized_file)
            or normalized_file.endswith("/" + normalized_log_path)
        ):
            suffix_matches.append(f)
    return suffix_matches[0] if len(suffix_matches) == 1 else None

def mark_file_finished(tid, file_name):
    """Atualiza o estado sem duplicar linhas nem deixar o ficheiro como pendente."""
    if file_name not in STATE["finished_files"][tid]:
        STATE["finished_files"][tid].append(file_name)
    if file_name in STATE["skipped_files"][tid]:
        STATE["skipped_files"][tid].remove(file_name)

def mark_file_failed(tid, file_name):
    """Marca um ficheiro como falhado e remove estados incompatíveis."""
    if not file_name:
        return
    if file_name not in STATE["failed_files"][tid]:
        STATE["failed_files"][tid].append(file_name)
    if file_name in STATE["finished_files"][tid]:
        STATE["finished_files"][tid].remove(file_name)
    if file_name in STATE["skipped_files"][tid]:
        STATE["skipped_files"][tid].remove(file_name)
    STATE["active_files"][tid] = [
        item for item in STATE["active_files"][tid]
        if item.get("name") != file_name
    ]

def find_rclone_error_file(message, candidates):
    """Extrai o caminho de erros rclone, incluindo os de limite de tamanho."""
    error_match = re.search(
        r"ERROR\s*:\s*(.+?):\s*(?:Failed|failed|.*(?:too large|file size|size limit|maximum size))",
        message,
        re.IGNORECASE
    )
    return match_rclone_file(error_match.group(1), candidates) if error_match else None

def remove_root_ghosts(paths):
    """Remove entradas da raiz duplicadas por uma versão dentro de uma pasta."""
    unique_paths = {
        str(path).replace("\\", "/").strip().strip("/")
        for path in paths
        if str(path).strip().strip("/")
    }
    nested_names = {
        path.rsplit("/", 1)[-1].lower()
        for path in unique_paths
        if "/" in path
    }
    return sorted(
        path for path in unique_paths
        if "/" in path or path.lower() not in nested_names
    )

def save_history(task_id, entry):
    history = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: history = json.load(f)
        except: history = {}
    tid = str(task_id)
    if tid not in history: history[tid] = []
    history[tid] = ([entry] + history[tid])[:50]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"Erro ao gravar histórico: {e}")

def update_last_sync(task_id, timestamp):
    tasks = load_tasks()
    for task in tasks:
        if str(task.get("id")) == str(task_id):
            task["last_sync"] = timestamp
            save_tasks(tasks)
            break

def set_bisync_initialized(task_id):
    """Só marca o resync inicial como concluído depois de um bisync bem-sucedido."""
    tasks = load_tasks()
    for task in tasks:
        if str(task.get("id")) == str(task_id) and task.get("type") == "bisync":
            task["bisync_initialized"] = True
            save_tasks(tasks)
            break

def get_bisync_workdir(task_id):
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    workdir = os.path.join(BISYNC_WORKDIR, safe_task_id)
    os.makedirs(workdir, exist_ok=True)
    return workdir

async def list_rclone_files(path):
    """Lista caminhos relativos, quer a origem seja local ou cloud."""
    proc = await asyncio.create_subprocess_exec(
        "rclone", "--config", RCLONE_CONFIG, "lsf", "-R", "--files-only", path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"Não foi possível listar {path}: {detail}")
    return {
        line.strip().strip("/")
        for line in stdout.decode("utf-8", errors="ignore").splitlines()
        if line.strip().strip("/")
    }

async def list_bisync_files(local_path, remote_path):
    """Une as duas árvores para a fila mostrar cada ficheiro apenas uma vez."""
    local_files, remote_files = await asyncio.gather(
        list_rclone_files(local_path),
        list_rclone_files(remote_path)
    )
    return remove_root_ghosts(local_files | remote_files)

def build_bisync_command(task, tid, remote_type, dry_run=False):
    """Prepara um bisync persistente e recuperável; o resync ocorre apenas na primeira vez."""
    cmd = [
        "rclone", "--config", RCLONE_CONFIG, "bisync", task["local"], task["remote"],
        "-P", "-v", "--stats", "1s", "--stats-file-name-length", "0",
        "--transfers", "1", "--checkers", "1", "--multi-thread-streams", "0",
        "--create-empty-src-dirs", "--resilient", "--recover",
        "--workdir", get_bisync_workdir(tid)
    ]
    if not task.get("bisync_initialized", False):
        # Na primeira execução, conserva a versão mais recente em vez de preferir sempre a local.
        cmd += ["--resync", "--resync-mode", "newer"]
    if dry_run:
        cmd.append("--dry-run")
    if remote_type == "onedrive":
        cmd += ["--onedrive-chunk-size", "10M"]
    return cmd

# --- MOTOR DE EXECUÇÃO ---

async def dryrun_step(src, dst, tid, sorted_files):
    """Simulação: Apenas identifica quais ficheiros da lista original serão transferidos."""
    to_transfer = set()
    total_bytes_anchor = 0
    
    # Reset: Começa com TUDO em 'skipped_files' (Laranja)
    STATE["skipped_files"][tid] = list(sorted_files)
    STATE["file_sizes"][tid] = {}

    cmd = ["rclone", "--config", RCLONE_CONFIG, "copy", src, dst, "-v", "--dry-run", "--update", "--modify-window", "2s", "--stats", "1s"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes: break 
        msg = clean_log_line(line_bytes.decode('utf-8', errors='ignore'))
        if not msg: continue
        STATE["logs"][tid] = [msg] + STATE["logs"][tid][:49]

        if "Skipped copy as --dry-run" in msg and "NOTICE:" in msg:
            try:
                # Isola o caminho do log e normaliza
                path_from_log = msg.split("NOTICE:")[1].split(": Skipped copy")[0].strip().strip('/')
                
                # Match inteligente com a lista LSF original
                for f in sorted_files:
                    f_norm = f.strip('/')
                    # Se o caminho é igual OU o ficheiro da lista termina com o nome do log
                    if f_norm.lower() == path_from_log.lower() or f_norm.lower().endswith("/" + path_from_log.lower()):
                        if f in STATE["skipped_files"][tid]:
                            STATE["skipped_files"][tid].remove(f) # Sai do laranja (Sem Alteração)
                        to_transfer.add(f)
                        if "(size " in msg:
                            sz_raw = msg.split("(size ")[1].split(')')[0]
                            STATE["file_sizes"][tid][f] = format_bytes(parse_size_to_bytes(sz_raw))
                        break
            except: pass

        if "Transferred:" in msg and "/" in msg:
            try:
                total_raw = msg.split('/')[1].split(',')[0].strip()
                if any(u in total_raw.upper() for u in ["B", "K", "M", "G"]):
                    total_bytes_anchor = parse_size_to_bytes(total_raw)
            except: pass
        
        await manager.broadcast({"type": "update", "state": STATE})
    return_code = await proc.wait()
    return to_transfer, total_bytes_anchor, return_code == 0

def find_bisync_log_file(message, candidates):
    """Resolve os formatos de delta, cópia e dry-run emitidos pelo rclone bisync."""
    patterns = [
        r"(?:NOTICE|INFO)\s*:\s*(.+?):\s*Skipped copy as --dry-run",
        r"INFO\s*:\s*(.+?):\s*(?:Copied|Moved|Updated)\b",
        r"(?:File is (?:new|newer|changed)|File was deleted)\s*-\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            matched_file = match_rclone_file(match.group(1), candidates)
            if matched_file:
                return matched_file
    return None

async def bisync_dryrun_step(task, tid, sorted_files, remote_type):
    """Mostra o plano do bisync nativo sem gravar alterações nem o estado de resync."""
    transfer_candidates = set()
    STATE["skipped_files"][tid] = list(sorted_files)
    STATE["file_sizes"][tid] = {}

    proc = await asyncio.create_subprocess_exec(
        *build_bisync_command(task, tid, remote_type, dry_run=True),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    buffer, last_ws_update = "", 0
    while True:
        chunk = await proc.stdout.read(1024)
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="ignore")
        if "\n" not in buffer and "\r" not in buffer:
            continue
        lines = re.split(r"[\r\n]+", buffer)
        buffer = lines.pop()
        for line in lines:
            msg = clean_log_line(line)
            if not msg:
                continue
            msg_low = msg.lower()
            if any(token in msg for token in ["INFO", "NOTICE", "ERROR", "*"]):
                STATE["logs"][tid] = [msg] + STATE["logs"][tid][:49]

            if "ERROR" in msg:
                failed_file = find_rclone_error_file(msg, sorted_files)
                if failed_file:
                    mark_file_failed(tid, failed_file)
                continue

            candidate = find_bisync_log_file(msg, sorted_files)
            if candidate:
                transfer_candidates.add(candidate)
                if candidate in STATE["skipped_files"][tid]:
                    STATE["skipped_files"][tid].remove(candidate)
                size_match = re.search(r"\(size\s+([^\)]+)\)", msg, re.IGNORECASE)
                if size_match:
                    STATE["file_sizes"][tid][candidate] = format_bytes(parse_size_to_bytes(size_match.group(1)))

        now = time.time()
        if now - last_ws_update > 0.3:
            await manager.broadcast({"type": "update", "state": STATE})
            last_ws_update = now

    return transfer_candidates, (await proc.wait()) == 0

async def native_bisync_step(task, tid, sorted_files, remote_type, planned_files=None):
    """Executa o rclone bisync nativo preservando lista, progresso e cores da fila."""
    transfer_candidates = set(planned_files or [])
    if planned_files is None:
        # Sem pré-simulação, presume inicialmente que os ficheiros sem delta ficam inalterados.
        STATE["skipped_files"][tid] = list(sorted_files)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_path = os.path.join(LOGS_DIR, f"debug_bisync_{tid}_{timestamp}.txt")
    current_fn, current_p = None, 0
    calibrated_total, last_ws_update, buffer, return_code = 0.0, 0, "", 1

    try:
        with open(debug_path, "w", encoding="utf-8") as f_log:
            cmd = build_bisync_command(task, tid, remote_type)
            f_log.write(f"=== INICIO BISYNC NATIVO ===\nData: {datetime.now()} | Tarefa: {tid}\n")
            f_log.write(f"Comando: {' '.join(cmd)}\n{'-' * 50}\n\n")
            f_log.flush()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            PROCESSES[tid] = proc

            while True:
                chunk = await proc.stdout.read(1024)
                if not chunk:
                    break
                raw_text = chunk.decode("utf-8", errors="ignore")
                f_log.write(raw_text)
                f_log.flush()
                buffer += raw_text

                if "\n" in buffer or "\r" in buffer:
                    lines = re.split(r"[\r\n]+", buffer)
                    buffer = lines.pop()
                    for line in lines:
                        msg = clean_log_line(line)
                        if not msg:
                            continue
                        msg_low = msg.lower()
                        if any(token in msg for token in ["INFO", "NOTICE", "DEBUG", "ERROR", "*"]):
                            STATE["logs"][tid] = [msg] + STATE["logs"][tid][:49]

                        if "ERROR" in msg:
                            failed_file = find_rclone_error_file(msg, sorted_files)
                            if not failed_file and any(term in msg_low for term in ["too large", "file size", "size limit", "maximum size"]):
                                failed_file = current_fn
                            if failed_file:
                                mark_file_failed(tid, failed_file)
                                if failed_file == current_fn:
                                    current_fn, current_p = None, 0
                            continue

                        candidate = find_bisync_log_file(msg, sorted_files)
                        if candidate:
                            transfer_candidates.add(candidate)
                            if candidate in STATE["skipped_files"][tid]:
                                STATE["skipped_files"][tid].remove(candidate)
                            if "copied" in msg_low or "moved" in msg_low or "updated" in msg_low:
                                mark_file_finished(tid, candidate)
                                if candidate == current_fn:
                                    current_fn, current_p = None, 0

                        if msg.startswith("*"):
                            percent_match = re.search(r"(\d+)%", msg)
                            if percent_match:
                                filename_from_log = msg[1:].split(":", 1)[0].strip()
                                active_file = match_rclone_file(filename_from_log, sorted_files)
                                if not active_file:
                                    active_file = next((f for f in sorted_files if f.lower().strip("/") in msg_low), None)
                                if active_file:
                                    transfer_candidates.add(active_file)
                                    if active_file in STATE["skipped_files"][tid]:
                                        STATE["skipped_files"][tid].remove(active_file)
                                    current_fn, current_p = active_file, int(percent_match.group(1))

                        # O resumo de progresso do rclone pode não ter o prefixo "*";
                        # aceitar ambos os formatos mantém a barra geral atualizada no bisync.
                        if "ETA" in msg and "Transferred:" in msg and "/" in msg:
                            try:
                                transferred_part = msg.split("Transferred:", 1)[1]
                                bytes_done = float(parse_size_to_bytes(transferred_part.split("/", 1)[0].strip()))
                                bytes_total = float(parse_size_to_bytes(transferred_part.split("/", 1)[1].split(",", 1)[0].strip()))
                                calibrated_total = max(calibrated_total, bytes_total)
                                if calibrated_total > 0:
                                    percent = min((bytes_done / calibrated_total) * 100, 100)
                                    STATE["stats"][tid] = {
                                        "transferred": format_bytes(bytes_done),
                                        "total": format_bytes(calibrated_total),
                                        "percent": round(percent, 2)
                                    }
                            except (IndexError, ValueError):
                                pass

                if current_fn:
                    STATE["active_files"][tid] = [{"name": current_fn, "progress": current_p}]
                now = time.time()
                if now - last_ws_update > 0.3:
                    await manager.broadcast({"type": "update", "state": STATE})
                    last_ws_update = now

            return_code = await proc.wait()
            f_log.write(f"\n--- FIM DO BISYNC: {datetime.now()} | retorno {return_code} ---\n")

        if return_code == 0:
            for file_name in transfer_candidates:
                if file_name not in STATE["failed_files"][tid]:
                    mark_file_finished(tid, file_name)
            STATE["active_files"][tid] = []
            set_bisync_initialized(tid)
            await manager.broadcast({"type": "update", "state": STATE})
        elif current_fn:
            mark_file_failed(tid, current_fn)
    except Exception as e:
        STATE["logs"][tid].insert(0, f"ERROR: {e}")
    finally:
        PROCESSES.pop(tid, None)

    return return_code == 0

async def native_bisync_with_preflight(task, tid, sorted_files, remote_type):
    STATE["running"][tid] = "simulating"
    planned_files, dryrun_succeeded = await bisync_dryrun_step(task, tid, sorted_files, remote_type)
    if not dryrun_succeeded:
        return False
    STATE["running"][tid] = "active"
    await manager.broadcast({"type": "update", "state": STATE})
    return await native_bisync_step(task, tid, sorted_files, remote_type, planned_files)

async def real_copy_step_sim(src, dst, tid, sorted_files, r_type, phase_label):
    """Cópia Real: Match rigoroso contra a lista original para evitar duplicados."""
    STATE["running"][tid] = "simulating"
    to_transfer, anchor_total, dryrun_succeeded = await dryrun_step(src, dst, tid, sorted_files)
    transfer_candidates = set(to_transfer)
    # O dry-run fornece a melhor estimativa inicial do volume a transferir.
    # Estas variáveis são usadas pelo cálculo de progresso abaixo.
    calibrated_total = float(anchor_total)
    offset_bytes = 0
    
    if not dryrun_succeeded:
        return False

    if not to_transfer:
        STATE["running"][tid] = "idle"
        await manager.broadcast({"type": "init", "tasks": load_tasks(), "state": STATE})
        return True

    STATE["running"][tid] = "active"
    cmd = ["rclone", "--config", RCLONE_CONFIG, "copy", src, dst, "-P", "-v", "--update", "--modify-window", "2s", "--stats", "1s", "--stats-file-name-length", "0", "--transfers", "1", "--checkers", "1", "--multi-thread-streams", "0"]
    if r_type == "onedrive": cmd += ["--onedrive-chunk-size", "10M"]

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    PROCESSES[tid] = proc
    last_ws_update, buffer, current_fn, current_p = 0, "", None, 0

    while True:
        chunk = await proc.stdout.read(1024)
        if not chunk: break 
        buffer += chunk.decode('utf-8', errors='ignore')
        if '\n' in buffer or '\r' in buffer:
            lines = re.split(r'[\r\n]+', buffer)
            buffer = lines.pop()
            for line in lines:
                msg = clean_log_line(line)
                if not msg: continue
                msg_low = msg.lower()
                if any(x in msg for x in ["INFO", "ERROR", "Transferred:", "*"]): STATE["logs"][tid] = [msg] + STATE["logs"][tid][:49]

                if "ERROR" in msg:
                    failed_file = find_rclone_error_file(msg, transfer_candidates)
                    if not failed_file and any(term in msg_low for term in ["too large", "file size", "size limit", "maximum size"]):
                        failed_file = current_fn
                    if failed_file:
                        mark_file_failed(tid, failed_file)
                        if failed_file == current_fn:
                            current_fn, current_p = None, 0
                    continue

                # A. SUCESSO (Verde)
                if "info" in msg_low and any(x in msg_low for x in [": copied", ": moved", ": updated"]):
                    copied_match = re.search(
                        r"INFO\s*:\s*(.+):\s*(?:Copied|Moved|Updated)\b",
                        msg,
                        re.IGNORECASE
                    )
                    if copied_match:
                        copied_file = match_rclone_file(copied_match.group(1), transfer_candidates)
                        if copied_file:
                            mark_file_finished(tid, copied_file)
                            STATE["active_files"][tid] = []; current_fn, current_p = None, 0
                    continue

                # B. AZUL (Ativo)
                if msg.startswith("*"):
                    p_match = re.search(r"(\d+)%", msg)
                    if p_match:
                        perc_val = int(p_match.group(1))
                        for f in sorted_files:
                            f_clean = f.lower().strip('/')
                            if f_clean in msg_low or f_clean.split('/')[-1] in msg_low:
                                if tid in STATE["skipped_files"] and f in STATE["skipped_files"][tid]: STATE["skipped_files"][tid].remove(f)
                                current_fn, current_p = f, perc_val
                                break

                # C. BARRA GERAL (Tua Fórmula)
                if "*" in msg and "ETA" in msg and "Transferred:" in msg:
                            try:
                                part_after = msg.split("Transferred:")[1]
                                val_done_raw = part_after.split('/')[0].strip()
                                val_total_raw = part_after.split('/')[1].split(',')[0].strip()
                                
                                bytes_done = float(parse_size_to_bytes(val_done_raw))
                                bytes_total = float(parse_size_to_bytes(val_total_raw))
                                
                                if bytes_total > calibrated_total: calibrated_total = bytes_total

                                if calibrated_total > 0 and bytes_done >= 0:
                                    total_acumulado = float(offset_bytes) + bytes_done
                                    # TUA FÓRMULA: 100 / (Total / Feito)
                                    manual_p = 100.0 / (calibrated_total / total_acumulado) if total_acumulado > 0 else 0
                                    
                                    old_p = STATE["stats"].get(tid, {}).get("percent", 0.0)
                                    p_final = max(manual_p, old_p)
                                    if p_final >= 100 and total_acumulado < calibrated_total: p_final = 99.98

                                    STATE["stats"][tid] = {
                                        "transferred": format_bytes(total_acumulado),
                                        "total": format_bytes(calibrated_total),
                                        "percent": round(min(p_final, 100.0), 2)
                                    }
                            except: pass

        if current_fn: STATE["active_files"][tid] = [{"name": current_fn, "progress": current_p}]
        now = time.time()
        if now - last_ws_update > 0.3:
            await manager.broadcast({"type": "update", "state": STATE})
            last_ws_update = now
    return_code = await proc.wait()
    if return_code == 0:
        # Um rclone terminado sem erros confirma todos os ficheiros escolhidos no dry-run.
        # Assim, diferenças de formatação nos logs não deixam itens concluídos a branco.
        for f in transfer_candidates:
            mark_file_finished(tid, f)
        STATE["active_files"][tid] = []
        await manager.broadcast({"type": "update", "state": STATE})
    elif current_fn:
        mark_file_failed(tid, current_fn)
    if tid in PROCESSES:
        del PROCESSES[tid]
    return return_code == 0


async def real_copy_step(src, dst, tid, sorted_files, r_type, phase_label, offset_bytes, grand_total_bytes):
    """
    Motor de Sincronização: Processa a cópia e atualiza o estado em tempo real via WebSockets.
    """
    STATE["logs"][tid].insert(0, f"--- INICIANDO: {phase_label} ---")

    cmd = [
        "rclone", "--config", RCLONE_CONFIG, "copy", src, dst, 
        "-vv", "-P", "--stats", "1s",
        "--transfers", "1", "--checkers", "1", "--multi-thread-streams", "0"
    ]
    if r_type == "onedrive": cmd += ["--onedrive-chunk-size", "10M"]

    last_ws_update, buffer = 0, ""
    current_fn, current_p = None, 0
    calibrated_total = float(grand_total_bytes)
    return_code = 1

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        PROCESSES[tid] = proc

        while True:
            chunk = await proc.stdout.read(1024)
            if not chunk: break 

            raw_text = chunk.decode('utf-8', errors='ignore')
            buffer += raw_text

            if '\n' in buffer or '\r' in buffer:
                lines = re.split(r'[\r\n]+', buffer)
                buffer = lines.pop()

                for line in lines:
                    msg = clean_log_line(line)
                    if not msg: continue
                    
                    # Log Visual na Consola
                    if any(x in msg for x in ["INFO", "DEBUG", "ERROR", "*"]):
                        STATE["logs"][tid] = [msg] + STATE["logs"][tid][:49]

                    msg_low = msg.lower()

                    if "ERROR" in msg:
                        failed_file = find_rclone_error_file(msg, sorted_files)
                        if not failed_file and any(term in msg_low for term in ["too large", "file size", "size limit", "maximum size"]):
                            failed_file = current_fn
                        if failed_file:
                            mark_file_failed(tid, failed_file)
                            if failed_file == current_fn:
                                current_fn, current_p = None, 0
                        continue

                    # LÓGICA: Ficheiros não alterados (Laranja)
                    if "DEBUG" in msg and "unchanged skipping" in msg_low:
                        try:
                            path_raw = msg.split("DEBUG : ")[1].split(": Unchanged skipping")[0].strip().strip('/')
                            for f in sorted_files:
                                if f.lower().strip('/') == path_raw.lower():
                                    if f not in STATE["skipped_files"][tid]: STATE["skipped_files"][tid].append(f)
                                    break
                        except: pass
                    
                    # LÓGICA: Barra de Progresso Geral
                    if "*" in msg and "ETA" in msg and "Transferred:" in msg:
                        try:
                            part_after = msg.split("Transferred:")[1]
                            val_done_raw = part_after.split('/')[0].strip()
                            val_total_raw = part_after.split('/')[1].split(',')[0].strip()
                            
                            bytes_done = float(parse_size_to_bytes(val_done_raw))
                            bytes_total = float(parse_size_to_bytes(val_total_raw))
                            
                            if bytes_total > calibrated_total: calibrated_total = bytes_total

                            if calibrated_total > 0 and bytes_done >= 0:
                                total_acumulado = float(offset_bytes) + bytes_done
                                manual_p = 100.0 / (calibrated_total / total_acumulado) if total_acumulado > 0 else 0
                                
                                old_p = STATE["stats"].get(tid, {}).get("percent", 0.0)
                                p_final = max(manual_p, old_p)
                                if p_final >= 100 and total_acumulado < calibrated_total: p_final = 99.98

                                STATE["stats"][tid] = {
                                    "transferred": format_bytes(total_acumulado),
                                    "total": format_bytes(calibrated_total),
                                    "percent": round(min(p_final, 100.0), 2)
                                }
                        except: pass

                    # LÓGICA: Ficheiro Ativo e Percentagem Individual
                    if "*" in msg and ":" in msg and "/" in msg:
                        try:
                            lado_esquerdo = msg.split("Transferred:")[0] if "transferred:" in msg_low else msg
                            partes_dois_pontos = lado_esquerdo.split(":")
                            if len(partes_dois_pontos) >= 2:
                                nome_raw = partes_dois_pontos[0].replace("*", "").strip()
                                bloco_dados = partes_dois_pontos[-1]
                                partes_barra = bloco_dados.split("/")
                                if len(partes_barra) >= 2:
                                    perc_raw = partes_barra[0].replace("%", "").strip()
                                    peso_raw = partes_barra[1].split(",")[0].strip()

                                    clean_log_name = nome_raw.replace("…", "*").replace("...", "*")
                                    ancoras = [p.lower().strip() for p in clean_log_name.split("*") if len(p.strip()) > 2]

                                    target_file = None
                                    for f in sorted_files:
                                        f_low = f.lower()
                                        match_confirmado = True
                                        ultima_posicao = 0
                                        for ancora in ancoras:
                                            posicao = f_low.find(ancora, ultima_posicao)
                                            if posicao == -1:
                                                match_confirmado = False
                                                break
                                            ultima_posicao = posicao + len(ancora)
                                        if match_confirmado:
                                            target_file = f
                                            break

                                    if target_file:
                                        current_fn = target_file
                                        current_p = int(perc_raw)
                                        if tid not in STATE["file_sizes"]: STATE["file_sizes"][tid] = {}
                                        STATE["file_sizes"][tid][target_file] = format_bytes(parse_size_to_bytes(peso_raw))
                        except: pass

                    # LÓGICA: Concluído com Sucesso (Verde)
                    if "INFO" in msg and "copied" in msg_low:
                        try:
                            path_raw = msg.split("INFO  :")[1].split(": Copied")[0].strip().strip('/')
                            for f in sorted_files:
                                if f.lower().strip('/') == path_raw.lower():
                                    if f not in STATE["finished_files"][tid]: STATE["finished_files"][tid].append(f)
                                    if f in STATE["skipped_files"][tid]: STATE["skipped_files"][tid].remove(f)
                                    STATE["active_files"][tid] = []; current_fn, current_p = None, 0
                                    break
                        except: pass

            if current_fn:
                STATE["active_files"][tid] = [{"name": current_fn, "progress": current_p}]

            # Atualização via WebSocket
            now = time.time()
            if now - last_ws_update > 0.5:
                await manager.broadcast({"type": "update", "state": STATE})
                last_ws_update = now

        return_code = await proc.wait()
        if return_code != 0 and current_fn:
            mark_file_failed(tid, current_fn)

    except Exception as e:
        STATE["logs"][tid].insert(0, f"ERROR: {e}")

    if tid in PROCESSES: del PROCESSES[tid]
    return return_code == 0

async def rclone_worker(task, manual_simulate=False):
    # Simulações são permitidas sem licença; qualquer cópia/sincronização real
    # (incluindo chamadas vindas de watchdog/polling) exige ativação válida.
    if not manual_simulate and not is_license_active():
        print("Sincronização bloqueada: licença inativa.")
        return
    tid = str(task['id'])
    if tid not in TASK_LOCKS: TASK_LOCKS[tid] = asyncio.Lock()
    if TASK_LOCKS[tid].locked(): return
    async with TASK_LOCKS[tid]:
        # Reset total inicial
        for key in ["failed_files", "active_files", "finished_files", "skipped_files", "logs", "file_sizes"]:
            STATE[key][tid] = {} if key == "file_sizes" else []
        STATE["stats"][tid] = {"transferred": "0.00 B", "total": "---", "percent": 0.0}
        STATE["running"][tid] = "active"
        await manager.broadcast({"type": "update", "state": STATE})

        operation_type = "Simulação" if manual_simulate else "Sincronização"
        operation_succeeded = False
        try:
            r_type = get_remote_type(task['remote'].split(":")[0])
            if task.get('type') == 'bisync':
                # A fila mostra a união dos dois lados, uma única vez por ficheiro.
                STATE["all_files"][tid] = await list_bisync_files(task['local'], task['remote'])

                if manual_simulate:
                    STATE["running"][tid] = "simulating"
                    await manager.broadcast({"type": "update", "state": STATE})
                    _, operation_succeeded = await bisync_dryrun_step(
                        task, tid, STATE["all_files"][tid], r_type
                    )
                elif STATE.get("auto_simulate", True):
                    operation_succeeded = await native_bisync_with_preflight(
                        task, tid, STATE["all_files"][tid], r_type
                    )
                else:
                    operation_succeeded = await native_bisync_step(
                        task, tid, STATE["all_files"][tid], r_type
                    )
                # O finally abaixo ainda grava historico e atualiza a interface.
                return

            src, dst = (task['remote'], task['local']) if task.get('type') == 'download' else (task['local'], task['remote'])
            
            # Listagem (Única fonte de verdade para a lista visual)
            all_detected = set()
            lsf = await asyncio.create_subprocess_exec("rclone", "--config", RCLONE_CONFIG, "lsf", "-R", "--files-only", src, stdout=asyncio.subprocess.PIPE)
            out, _ = await lsf.communicate()
            for f in out.decode('utf-8', errors='ignore').split("\n"):
                clean_f = f.strip().strip('/')
                if clean_f: all_detected.add(clean_f)
            STATE["all_files"][tid] = remove_root_ghosts(all_detected)
            
            # O botão "Simular" nunca transfere ficheiros.
            if manual_simulate:
                STATE["running"][tid] = "simulating"
                await manager.broadcast({"type": "update", "state": STATE})
                _, _, operation_succeeded = await dryrun_step(src, dst, tid, STATE["all_files"][tid])

            # O botão "Iniciar" usa ou não uma pré-simulação conforme a preferência.
            elif STATE.get("auto_simulate", True):
                operation_succeeded = await real_copy_step_sim(
                    src, dst, tid, STATE["all_files"][tid], r_type, "Sincronização"
                )
            else:
                operation_succeeded = await real_copy_step(
                    src, dst, tid, STATE["all_files"][tid], r_type,
                    "Sincronização", offset_bytes=0, grand_total_bytes=0
                )
        except Exception as e:
            STATE["logs"][tid].insert(0, f"ERROR: {e}")
        finally:
            completed_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            save_history(tid, {
                "date": completed_at,
                "type": operation_type,
                "mode": task.get("type", "upload"),
                "status": "Sucesso" if operation_succeeded else "Erro",
                "log": "\n".join(STATE["logs"].get(tid, [])[:50])
            })
            if operation_succeeded and not manual_simulate:
                update_last_sync(tid, completed_at)
            STATE["running"][tid] = "idle"
            STATE["active_files"][tid] = []
            await manager.broadcast({"type": "init", "tasks": load_tasks(), "state": STATE})

# --- ENDPOINTS E SERVICES ---

@app.get("/api/browse/local")
def browse_local_endpoint(path: str = "/mnt"):
    """Lista apenas pastas do sistema local."""
    if not path or path == "undefined":
        path = "/mnt"
    
    try:
        if not os.path.exists(path):
            return []
            
        entries = []
        # os.scandir é mais eficiente que os.listdir
        with os.scandir(path) as it:
            for entry in it:
                # FILTRO CRÍTICO: Apenas se for diretório e não for oculto
                if entry.is_dir() and not entry.name.startswith('.'):
                    entries.append({"name": entry.name, "is_dir": True})
        
        # Ordenar alfabeticamente
        return sorted(entries, key=lambda x: x["name"].lower())
    except Exception as e:
        print(f"Erro ao navegar localmente em {path}: {e}")
        return []

@app.get("/api/browse/remotes")
def list_remotes_endpoint():
    """Lista os nomes das clouds configuradas no rclone.conf"""
    try:
        # Adicionado o --config RCLONE_CONFIG para ele encontrar os teus remotes
        out = subprocess.check_output(["rclone", "--config", RCLONE_CONFIG, "listremotes"]).decode("utf-8")
        return [l.strip().replace(":", "") for l in out.split("\n") if l.strip()]
    except Exception as e:
        print(f"Erro ao listar remotes: {e}")
        return []

@app.get("/api/browse/cloud")
def browse_cloud_endpoint(remote: str, path: str = ""):
    """Lista as pastas dentro de uma cloud específica."""
    try:
        # Garante que o nome do remote tem os dois pontos no final
        remote_name = remote.replace(":", "") + ":"
        res = subprocess.check_output([
            "rclone", "--config", RCLONE_CONFIG, "lsd", 
            f"{remote_name}{path.lstrip('/')}"
        ]).decode("utf-8")
        # Extrai o nome da pasta (o rclone lsd tem um formato fixo)
        return [line.split(None, 4)[4] for line in res.split("\n") if line.strip() and len(line.split(None, 4)) >= 5]
    except Exception as e:
        print(f"Erro ao navegar na cloud {remote}: {e}")
        return []

def get_remote_type(remote_name):
    try:
        res = subprocess.check_output(["rclone", "--config", RCLONE_CONFIG, "listremotes", "--long"]).decode()
        for line in res.split("\n"):
            if line.startswith(remote_name.replace(":", "") + ":"): return line.split(":")[1].strip().lower()
    except: pass
    return "unknown"

async def check_single_remote(remote_name):
    try:
        proc = await asyncio.create_subprocess_exec("rclone", "--config", RCLONE_CONFIG, "lsd", f"{remote_name}:", "--max-depth", "1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=7.0)
        if proc.returncode == 0: return {"name": remote_name, "status": "online", "message": "OK"}
        return {"name": remote_name, "status": "offline", "message": "Erro de Token/Acesso"}
    except: return {"name": remote_name, "status": "offline", "message": "Timeout"}

async def update_health_cache():
    global HEALTH_CACHE
    try:
        out = subprocess.check_output(["rclone", "--config", RCLONE_CONFIG, "listremotes"]).decode("utf-8")
        remotes = [x.strip().replace(":", "") for x in out.split("\n") if x.strip()]
        HEALTH_CACHE = await asyncio.gather(*[check_single_remote(r) for r in remotes])
        await manager.broadcast({"type": "health_update", "health": HEALTH_CACHE})
    except: pass



@app.post("/api/tasks")
async def post_tasks(request: Request):
    """Cria, edita ou remove tarefas e propaga a lista atualizada à interface."""
    try:
        tasks = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "JSON inválido."})

    if not isinstance(tasks, list) or any(not isinstance(task, dict) or "id" not in task for task in tasks):
        return JSONResponse(status_code=400, content={"message": "Lista de tarefas inválida."})

    # The bisync database is tied to one local/remote pair. Editing either side
    # requires a new safe reconciliation only for that task.
    previous_tasks = {str(item.get("id")): item for item in load_tasks()}
    for task in tasks:
        previous = previous_tasks.get(str(task["id"]))
        if task.get("type") != "bisync":
            task.pop("bisync_initialized", None)
        elif (
            not previous
            or previous.get("type") != "bisync"
            or previous.get("local") != task.get("local")
            or previous.get("remote") != task.get("remote")
        ):
            task["bisync_initialized"] = False

    if not save_tasks(tasks):
        return JSONResponse(status_code=500, content={"message": "Não foi possível gravar as tarefas."})

    sync_realtime_watchers(tasks)
    sync_scheduled_tasks(tasks)
    await manager.broadcast({"type": "init", "tasks": tasks, "state": STATE})
    return {"status": "ok", "tasks": tasks}

@app.post("/api/settings/legacy")
async def post_settings(request: Request):
    try:
        data = await request.json()
        current = load_settings()
        current.update(data)
        save_settings(current)
        
        # ATUALIZA A MEMÓRIA GLOBAL PARA O WEBSOCKET
        if "auto_simulate" in data:
            STATE["auto_simulate"] = data["auto_simulate"]
        if "terms_accepted" in data:
            STATE["terms_accepted"] = data["terms_accepted"]
            
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})

@app.get("/api/settings")
def get_settings():
    settings = load_settings()
    license_data = load_license()
    return {
        "auto_simulate": settings["auto_simulate"],
        "terms_accepted": settings["terms_accepted"],
        "license_email": license_data.get("email", ""),
        "license_active": is_license_active(),
        "license_info": {"plan": license_data.get("plan"), "device_name": license_data.get("device_name"), "activated_at": license_data.get("activated_at")}
    }



@app.post("/api/settings")
async def update_settings_endpoint(request: Request):
    try:
        new_data = await request.json()
        current = load_settings()
        allowed = {"auto_simulate", "terms_accepted"}
        current.update({key: value for key, value in new_data.items() if key in allowed})
        save_settings(current)
        
        # Sincroniza a memória global para o próximo sinal de WebSocket
        if "auto_simulate" in new_data:
            STATE["auto_simulate"] = new_data["auto_simulate"]
        if "terms_accepted" in new_data:
            STATE["terms_accepted"] = new_data["terms_accepted"]
        STATE["license_active"] = is_license_active()
        await refresh_automation_services()
        await manager.broadcast({"type": "update", "state": STATE})
            
        return {
            "status": "ok", "license_active": STATE["license_active"],
            "message": None, "plan": None, "code": None
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})

@app.post("/api/terms/accept")
async def accept_terms():
    s = load_settings()
    s["terms_accepted"] = True
    save_settings(s)
    # Atualiza memória global
    STATE["terms_accepted"] = True
    return {"status": "ok"}

@app.get("/api/history/{task_id}")
async def get_history(task_id: str):
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f: return json.load(f).get(task_id, [])
    return []

@app.post("/api/sync/{task_id}")
async def start_sync(task_id: str, bt: BackgroundTasks, simulate: bool = False):
    t = next((x for x in load_tasks() if str(x['id']) == task_id), None)
    if not t:
        return JSONResponse(status_code=404, content={"message": "Tarefa não encontrada."})
    if not simulate and not is_license_active():
        return JSONResponse(status_code=403, content={"message": "Ative a licença para iniciar sincronizações."})
    bt.add_task(rclone_worker, t, simulate)
    return {"status": "ok"}

@app.post("/api/sync/stop/{task_id}")
async def stop_sync(task_id: str):
    if not is_license_active():
        return JSONResponse(status_code=403, content={"message": "Ative a licença para controlar sincronizações."})
    """Pára a execução do Rclone de forma imediata."""
    if task_id in PROCESSES:
        try:
            proc = PROCESSES[task_id]
            # Envia sinal de terminação
            proc.terminate()
            # Aguarda o encerramento para não deixar zombies
            await asyncio.sleep(0.5)
            if proc.returncode is None:
                proc.kill()
            
            del PROCESSES[task_id]
            STATE["running"][task_id] = "idle"
            STATE["active_files"][task_id] = []
            
            await manager.broadcast({"type": "update", "state": STATE})
            return {"status": "ok"}
        except Exception as e:
            return JSONResponse(status_code=500, content={"message": str(e)})
    return {"status": "not_running"}        

@app.get("/api/health")
async def get_health(): return HEALTH_CACHE

@app.get("/")
async def serve_index(): return FileResponse(os.path.join(WWW_PATH, "index.html"))
app.mount("/", StaticFiles(directory=WWW_PATH), name="static")
