import os, json, asyncio, subprocess, re, typing, time, signal, shutil, hashlib, uuid, platform, httpx, socket, sys, base64, threading
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import URLError, HTTPError
from fastapi import FastAPI, BackgroundTasks, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager # <--- Garante este import
from fastapi.middleware.cors import CORSMiddleware

# --- Web Push (notificações push a sério, mesmo com o ecrã desligado) ---
# Dependência opcional: sem 'pywebpush'/'cryptography' instalados, a app
# continua a funcionar normalmente, só sem push (fica só a notificação
# local, que já existia, ativa enquanto a app está aberta).
try:
    from pywebpush import webpush, WebPushException
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False
    print(">>> [PUSH] 'pywebpush'/'cryptography' não instalados — push desativado. Corre: pip install pywebpush")

# --- mDNS/Bonjour (deteção automática do servidor na rede local) ---
# Dependência opcional: se 'zeroconf' não estiver instalado, a app continua a
# funcionar normalmente, apenas sem anúncio automático na rede.
try:
    from zeroconf import ServiceInfo, Zeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

# --- Verificação de assinatura da licença (Ed25519) ---
# Usa só o 'cryptography' (não o 'pywebpush') -- por isso tem a sua própria
# flag, independente do PUSH_AVAILABLE. Sem isto instalado, por segurança a
# app trata qualquer licença como não verificável (nunca assume válida às
# cegas só porque a biblioteca falta).
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    LICENSE_SIGNING_AVAILABLE = True
except ImportError:
    LICENSE_SIGNING_AVAILABLE = False
    print(">>> [LICENSE] 'cryptography' não instalada — verificação de assinatura desativada. Corre: pip install cryptography")

def _license_signing_payload(email, hwid, expires_at):
    """Constrói exatamente a mesma string que o Railway assina -- os dois
    lados têm de concordar byte a byte, incluindo a normalização (email em
    minúsculas, sem espaços à volta)."""
    return f"{str(email or '').strip().lower()}|{str(hwid or '').strip()}|{str(expires_at or '').strip()}|active".encode("utf-8")

def verify_license_signature(email, hwid, expires_at, signature_b64):
    """True só se a assinatura bater certo com estes campos exatos -- ou
    seja, só se foi mesmo o Railway (dono da chave privada) a validar esta
    combinação precisa de email+hwid+expires_at. Editar o license.json à
    mão, ou reaproveitar a assinatura de outro dispositivo/data, falha aqui."""
    if not LICENSE_SIGNING_AVAILABLE:
        print(">>> [LICENSE] 'cryptography' não instalada -- não é possível verificar a assinatura.")
        return False
    if not signature_b64 or LICENSE_PUBLIC_KEY_B64 == "COLA_AQUI_A_CHAVE_PUBLICA_BASE64":
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(LICENSE_PUBLIC_KEY_B64))
        public_key.verify(base64.b64decode(signature_b64), _license_signing_payload(email, hwid, expires_at))
        return True
    except InvalidSignature:
        return False
    except Exception as e:
        print(f">>> [LICENSE] Erro ao verificar assinatura: {e}")
        return False

# --- CONFIGURAÇÕES DE CAMINHOS (híbrido ZimaOS/Docker + Windows/macOS/Linux) ---
# Dentro do container ZimaOS a imagem tem sempre /app_dist e /www_dist (criadas
# no build), por isso usamos a presença dessas pastas para saber que estamos
# "dentro" do container. Nesse caso os caminhos continuam fixos, como sempre
# (/app, /www, /config são os volumes montados pelo ZimaOS). Fora do
# container -- ou seja, quando corres o main.py diretamente no Windows, no
# macOS ou num Linux "normal" -- usamos as pastas de configuração nativas de
# cada SO, para não precisares de permissões de root nem de caminhos tipo
# /config que não existem fora do Docker.
IS_CONTAINER = os.path.exists("/app_dist") and os.path.exists("/www_dist")
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

# --- Auto-atualização (só na versão Windows) -------------------------------
# Publicas uma nova versão fazendo commit do instalador compilado para
# Downloads/Windows/ no repositório, com o nome "SyncPulse vX.Y_Setup.exe".
# Não há Releases/tags -- comparamos sempre com o ficheiro mais recente
# dessa pasta. IMPORTANTE: sempre que compilares e publicares um novo
# instalador, atualiza também este número para bater certo com o nome do
# ficheiro (ex: "SyncPulse v2.1_Setup.exe" -> APP_VERSION = "2.1"), já que
# main.py viaja dentro do próprio instalador e é ele que diz à app "em que
# versão estou eu, a correr agora".
APP_VERSION = "2.9"
UPDATE_REPO = "syncpulsegeral-collab/syncpulse"
UPDATE_DIR = "Downloads/Windows"
UPDATE_FILENAME_RE = re.compile(r"^SyncPulse v([0-9]+(?:\.[0-9]+)*)_Setup\.exe$", re.IGNORECASE)
UPDATE_CHECK_INTERVAL = 6 * 3600  # segundos entre verificações ao GitHub (poupa o rate-limit da API pública)
UPDATE_STATE = {
    "checked_at": 0, "available": False, "current_version": APP_VERSION,
    "latest_version": None, "download_url": None, "size": None, "sha": None, "filename": None,
}

# Pasta onde a app está instalada -- usada para encontrar a pasta "www"
# (frontend) fora do Docker. Se for um .exe empacotado (PyInstaller +
# Inno Setup), usamos a pasta onde o .exe está instalado (sys.executable),
# que é onde o Inno Setup copia a pasta "www" como ficheiros soltos.
# Se for o main.py a correr diretamente (python main.py), usamos a pasta
# do próprio script.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)

def _default_config_dir():
    """Pasta nativa de configuração do SyncPulse quando não estamos em Docker."""
    if IS_WINDOWS:
        base = os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "SyncPulse")
    if IS_MACOS:
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "SyncPulse")
    # Linux nativo (fora de Docker) -> segue o padrão XDG
    xdg = os.getenv("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "syncpulse")

def _default_rclone_config():
    """Por omissão aponta para o rclone.conf 'oficial' de cada SO (o mesmo
    sítio onde o rclone normal guarda a configuração), para o SyncPulse
    aproveitar automaticamente remotes já configurados pelo utilizador:
      Windows -> %APPDATA%\\rclone\\rclone.conf
      macOS   -> ~/Library/Application Support/rclone/rclone.conf
      Linux   -> ~/.config/rclone/rclone.conf
    Pode sempre ser sobreposto com a variável de ambiente
    SYNCPULSE_RCLONE_CONFIG."""
    if IS_WINDOWS:
        base = os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "rclone", "rclone.conf")
    if IS_MACOS:
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "rclone", "rclone.conf")
    xdg = os.getenv("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "rclone", "rclone.conf")

if IS_CONTAINER:
    WWW_PATH = os.getenv("SYNCPULSE_WWW_PATH", "/www")
    CONFIG_DIR = os.getenv("SYNCPULSE_CONFIG_DIR", "/config")
    RCLONE_CONFIG = os.getenv("SYNCPULSE_RCLONE_CONFIG", os.path.join(CONFIG_DIR, "rclone.conf"))
else:
    # Em aplicações PyInstaller os assets vivem em _MEIPASS, não junto do .exe.
    WWW_PATH = os.getenv("SYNCPULSE_WWW_PATH", os.path.join(BUNDLE_DIR, "www"))
    CONFIG_DIR = os.getenv("SYNCPULSE_CONFIG_DIR", _default_config_dir())
    RCLONE_CONFIG = os.getenv("SYNCPULSE_RCLONE_CONFIG", _default_rclone_config())

CONFIG_FILE = os.path.join(CONFIG_DIR, "tasks.json")
LOGS_DIR = os.path.join(CONFIG_DIR, "logs")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
LICENSE_FILE = os.path.join(CONFIG_DIR, "license.json")
DEVICE_ID_FILE = os.path.join(CONFIG_DIR, "device-id")
BISYNC_WORKDIR = os.path.join(CONFIG_DIR, "bisync")
_bundled_rclone = os.path.join(BUNDLE_DIR, "rclone", "rclone.exe" if IS_WINDOWS else "rclone")
RCLONE_EXE = os.getenv("SYNCPULSE_RCLONE_PATH", _bundled_rclone if os.path.isfile(_bundled_rclone) else "rclone")
HWID_SALT = os.getenv("SYNCPULSE_HWID_SALT", "syncpulse-hwid-v1")
LAST_BOX_CHECK_TIME = 0

# Chave PÚBLICA Ed25519 do par gerado para o licenciamento (ver chaves.py).
# É segura de trazer aqui -- só serve para VERIFICAR assinaturas, nunca para
# as criar; extraí-la do .exe não dá a ninguém forma de fabricar uma licença
# nova. Substitui pelo valor real que o script te der (base64, 32 bytes),
# ou define a variável de ambiente SYNCPULSE_LICENSE_PUBLIC_KEY.
LICENSE_PUBLIC_KEY_B64 = os.getenv("SYNCPULSE_LICENSE_PUBLIC_KEY", "TtNoXgSv3Uuqb+b8s1iC0y5c6UpcRGAauZ2mobip/bw=")

# --- Notificações push (Web Push) ---
VAPID_PRIVATE_FILE = os.path.join(CONFIG_DIR, "vapid_private.pem")
VAPID_PUBLIC_FILE = os.path.join(CONFIG_DIR, "vapid_public_key.txt")
PUSH_SUBS_FILE = os.path.join(CONFIG_DIR, "push_subscriptions.json")
NOTIFY_STATE_FILE = os.path.join(CONFIG_DIR, "notify_state.json")
DEFAULT_PUSH_PREFS = {
    "success": True, "error": True,
    "license_expiring": True, "license_expired": True,
}

# Porto onde o servidor está exposto na rede local. Se mapeares o container
# para outro porto no ZimaOS, ajusta via variável de ambiente SYNCPULSE_PORT.
MDNS_SERVICE_TYPE = "_syncpulse._tcp.local."
MDNS_PORT = int(os.getenv("SYNCPULSE_PORT", "8000"))
ZC_INSTANCE = None
ZC_SERVICE_INFO = None

for p in [CONFIG_DIR, LOGS_DIR, BISYNC_WORKDIR, os.path.dirname(RCLONE_CONFIG)]:
    if p and not os.path.exists(p): os.makedirs(p, exist_ok=True)
    
# --- 2. CONFIGURAÇÕES E AGENDADOR ---
app_scheduler = AsyncIOScheduler()
# (Mantém as tuas variáveis de caminhos como WWW_PATH, etc.)    

def get_secure_hwid():
    """Devolve um fingerprint hash; nunca expõe o identificador bruto à API."""
    machine_id = os.getenv("SYNCPULSE_HWID", "").strip()
    if not machine_id:
        try:
            with open(DEVICE_ID_FILE, "r", encoding="utf-8") as source:
                machine_id = source.read().strip()
        except OSError:
            pass
    if not machine_id:
        try:
            if IS_WINDOWS:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                    machine_id = winreg.QueryValueEx(key, "MachineGuid")[0]
            elif IS_MACOS:
                out = subprocess.check_output(
                    ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], text=True, timeout=5
                )
                m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
                if m:
                    machine_id = m.group(1)
            else:
                # Linux (container ou nativo)
                for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                    try:
                        with open(path, "r", encoding="utf-8") as source:
                            machine_id = source.read().strip()
                        if machine_id:
                            break
                    except OSError:
                        pass
        except Exception:
            pass
    if not machine_id:
        machine_id = f"{uuid.getnode()}|{platform.node()}"
    try:
        if not os.path.exists(DEVICE_ID_FILE):
            with open(DEVICE_ID_FILE, "w", encoding="utf-8") as target:
                target.write(machine_id)
                target.flush()
                os.fsync(target.fileno())
    except OSError:
        pass
    return hashlib.sha256(f"{HWID_SALT}|{machine_id}".encode("utf-8")).hexdigest()

def get_local_ip():
    """Descobre o IP local da máquina na rede (não depende de hostname/DNS)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def start_mdns_advertisement():
    """Anuncia o SyncPulse na rede local via mDNS/Bonjour, para a app mobile
    o descobrir automaticamente sem precisar de introduzir o IP à mão."""
    global ZC_INSTANCE, ZC_SERVICE_INFO
    if not ZEROCONF_AVAILABLE:
        print(">>> [mDNS] Biblioteca 'zeroconf' não instalada — deteção automática desativada.")
        print(">>> [mDNS] Adiciona 'zeroconf' ao requirements.txt para ativar.")
        return
    try:
        local_ip = get_local_ip()
        hostname = (platform.node() or "syncpulse").split(".")[0]
        instance_name = f"SyncPulse-{hostname}"
        ZC_SERVICE_INFO = ServiceInfo(
            MDNS_SERVICE_TYPE,
            f"{instance_name}.{MDNS_SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=MDNS_PORT,
            properties={"hwid": get_secure_hwid()[:12], "version": "1.1", "device": hostname},
            server=f"{hostname}.local.",
        )
        ZC_INSTANCE = Zeroconf()
        ZC_INSTANCE.register_service(ZC_SERVICE_INFO)
        print(f">>> [mDNS] SyncPulse anunciado na rede local: {hostname}.local ({local_ip}:{MDNS_PORT})")
    except Exception as e:
        print(f">>> [mDNS] Erro ao anunciar na rede local: {e}")

def stop_mdns_advertisement():
    global ZC_INSTANCE, ZC_SERVICE_INFO
    if ZC_INSTANCE and ZC_SERVICE_INFO:
        try:
            ZC_INSTANCE.unregister_service(ZC_SERVICE_INFO)
            ZC_INSTANCE.close()
            print(">>> [mDNS] Anúncio removido da rede local.")
        except Exception:
            pass

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

def license_is_current(license_data):
    raw_expiry = str(license_data.get("expires_at") or "").strip()
    if not raw_expiry:
        return False
    try:
        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > datetime.now(timezone.utc)
    except ValueError:
        return False

def license_is_expired(license_data):
    raw_expiry = str(license_data.get("expires_at") or "").strip()
    if not raw_expiry:
        return False
    try:
        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)
    except ValueError:
        return False

def load_tasks():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

def get_initial_state():
    """Inicializa o estado global lendo a licença do disco de forma rigorosa."""
    s = load_settings()
    lic = load_license() # Esta função lê o /config/license.json
    hwid_atual = get_secure_hwid()
    has_saved_license = bool(lic.get("email") and lic.get("key"))

    # Validação Híbrida:
    # 1. O ficheiro tem de ter "active": true (como no teu print)
    # 2. O HWID gravado tem de ser IGUAL ao HWID atual do hardware
    # 3. A assinatura tem de bater certo com os campos gravados -- sem isto,
    #    editar o license.json à mão continuaria a "funcionar" só por
    #    reescrever active/hwid/expires_at.
    is_valid = (
        lic.get("active") is True
        and lic.get("hwid") == hwid_atual
        and license_is_current(lic)
        and verify_license_signature(lic.get("email"), lic.get("hwid"), lic.get("expires_at"), lic.get("signature"))
    )
    is_expired = lic.get("active") is True and license_is_expired(lic)

    return {
        "last_error_id": {},
        "running": {}, "logs": {}, "active_files": {}, "finished_files": {},
        "all_files": {}, "skipped_files": {}, "stats": {}, "failed_files": {},
        "file_sizes": {}, "task_error": {},
        "auto_simulate": s.get("auto_simulate", True),
        "terms_accepted": s.get("terms_accepted", False),
        # Enviamos as duas variantes para garantir que o Frontend e o Backend se entendem
        "licensed": has_saved_license,
        "license_active": is_valid, 
        "license_expired": is_expired,
        "license_info": {
            # Valores por omissão (garantem que estas chaves existem sempre,
            # mesmo sem nenhuma licença gravada -- lic == {} no primeiro
            # arranque). O frontend conta com isto para nunca ver "undefined".
            "email": "", "key": "", "device_name": "", "activated_at": "",
            "plan": 1, "expires_at": "", "hwid": "",
            **lic,  # sobrepõe com o que estiver gravado em disco -- todos os
                    # campos (incluindo signature, last_check e quaisquer
                    # outros futuros), não só uma lista fixa que já se
                    # esqueceu duas vezes de campos importantes.
            "active": is_valid,  # e isto é sempre o valor recalculado agora, nunca o cru do ficheiro
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
# Só faz sentido dentro do container ZimaOS/Docker, onde a imagem traz o
# código "de fábrica" em /app_dist e /www_dist e os copia para os volumes
# /app, /www e /config. No Windows, macOS ou Linux nativo não há nada para
# "instalar" -- o main.py já corre diretamente da pasta onde está, por isso
# a função nem é chamada (ver mais abaixo).
def bootstrap():
    if not IS_CONTAINER:
        return
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



# Quantos dias a app continua a confiar numa licença já validada, mesmo sem
# conseguir voltar a contactar o Railway (viagens sem rede, firewall
# corporativa, downtime do servidor, etc.). Passada esta janela sem uma
# confirmação nova, a app bloqueia -- deixa de ser "confia para sempre" só
# porque o servidor está inacessível.
LICENSE_OFFLINE_GRACE_DAYS = 7

def _license_offline_grace_expired():
    """True se já não validamos a licença há mais dias do que a janela de
    tolerância permite (LICENSE_OFFLINE_GRACE_DAYS) -- nesse caso deixamos
    de confiar cegamente em "não consegui contactar o servidor". Sem
    nenhuma validação bem-sucedida registada, não há tolerância nenhuma."""
    raw_last_check = str(STATE["license_info"].get("last_check") or "").strip()
    if not raw_last_check:
        return True
    try:
        last_check = datetime.fromisoformat(raw_last_check.replace("Z", "+00:00"))
        if last_check.tzinfo is None:
            last_check = last_check.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_check) > timedelta(days=LICENSE_OFFLINE_GRACE_DAYS)

async def silent_license_check():
    """Valida a licença no Railway em background sem interromper o utilizador."""
    await asyncio.sleep(1)
    
    if not (STATE["license_info"].get("email") and STATE["license_info"].get("key")):
        return

    print(">>> [BACKGROUND] A validar licença com o servidor central...")
    email = STATE["license_info"].get("email")
    key = STATE["license_info"].get("key")
    hwid = get_secure_hwid()

    try:
        async with httpx.AsyncClient() as client:
            # Este pedido tem de ser apenas de validação. Usar /activate aqui
            # volta a registar o HWID quando o dispositivo foi removido na BD.
            response = await client.post(
                LICENSE_VALIDATE_URL,
                json={"email": email, "license_key": key, "hwid": hwid, "device_name": STATE["license_info"].get("device_name") or get_device_name()},
                timeout=10.0
            )

        if response.status_code == 200:
            res_data = response.json()
            expires_at = res_data.get("expires_at") or STATE["license_info"].get("expires_at", "")
            signature_ok = res_data.get("valid") and verify_license_signature(email, hwid, expires_at, res_data.get("signature"))
            if res_data.get("valid") and not signature_ok:
                # O servidor disse "válido", mas a assinatura não bate certo
                # -- trata isto como recusa, nunca como confirmação.
                print(">>> [BACKGROUND] Resposta do servidor com assinatura inválida -- a tratar como recusa.")
            if signature_ok:
                # Licença confirmada! Atualizamos o ficheiro local com a data do check
                STATE["licensed"] = True
                STATE["license_active"] = True
                STATE["license_expired"] = False
                STATE["license_info"]["active"] = True
                STATE["license_info"]["hwid"] = hwid
                STATE["license_info"]["last_check"] = datetime.now(timezone.utc).isoformat()
                STATE["license_info"]["plan"] = res_data.get("plan", STATE["license_info"].get("plan", 1))
                STATE["license_info"]["expires_at"] = expires_at
                STATE["license_info"]["signature"] = res_data.get("signature")
                save_license(STATE["license_info"])
                await refresh_automation_services()
                await manager.broadcast({"type": "update", "state": STATE})
                print(">>> [BACKGROUND] Licença confirmada e atualizada.")
            else:
                # O servidor diz que a licença já não é válida (ex: refund ou remoção de slot)
                print(">>> [BACKGROUND] Licença revogada pelo servidor!")
                if res_data.get("reason") == "expired":
                    STATE["license_info"]["expires_at"] = res_data.get("expires_at", "")
                await revoke_license_local(expired=res_data.get("reason") == "expired")
        elif response.status_code in (400, 401, 403):
            # Estas respostas recusam explicitamente a licença/HWID; não são
            # uma falha de ligação e devem bloquear a ativação local.
            print(">>> [BACKGROUND] Licença recusada pelo servidor!")
            details = response.json()
            if details.get("reason") == "expired":
                STATE["license_info"]["expires_at"] = details.get("expires_at", "")
            await revoke_license_local(expired=details.get("reason") == "expired")
        else:
            # Servidor com erro (5xx, etc.): mantemos o utilizador ativo, mas
            # só dentro da janela de tolerância -- para lá disso, já não
            # confiamos só porque "não conseguimos confirmar que é inválida".
            print(f">>> [BACKGROUND] Servidor central indisponível ({response.status_code}).")
            if _license_offline_grace_expired():
                print(">>> [BACKGROUND] Janela de tolerância offline expirou -- a bloquear.")
                await revoke_license_local()

    except Exception as e:
        print(f">>> [BACKGROUND] Sem ligação à internet para validar: {e}")
        if _license_offline_grace_expired():
            print(">>> [BACKGROUND] Janela de tolerância offline expirou -- a bloquear.")
            await revoke_license_local()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global APP_LOOP
    APP_LOOP = asyncio.get_running_loop()

    # 0. Anuncia o servidor na rede local (mDNS/Bonjour) para a app mobile
    #    o detetar automaticamente sem precisar do IP manual.
    start_mdns_advertisement()

    # 1. Inicia TUDO o que for local imediatamente (Sincronização arranca já)
    if STATE.get("license_active"):
        print(">>> [STARTUP] Licença local detectada. A iniciar motores...")
        sync_realtime_watchers(load_tasks())
        sync_scheduled_tasks(load_tasks())
        asyncio.create_task(poll_realtime_download_tasks())
    
    asyncio.create_task(update_health_cache())
    
    # 2. Configura o polling periódico (se licenciado)
    if STATE.get("license_active"):
        app_scheduler.add_job(
            poll_realtime_download_tasks, 'interval', seconds=REMOTE_POLL_SECONDS,
            id='remote-realtime-poll', replace_existing=True,
            max_instances=1, coalesce=True
        )

    # 3. LANÇA A VERIFICAÇÃO HÍBRIDA EM SEGUNDO PLANO
    asyncio.create_task(silent_license_check())

    # 4. Notificações push: verifica expiração da licença já no arranque,
    #    e depois recorrentemente a cada 24h.
    asyncio.create_task(check_license_expiry_and_notify())
    app_scheduler.add_job(
        check_license_expiry_and_notify, 'interval', hours=24,
        id='license-expiry-check', replace_existing=True,
        max_instances=1, coalesce=True
    )

    app_scheduler.start()
    
    yield # App operacional
    
    if app_scheduler.running:
        app_scheduler.shutdown(wait=False)
    stop_mdns_advertisement()

# --- 4. AGORA SIM, CRIAR A INSTÂNCIA DA APP ---
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        device_name = str(data.get("device_name") or get_device_name()).strip()[:120]
        
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

        # 2b. CONFIRMA A ASSINATURA -- sem isto, um proxy/MITM ou um servidor
        # falso poderia devolver "valid: true" para qualquer coisa. Só
        # aceitamos se a assinatura bater certo com email+hwid+expires_at
        # exatamente como o Railway (dono da chave privada) os validou.
        expires_at = res_data.get("expires_at", "")
        if not verify_license_signature(email, hwid, expires_at, res_data.get("signature")):
            print(">>> [LICENSE] Assinatura do servidor inválida ou ausente -- ativação recusada.")
            return JSONResponse(status_code=502, content={
                "message": "Não foi possível confirmar a autenticidade da resposta do servidor de licenças. Tenta novamente."
            })

        # 3. SE O SERVIDOR ACEITAR: PREPARAR DADOS PARA O DISCO
        # Adicionamos 'last_check' para o modo híbrido saber quando foi a última validação online
        license_data = {
            "email": email,
            "key": key,
            "active": True,
            "hwid": hwid,
            "device_name": device_name,
            "plan": res_data.get("plan", 1),
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "last_check": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "signature": res_data.get("signature"),
        }

        # 4. PERSISTÊNCIA FÍSICA (Garante que sobrevive ao Reboot)
        with open(LICENSE_FILE, "w", encoding="utf-8") as f:
            json.dump(license_data, f)
            f.flush()
            os.fsync(f.fileno()) # Força o ZimaOS a gravar no disco real

        # 5. ATUALIZAÇÃO DO ESTADO EM MEMÓRIA (Desbloqueio instantâneo)
        STATE["licensed"] = True
        STATE["license_active"] = True
        STATE["license_expired"] = False
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
            "license_active": True,
            "plan": license_data["plan"]
        }

    except Exception as e:
        print(f">>> [LICENSE] Erro crítico na ativação: {e}")
        return JSONResponse(
            status_code=500, 
            content={"message": "Erro ao contactar servidor de ativação."}
        )

async def revoke_license_local(expired=False):
    """Bloqueia a app imediatamente se a validação falhar."""
    STATE["licensed"] = False
    STATE["license_active"] = False
    STATE["license_expired"] = expired
    if expired:
        STATE["license_info"]["active"] = False
        save_license(STATE["license_info"])
    else:
        STATE["license_info"] = {}
    if not expired and os.path.exists(LICENSE_FILE):
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
    


# --- LICENCIAMENTO ---------------------------------------------------------
# O servidor de licenciamento (hoje alojado no Render; o Railway expirou) é a
# fonte de verdade para licenças e limite de dispositivos. A aplicação guarda
# localmente apenas uma ativação já validada para que não seja necessário
# consultar a API a cada sincronização.
TEST_LICENSE_EMAIL = "syncpulsegeral@gmail.com"
TEST_LICENSE_KEY = "SYNC-TEST-2026-UNLOCK"
AUTH_SERVER_URL = os.getenv(
    "SYNCPULSE_AUTH_SERVER_URL", "https://syncpulse-auth.onrender.com"
).rstrip("/")
LICENSE_API_URL = f"{AUTH_SERVER_URL}/api/licenses/activate"
# Endpoint sem efeitos laterais: confirma que a ativação deste HWID ainda
# existe na BD, mas nunca cria nem recupera uma ativação removida.
LICENSE_VALIDATE_URL = os.getenv(
    "SYNCPULSE_LICENSE_VALIDATE_URL",
    f"{AUTH_SERVER_URL}/api/licenses/validate"
)
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
    if license_data.get("active") is not True:
        return False
    if license_data.get("hwid") != get_secure_hwid():
        return False
    if not license_is_current(license_data):
        return False
    # A assinatura tem de bater certo com os campos exatamente como estão
    # gravados no ficheiro -- é isto que impede alguém de editar o
    # license.json à mão (mudar active/hwid/expires_at quebra a assinatura,
    # porque deixa de bater com o que o Railway realmente assinou).
    return verify_license_signature(
        license_data.get("email"), license_data.get("hwid"),
        license_data.get("expires_at"), license_data.get("signature"),
    )

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
            RCLONE_EXE, "--config", RCLONE_CONFIG, "lsjson", "--recursive", "--files-only", remote,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hidden_subprocess_kwargs()
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
        RCLONE_EXE, "--config", RCLONE_CONFIG, "lsf", "-R", "--files-only", path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_hidden_subprocess_kwargs()
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

def record_task_error(task, task_id, error):
    """Regista erros sem ficheiro (token, cloud inacessível, listagem) como
    erro da tarefa, para a UI os mostrar como qualquer erro de cópia."""
    message = str(error).strip() or "Erro desconhecido ao aceder à cloud."
    source = task.get("remote") if task.get("type") == "download" else task.get("local")
    failed_entry = f"[Erro de acesso] {source or task.get('name', 'Tarefa')}"
    STATE["logs"].setdefault(task_id, []).insert(0, f"ERROR: {message}")
    STATE["task_error"][task_id] = True
    STATE["failed_files"][task_id] = [failed_entry]
    STATE["all_files"][task_id] = [failed_entry]

def build_bisync_command(task, tid, remote_type, dry_run=False):
    """Prepara um bisync persistente e recuperável; o resync ocorre apenas na primeira vez."""
    cmd = [
        RCLONE_EXE, "--config", RCLONE_CONFIG, "bisync", task["local"], task["remote"],
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

    cmd = [RCLONE_EXE, "--config", RCLONE_CONFIG, "copy", src, dst, "-v", "--dry-run", "--update", "--modify-window", "2s", "--stats", "1s"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **_hidden_subprocess_kwargs())
    
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
        stderr=asyncio.subprocess.STDOUT,
        **_hidden_subprocess_kwargs()
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
                stderr=asyncio.subprocess.STDOUT,
                **_hidden_subprocess_kwargs()
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
    cmd = [RCLONE_EXE, "--config", RCLONE_CONFIG, "copy", src, dst, "-P", "-v", "--update", "--modify-window", "2s", "--stats", "1s", "--stats-file-name-length", "0", "--transfers", "1", "--checkers", "1", "--multi-thread-streams", "0"]
    if r_type == "onedrive": cmd += ["--onedrive-chunk-size", "10M"]

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **_hidden_subprocess_kwargs())
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
        RCLONE_EXE, "--config", RCLONE_CONFIG, "copy", src, dst, 
        "-vv", "-P", "--stats", "1s",
        "--transfers", "1", "--checkers", "1", "--multi-thread-streams", "0"
    ]
    if r_type == "onedrive": cmd += ["--onedrive-chunk-size", "10M"]

    last_ws_update, buffer = 0, ""
    current_fn, current_p = None, 0
    calibrated_total = float(grand_total_bytes)
    return_code = 1

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **_hidden_subprocess_kwargs())
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
        STATE["task_error"][tid] = False # <--- ADICIONA ESTA LINHA AQUI
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
            STATE["all_files"][tid] = remove_root_ghosts(await list_rclone_files(src))
            
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
            record_task_error(task, tid, e)
        finally:
            completed_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            # Determina se houve erro real
            had_error = not operation_succeeded
            
            save_history(tid, {
                "date": completed_at,
                "type": operation_type,
                "mode": task.get("type", "upload"),
                "status": "Sucesso" if not had_error else "Erro",
                "log": "\n".join(STATE["logs"].get(tid, [])[:50])
            })

            # Atualiza o estado de erro e gera ID único para o telemóvel "acordar"
            STATE["task_error"][tid] = had_error
            if had_error:
                STATE["last_error_id"][tid] = f"err-{time.time()}"
            else:
                STATE["last_error_id"][tid] = f"ok-{time.time()}"

            STATE["running"][tid] = "idle"
            STATE["active_files"][tid] = []
            
            # Envia o estado completo (tipo init garante que o telemóvel recebe tudo)
            await manager.broadcast({"type": "init", "tasks": load_tasks(), "state": STATE})

            # Push a sério (funciona com o ecrã desligado/app fechada) — só
            # para sincronizações reais, não para simulações.
            if not manual_simulate:
                task_name = task.get("name", "Tarefa")
                if had_error:
                    reason = summarize_error_log(STATE["logs"].get(tid, []))
                    await asyncio.to_thread(
                        notify_push, "error", "Erro na sincronização",
                        f"{task_name}: {reason}", f"sync-{tid}",
                    )
                else:
                    await asyncio.to_thread(
                        notify_push, "success", "Sincronização concluída",
                        f"{task_name} foi sincronizada com sucesso.", f"sync-{tid}",
                    )
            
# --- ENDPOINTS E SERVICES ---

# ==================== NOTIFICAÇÕES PUSH (Web Push) ====================

def ensure_vapid_keys():
    """Gera (uma única vez) e devolve o par de chaves VAPID usado para assinar
    as notificações push. A chave pública é enviada ao browser; a privada
    nunca sai do servidor."""
    if not PUSH_AVAILABLE:
        return None, None
    if not os.path.exists(VAPID_PRIVATE_FILE):
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with open(VAPID_PRIVATE_FILE, "wb") as f:
            f.write(pem)
        public_key = private_key.public_key()
        raw_pub = public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        pub_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("utf-8")
        with open(VAPID_PUBLIC_FILE, "w", encoding="utf-8") as f:
            f.write(pub_b64)
    with open(VAPID_PUBLIC_FILE, "r", encoding="utf-8") as f:
        pub_b64 = f.read().strip()
    return VAPID_PRIVATE_FILE, pub_b64

def load_push_subs():
    if os.path.exists(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def save_push_subs(subs):
    with open(PUSH_SUBS_FILE, "w", encoding="utf-8") as f:
        json.dump(subs, f, indent=2, ensure_ascii=False)

def _sub_id_for(endpoint):
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:24]

def _send_push_sync(subscription_info, payload, private_key_path):
    """Envio síncrono (a lib pywebpush é bloqueante) — chamar sempre via
    asyncio.to_thread para não travar o event loop."""
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=private_key_path,
            vapid_claims={"sub": "mailto:suporte@syncpulse.app"},
            ttl=3600,
        )
        return "ok"
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            return "gone"  # subscrição inválida/expirada no lado do browser
        print(f">>> [PUSH] Falha ao enviar: {e}")
        return "error"
    except Exception as e:
        print(f">>> [PUSH] Falha ao enviar: {e}")
        return "error"

def notify_push(notif_type, title, body, tag=None, url="/mobile.html"):
    """Envia uma notificação a todos os dispositivos subscritos que a tenham
    ativada nas preferências. Função síncrona — chamar via asyncio.to_thread."""
    if not PUSH_AVAILABLE:
        return
    private_path, _ = ensure_vapid_keys()
    subs = load_push_subs()
    changed = False
    for sub_id, entry in list(subs.items()):
        if not entry.get("prefs", {}).get(notif_type, True):
            continue
        result = _send_push_sync(entry["subscription"], {
            "title": title, "body": body, "tag": tag, "url": url
        }, private_path)
        if result == "gone":
            del subs[sub_id]
            changed = True
    if changed:
        save_push_subs(subs)

def summarize_error_log(log_lines):
    """Resume as últimas linhas de log a uma frase curta — mesmo critério
    usado no lado do frontend para as notificações locais, para os dois
    tipos de aviso dizerem a mesma coisa."""
    text = " ".join(log_lines[:5]) if log_lines else ""
    if "file_size_limit_exceeded" in text:
        return "Ficheiro demasiado grande para o destino."
    if "invalid_grant" in text:
        return "Login expirado. Reautentica a Cloud."
    lower = text.lower()
    if any(k in lower for k in ["oauth", "401", "403", "unauthorized", "authentication"]):
        return "Token expirado ou sem autorização."
    if any(k in lower for k in ["timeout", "deadline", "i/o timeout"]):
        return "Tempo de resposta esgotado."
    if any(k in lower for k in ["no such host", "connection refused", "network is unreachable", "dial tcp", "couldn't", "could not"]):
        return "Não foi possível ligar ao servidor remoto."
    if log_lines and log_lines[0].strip():
        first = log_lines[0].strip()
        return (first[:140] + "…") if len(first) > 140 else first
    return "Erro desconhecido."

def parse_license_expiry(expires_at_raw):
    """Interpreta o campo expires_at devolvido pelo servidor de licenças
    (formato Postgres, ex: '2027-07-27 20:53:52+00') como datetime UTC."""
    if not expires_at_raw:
        return None
    s = str(expires_at_raw).strip()
    if not s:
        return None
    s = s.replace(" ", "T", 1)
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)  # "+00" -> "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def load_notify_state():
    if os.path.exists(NOTIFY_STATE_FILE):
        try:
            with open(NOTIFY_STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def save_notify_state(data):
    with open(NOTIFY_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

async def check_license_expiry_and_notify():
    """Corrido no arranque e depois a cada 24h. Avisa aos 30/15/5 dias e no
    dia da expiração — cada aviso é enviado uma única vez (fica marcado em
    notify_state.json), e reinicia-se sozinho se a data de expiração mudar
    (renovação da licença)."""
    if not PUSH_AVAILABLE:
        return
    expires_raw = STATE.get("license_info", {}).get("expires_at")
    dt = parse_license_expiry(expires_raw)
    if dt is None:
        return

    notify_state = load_notify_state()
    if notify_state.get("expires_at") != expires_raw:
        notify_state = {"expires_at": expires_raw}

    now = datetime.now(timezone.utc)
    days_left = (dt - now).days

    for days, key in [(30, "license_30"), (15, "license_15"), (5, "license_5")]:
        if days_left <= days and days_left > 0 and not notify_state.get(key):
            await asyncio.to_thread(
                notify_push, "license_expiring", "A tua licença SyncPulse vai expirar",
                f"Restam {days_left} dias. Renova para não perderes o acesso.",
                "license-expiry",
            )
            notify_state[key] = True

    if days_left <= 0 and not notify_state.get("license_expired"):
        await asyncio.to_thread(
            notify_push, "license_expired", "A tua licença SyncPulse expirou",
            "As sincronizações automáticas estão paradas. Renova a licença para continuar.",
            "license-expired",
        )
        notify_state["license_expired"] = True

    save_notify_state(notify_state)

@app.get("/api/push/vapid_public_key")
async def push_vapid_public_key():
    if not PUSH_AVAILABLE:
        return JSONResponse(status_code=501, content={"message": "Notificações push indisponíveis: instala 'pywebpush' no servidor."})
    _, pub = ensure_vapid_keys()
    return {"key": pub}

@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    if not PUSH_AVAILABLE:
        return JSONResponse(status_code=501, content={"message": "Notificações push indisponíveis: instala 'pywebpush' no servidor."})
    body = await request.json()
    subscription = body.get("subscription")
    prefs = body.get("prefs") or {}
    if not subscription or not subscription.get("endpoint"):
        return JSONResponse(status_code=400, content={"message": "Subscrição inválida."})
    sub_id = _sub_id_for(subscription["endpoint"])
    subs = load_push_subs()
    subs[sub_id] = {
        "subscription": subscription,
        "prefs": {**DEFAULT_PUSH_PREFS, **prefs},
        "updated_at": datetime.now().isoformat(),
    }
    save_push_subs(subs)
    return {"status": "ok", "id": sub_id}

@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    body = await request.json()
    endpoint = body.get("endpoint")
    if not endpoint:
        return JSONResponse(status_code=400, content={"message": "Endpoint em falta."})
    subs = load_push_subs()
    sub_id = _sub_id_for(endpoint)
    if sub_id in subs:
        del subs[sub_id]
        save_push_subs(subs)
    return {"status": "ok"}

@app.post("/api/push/test")
async def push_test(request: Request):
    if not PUSH_AVAILABLE:
        return JSONResponse(status_code=501, content={"message": "Notificações push indisponíveis: instala 'pywebpush' no servidor."})
    body = await request.json()
    endpoint = body.get("endpoint")
    if not endpoint:
        return JSONResponse(status_code=400, content={"message": "Endpoint em falta."})
    sub_id = _sub_id_for(endpoint)
    subs = load_push_subs()
    entry = subs.get(sub_id)
    if not entry:
        return JSONResponse(status_code=404, content={"message": "Subscrição não encontrada."})
    private_path, _ = ensure_vapid_keys()
    result = await asyncio.to_thread(_send_push_sync, entry["subscription"], {
        "title": "SyncPulse", "body": "Notificação de teste — está tudo a funcionar!", "tag": "test", "url": "/mobile.html"
    }, private_path)
    if result == "gone":
        del subs[sub_id]
        save_push_subs(subs)
        return JSONResponse(status_code=410, content={"message": "Subscrição expirada. Ativa as notificações outra vez."})
    if result != "ok":
        return JSONResponse(status_code=502, content={"message": "Falha ao enviar a notificação de teste."})
    return {"status": "ok"}


@app.get("/api/browse/local")
def browse_local_endpoint(path: str = "/mnt"):
    """Lista apenas pastas do sistema local."""
    if not path or path == "undefined":
        path = "/mnt" if not IS_WINDOWS else ""
    if IS_WINDOWS and path in {"/", "/mnt", ""}:
        import string
        return [
            {"name": f"{letter}:\\", "path": f"{letter}:\\", "is_dir": True, "is_drive": True}
            for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")
        ]
    
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

@app.post("/api/remotes/terminal/start")
def start_rclone_config_terminal():
    """Abre o configurador interativo apenas na aplicação Windows nativa."""
    if not IS_WINDOWS or IS_CONTAINER:
        return JSONResponse(status_code=400, content={
            "message": "A configuração por terminal é exclusiva da versão Windows."
        })
    if not os.path.isfile(RCLONE_EXE):
        return JSONResponse(status_code=500, content={"message": "rclone.exe não foi encontrado na instalação."})
    try:
        # A sintaxe cmd/start evita problemas com espaços no caminho de instalação.
        command = f'start cmd /c ""{RCLONE_EXE}" --config "{RCLONE_CONFIG}" config"'
        subprocess.Popen(command, shell=True, cwd=os.path.dirname(RCLONE_EXE))
        return {"status": "ok"}
    except OSError as error:
        return JSONResponse(status_code=500, content={"message": str(error)})

# --- ASSISTENTE DE CRIAÇÃO DE REMOTES (WIZARD INTERATIVO) ---
#
# Em vez de abrir uma consola externa com "rclone config" (start_rclone_config_terminal,
# acima), o wizard conduz o mesmo diálogo interativo do rclone dentro da própria app,
# pergunta a pergunta, usando "rclone config create ... --non-interactive". Esse modo
# devolve sempre JSON a descrever o próximo passo: {"State":..., "Option":{...}, "Error":...}.

def _hidden_subprocess_kwargs():
    """No Windows evita que apareça (a piscar) uma janela de consola ao correr o rclone."""
    return {"creationflags": 0x08000000} if IS_WINDOWS else {}

def run_rclone_raw(args, timeout=30):
    """Executa o rclone e devolve stdout, stderr e código de saída."""
    cmd = [RCLONE_EXE, "--config", RCLONE_CONFIG] + args
    try:
        process = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=timeout, **_hidden_subprocess_kwargs()
        )
        return process.stdout, process.stderr, process.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1

def run_rclone_json(args, timeout=300):
    """Executa o rclone e tenta interpretar o stdout como JSON (fluxo --non-interactive)."""
    cmd = [RCLONE_EXE, "--config", RCLONE_CONFIG] + args
    try:
        process = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=timeout, **_hidden_subprocess_kwargs()
        )
        try:
            data = json.loads(process.stdout.strip())
        except Exception:
            data = None
        return data, process.stdout, process.stderr, process.returncode
    except subprocess.TimeoutExpired:
        return None, "", "timeout", 1
    except Exception as e:
        return None, "", str(e), 1

# Cache de "rclone config providers" (lista TODOS os backends já com a sua
# lista "Options" completa - é o único comando que devolve mesmo os campos
# de cada serviço; "config options <tipo>" não existe no rclone). Corremos
# isto uma única vez por arranque e guardamos em cache.
_PROVIDERS_RAW_CACHE = None
_PROVIDERS_RAW_LOCK = threading.Lock()

def _get_all_providers_raw():
    global _PROVIDERS_RAW_CACHE
    with _PROVIDERS_RAW_LOCK:
        if _PROVIDERS_RAW_CACHE is not None:
            return _PROVIDERS_RAW_CACHE
        stdout, _, _ = run_rclone_raw(["config", "providers"])
        data = []
        try:
            start = stdout.find('[')
            end = stdout.rfind(']') + 1
            if start != -1 and end > start:
                data = json.loads(stdout[start:end])
        except Exception:
            data = []
        _PROVIDERS_RAW_CACHE = data
        return data

@app.get("/api/providers")
def get_providers():
    """Lista os serviços cloud suportados pelo rclone (id + descrição)."""
    providers = _get_all_providers_raw()
    if providers:
        return sorted([
            {"name": p.get("Name"), "desc": p.get("Description") or p.get("Name")}
            for p in providers if p.get("Name") not in ["alias", "crypt", "union"]
        ], key=lambda x: x["desc"])

    # Fallback em texto, caso esta versão do rclone não devolva JSON válido.
    stdout, _, _ = run_rclone_raw(["config", "providers"])
    providers_list = []
    current_id = None
    for line in [l for l in stdout.splitlines() if l.strip()]:
        if not line.startswith(" ") and not line.startswith("\t"):
            if line.strip() in ["[", "]", "{", "}"]:
                continue
            current_id = line.strip()
        elif current_id:
            if current_id not in ["alias", "crypt", "union"]:
                providers_list.append({"name": current_id, "desc": line.strip()})
            current_id = None
    return sorted(providers_list, key=lambda x: x["desc"])

# Nomes de opções que pertencem à "maquinaria" OAuth (client_id, client_secret,
# token, etc.) - nunca são mostrados como campo editável, são geridos
# automaticamente pelo rclone.
OAUTH_INTERNAL_FIELDS = {
    "token", "client_id", "client_secret", "auth_url", "token_url",
    "service_account_credentials", "service_account_file", "client_credentials",
}

@app.get("/api/providers/{p_type}/options")
def get_provider_options(p_type: str):
    """
    Analisa as opções de configuração de um backend rclone e classifica-o em:
      - "oauth"    -> autenticação via browser (Google, OneDrive, Dropbox, Box, ...)
      - "userpass" -> autenticação por utilizador/password (FTP, SFTP, SMB, WebDAV, ...)
      - "other"    -> qualquer outro caso (S3 com access keys, backends locais, etc.)
    A classificação vem da própria metadata do rclone (sem listas fixas por
    serviço): backends OAuth expõem sempre "token"; backends user/pass
    expõem "user" e "pass".
    """
    providers = _get_all_providers_raw()
    provider = next((p for p in providers if p.get("Name") == p_type), None)
    data = (provider.get("Options") or []) if provider else []

    all_names = {opt.get("Name") for opt in data}
    is_oauth = "token" in all_names
    is_userpass = ("user" in all_names and "pass" in all_names) and not is_oauth
    auth_type = "oauth" if is_oauth else ("userpass" if is_userpass else "other")

    fields = []
    for opt in data:
        name = opt.get("Name")
        # "Hide" é um bitmask do próprio rclone: bit 2 (OptionHideConfigurator)
        # marca campos que não devem ser mostrados numa UI de configuração.
        hide = opt.get("Hide", 0)
        if hide is True or (isinstance(hide, int) and hide & 2):
            continue
        if is_oauth and name in OAUTH_INTERNAL_FIELDS:
            continue
        # Em backends userpass, "user"/"pass" são a credencial em si - têm de
        # aparecer sempre, mesmo que o rclone os marque como "Advanced".
        is_userpass_credential = is_userpass and name in ("user", "pass")
        # Mostra campos normais e ainda avançados MAS obrigatórios (ex: "scope"
        # no Google Drive ou "drive_type" no OneDrive, essenciais para o wizard).
        if opt.get("Advanced") and not opt.get("Required") and not is_userpass_credential:
            continue

        examples = [
            {"value": ex.get("Value"), "help": ex.get("Help")}
            for ex in (opt.get("Examples") or [])
        ]
        fields.append({
            "name": name,
            "help": opt.get("Help", ""),
            "default": opt.get("Default", ""),
            "required": bool(opt.get("Required", False)) or is_userpass_credential,
            "is_password": bool(opt.get("IsPassword", False)),
            "examples": examples,
        })

    return {"auth_type": auth_type, "fields": fields}

# Sessões de criação de remotes em curso. Alguns backends (OneDrive, Drive, ...)
# fazem mais perguntas depois da autorização no browser - tipo de ligação, qual
# das drives disponíveis usar, etc. Em vez de responder por eles com valores
# por omissão, guardamos aqui cada sessão e devolvemos ao frontend EXATAMENTE
# a pergunta que o rclone faria no terminal, para o utilizador decidir.
PENDING_REMOTE_SESSIONS = {}
_REMOTE_SESSIONS_LOCK = threading.Lock()

def _advance_remote_session(session_id: str, args: list, timeout: int = 300):
    """Corre um passo do assistente do rclone em background (pode demorar,
    ex: à espera do browser) e guarda o resultado na sessão.

    No Docker/ZimaOS o browser de quem está a usar a app corre sempre numa
    máquina diferente do container, por isso o fluxo "auto config" do
    rclone -- que tenta abrir um browser local e espera a resposta num
    mini-webserver em 127.0.0.1:53682 dentro do próprio container -- nunca
    consegue funcionar (o container não tem browser, e mesmo que tivesse,
    esse endereço 127.0.0.1 é local ao container, inacessível de fora).
    Assim que o rclone pergunta "Use auto config?" (Option "config_is_local"),
    respondemos "não" automaticamente em nome do utilizador. O rclone avança
    então para o modo "remote setup": devolve o comando exato
    "rclone authorize ..." para correr noutra máquina com browser, e fica à
    espera que o resultado seja colado de volta -- é esse passo (Option
    "config_token") que o wizard mostra ao utilizador com uma caixa de
    comando copiável e um campo para colar a resposta.
    Nas outras plataformas (Windows, macOS, Linux nativo), onde a app e o
    browser correm na mesma máquina, o comportamento mantém-se inalterado:
    a pergunta "Use auto config?" continua a ser feita normalmente.
    """
    def worker():
        current_args = args
        for _ in range(5):  # guarda contra loops infinitos; normalmente resolve-se numa única iteração
            data, out, err, code = run_rclone_json(current_args, timeout=timeout)
            with _REMOTE_SESSIONS_LOCK:
                session = PENDING_REMOTE_SESSIONS.get(session_id)
                if session is None:
                    return
                if data is None:
                    session["status"] = "error"
                    session["error"] = err or out or "Resposta inesperada do rclone."
                    return
                if data.get("Error"):
                    session["status"] = "error"
                    session["error"] = data["Error"]
                    return
                new_state = data.get("State") or ""
                if not new_state:
                    session["status"] = "done"
                    session["state"] = None
                    session["option"] = None
                    return
                option = data.get("Option") or {}
                name, p_type = session["name"], session["type"]

            if IS_CONTAINER and option.get("Name") == "config_is_local":
                current_args = ["config", "create", name, p_type, "--non-interactive",
                                 "--continue", "--state", new_state, "--result", "false"]
                continue

            with _REMOTE_SESSIONS_LOCK:
                session = PENDING_REMOTE_SESSIONS.get(session_id)
                if session is None:
                    return
                session["state"] = new_state
                session["option"] = option
                session["status"] = "input_required"
            return
    threading.Thread(target=worker, daemon=True).start()

@app.post("/api/remotes/create")
async def create_remote_wizard(req: Request):
    """Inicia a criação de um remote a partir do wizard (1º passo do fluxo
    --non-interactive do rclone)."""
    body = await req.json()
    name = (body.get("name") or "").strip().replace(" ", "_")
    p_type = body.get("type")
    options = body.get("options") or {}
    if not name or not p_type:
        return JSONResponse(status_code=400, content={"message": "Nome e serviço são obrigatórios."})

    session_id = uuid.uuid4().hex
    args = ["config", "create", name, p_type]
    for k, v in options.items():
        if v:
            args.append(f"{k}={v}")
    args.append("--non-interactive")

    with _REMOTE_SESSIONS_LOCK:
        PENDING_REMOTE_SESSIONS[session_id] = {
            "name": name, "type": p_type,
            "state": None, "status": "working",
            "option": None, "error": None,
        }
    _advance_remote_session(session_id, args)
    return {"session_id": session_id, "status": "working"}

@app.get("/api/remotes/create/{session_id}/status")
def get_remote_create_status(session_id: str):
    """O frontend faz polling a este endpoint para saber se há uma pergunta
    pendente, se terminou com sucesso, ou se falhou (ex: por permissões)."""
    with _REMOTE_SESSIONS_LOCK:
        session = PENDING_REMOTE_SESSIONS.get(session_id)
        if session is None:
            return JSONResponse(status_code=404, content={"message": "Sessão inválida ou expirada."})
        return {
            "status": session["status"],
            "option": session.get("option"),
            "error": session.get("error"),
        }

@app.post("/api/remotes/create/{session_id}/answer")
async def answer_remote_create(session_id: str, req: Request):
    """Recebe a resposta do utilizador a UMA pergunta pendente e avança o
    assistente do rclone com ela (equivalente a escrever no terminal)."""
    body = await req.json()
    value = body.get("value", "")

    with _REMOTE_SESSIONS_LOCK:
        session = PENDING_REMOTE_SESSIONS.get(session_id)
        if session is None:
            return JSONResponse(status_code=404, content={"message": "Sessão inválida ou expirada."})
        if session["status"] != "input_required":
            return JSONResponse(status_code=409, content={"message": "Não há nenhuma pergunta pendente nesta sessão."})
        state = session["state"]
        name, p_type = session["name"], session["type"]
        session["status"] = "working"

    args = ["config", "create", name, p_type, "--non-interactive",
            "--continue", "--state", state, "--result", str(value)]
    _advance_remote_session(session_id, args)
    return {"status": "working"}

@app.delete("/api/remotes/create/{session_id}")
def cancel_remote_create(session_id: str):
    """Cancela/limpa uma sessão de criação (ex: o utilizador fechou o modal)."""
    with _REMOTE_SESSIONS_LOCK:
        PENDING_REMOTE_SESSIONS.pop(session_id, None)
    return {"status": "cancelled"}


@app.get("/api/browse/remotes")
def list_remotes_endpoint():
    """Lista os nomes das clouds configuradas no rclone.conf"""
    try:
        # Adicionado o --config RCLONE_CONFIG para ele encontrar os teus remotes
        out = subprocess.check_output([RCLONE_EXE, "--config", RCLONE_CONFIG, "listremotes"], **_hidden_subprocess_kwargs()).decode("utf-8")
        return [l.strip().replace(":", "") for l in out.split("\n") if l.strip()]
    except Exception as e:
        print(f"Erro ao listar remotes: {e}")
        return []

@app.get("/api/browse/remotes_typed")
def list_remotes_typed_endpoint():
    """Lista nome + tipo de cada cloud configurada, sem consultar quota (rápido, para grelhas de seleção)."""
    try:
        out = subprocess.check_output([RCLONE_EXE, "--config", RCLONE_CONFIG, "listremotes", "--long"], **_hidden_subprocess_kwargs()).decode("utf-8")
        result = []
        for line in out.split("\n"):
            if ":" in line:
                name, _, type_part = line.partition(":")
                if name.strip():
                    result.append({"name": name.strip(), "type": type_part.strip().lower()})
        return result
    except Exception as e:
        print(f"Erro ao listar remotes com tipo: {e}")
        return []

@app.get("/api/system/home")
def system_home_endpoint():
    """Devolve a pasta inicial para arrancar o explorador local.

    No ZimaOS/Docker o storage do utilizador vive em /mnt/storage (a home
    do processo dentro do container, ex. /root, não interessa a ninguém).
    Nas outras plataformas (Windows, macOS, Linux nativo, mobile/PWA)
    mantém-se o comportamento anterior: a home do utilizador com sessão
    iniciada.
    """
    if IS_CONTAINER:
        container_storage = "/mnt/storage"
        if os.path.isdir(container_storage):
            return {"path": container_storage}
    home = os.path.expanduser("~")
    if IS_WINDOWS and not home.endswith("\\"):
        home += "\\"
    return {"path": home}

@app.get("/api/browse/cloud")
def browse_cloud_endpoint(remote: str, path: str = ""):
    """Lista as pastas dentro de uma cloud específica."""
    try:
        # Garante que o nome do remote tem os dois pontos no final
        remote_name = remote.replace(":", "") + ":"
        res = subprocess.check_output([
            RCLONE_EXE, "--config", RCLONE_CONFIG, "lsd", 
            f"{remote_name}{path.lstrip('/')}"
        ], **_hidden_subprocess_kwargs()).decode("utf-8")
        # Extrai o nome da pasta (o rclone lsd tem um formato fixo)
        return [line.split(None, 4)[4] for line in res.split("\n") if line.strip() and len(line.split(None, 4)) >= 5]
    except Exception as e:
        print(f"Erro ao navegar na cloud {remote}: {e}")
        return []

def get_remote_type(remote_name):
    try:
        res = subprocess.check_output([RCLONE_EXE, "--config", RCLONE_CONFIG, "listremotes", "--long"], **_hidden_subprocess_kwargs()).decode()
        for line in res.split("\n"):
            if line.startswith(remote_name.replace(":", "") + ":"): return line.split(":")[1].strip().lower()
    except: pass
    return "unknown"

async def check_single_remote(remote_name):
    try:
        proc = await asyncio.create_subprocess_exec(RCLONE_EXE, "--config", RCLONE_CONFIG, "lsd", f"{remote_name}:", "--max-depth", "1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **_hidden_subprocess_kwargs())
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=7.0)
        if proc.returncode == 0: return {"name": remote_name, "status": "online", "message": "OK"}
        return {"name": remote_name, "status": "offline", "message": "Erro de Token/Acesso"}
    except: return {"name": remote_name, "status": "offline", "message": "Timeout"}

async def update_health_cache():
    global HEALTH_CACHE, LAST_BOX_CHECK_TIME
    try:
        # 1. Obtemos a lista longa para saber o tipo de cada cloud (drive, box, onedrive...)
        out = subprocess.check_output([RCLONE_EXE, "--config", RCLONE_CONFIG, "listremotes", "--long"], **_hidden_subprocess_kwargs()).decode("utf-8")
        
        remotes_to_check = []
        now = time.time()
        
        for line in out.split("\n"):
            if ":" in line:
                name = line.split(":")[0].strip()
                type_name = line.split(":")[1].strip().lower()
                
                # 2. Lógica Especial para a BOX
                if type_name == "box":
                    # Se já temos a Box no cache e passaram menos de 60 min (3600s), não verificamos de novo
                    existing_box = next((item for item in HEALTH_CACHE if item["name"] == name), None)
                    if existing_box and (now - LAST_BOX_CHECK_TIME < 3600):
                        # Reutilizamos o estado anterior para não gastar o token
                        remotes_to_check.append(asyncio.sleep(0, result=existing_box))
                        continue
                    else:
                        LAST_BOX_CHECK_TIME = now
                
                # 3. Para as outras nuvens ou se for hora de validar a Box a sério
                remotes_to_check.append(check_single_remote(name))

        # Executa as verificações (as reais e as "falsas" da Box)
        HEALTH_CACHE = await asyncio.gather(*remotes_to_check)
        
        await manager.broadcast({"type": "health_update", "health": HEALTH_CACHE})
        
    except Exception as e:
        print(f">>> [HEALTH] Erro ao atualizar: {e}")



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
        "license_expired": license_is_expired(license_data),
        "license_info": {
            "plan": license_data.get("plan"),
            "device_name": license_data.get("device_name"),
            "activated_at": license_data.get("activated_at"),
            "expires_at": license_data.get("expires_at")
        }
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
        with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f).get(task_id, [])
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

@app.delete("/api/clouds/remove/{name}")
async def delete_cloud_config(name: str):
    """Remove permanentemente uma configuração de cloud do rclone.conf"""
    try:
        # IMPORTANTE: No rclone config delete, o nome vai SEM os dois pontos ":"
        clean_name = name.replace(":", "")
        
        print(f">>> [RCLONE] A tentar apagar a cloud: {clean_name}")
        
        # Executamos o comando e capturamos a saída
        result = subprocess.run([
            RCLONE_EXE, "--config", RCLONE_CONFIG, "config", "delete", clean_name
        ], capture_output=True, text=True, **_hidden_subprocess_kwargs())

        if result.returncode != 0:
            print(f">>> [RCLONE] Erro ao apagar: {result.stderr}")
            return JSONResponse(status_code=500, content={"message": result.stderr})

        # Limpar o cache de saúde imediatamente
        global HEALTH_CACHE
        HEALTH_CACHE = [c for c in HEALTH_CACHE if c['name'] != clean_name]
        
        print(f">>> [RCLONE] Cloud {clean_name} apagada com sucesso.")
        return {"status": "ok"}

    except Exception as e:
        print(f">>> [API] Erro ao processar pedido: {e}")
        return JSONResponse(status_code=500, content={"message": str(e)})
        
@app.get("/api/remotes/list_with_quota")
async def list_remotes_with_quota():
    if not os.path.exists(RCLONE_CONFIG):
        return []
    
    try:
        # 1. Lista remotes
        res_list = subprocess.run([RCLONE_EXE, "--config", RCLONE_CONFIG, "listremotes", "--long"], 
                                  capture_output=True, text=True, timeout=5, **_hidden_subprocess_kwargs())
        if res_list.returncode != 0: return []
        
        remotes = []
        for line in res_list.stdout.strip().split('\n'):
            if ':' in line:
                name = line.split(':')[0].strip()
                r_type = line.split(':')[1].strip()
                
                # 2. Tenta quota, mas com timeout curto para não travar a UI
                quota = {"total": 0, "used": 0, "free": 0, "supported": False}
                status, error_detail = "ok", None
                try:
                    res_about = subprocess.run([RCLONE_EXE, "--config", RCLONE_CONFIG, "about", f"{name}:", "--json"], 
                                             capture_output=True, text=True, timeout=8, **_hidden_subprocess_kwargs())
                    if res_about.returncode == 0:
                        data = json.loads(res_about.stdout)
                        quota = { "total": data.get("total", 0), "used": data.get("used", 0), "supported": True }
                    else:
                        err = (res_about.stderr or "").lower()
                        if "not supported" in err or "doesn't support" in err:
                            pass  # backend simplesmente não reporta quota, não é um erro de ligação
                        elif any(k in err for k in ["invalid_grant", "oauth", "token", "401", "403", "unauthorized", "authentication"]):
                            status, error_detail = "error", "auth"
                        elif any(k in err for k in ["timeout", "deadline", "i/o timeout"]):
                            status, error_detail = "error", "timeout"
                        elif any(k in err for k in ["no such host", "connection refused", "network is unreachable", "couldn't", "could not", "dial tcp"]):
                            status, error_detail = "error", "connection"
                        elif err.strip():
                            status, error_detail = "error", "unknown"
                except subprocess.TimeoutExpired:
                    status, error_detail = "error", "timeout"
                except Exception:
                    pass
                
                remotes.append({"name": name, "type": r_type, "quota": quota, "status": status, "error_detail": error_detail})
        return remotes
    except:
        return []


@app.get("/api/health")
async def get_health(): return HEALTH_CACHE

@app.post("/api/system/restart")
async def restart_server():
    """Reinicia o processo do SyncPulse (não a máquina/SO).
    Funciona da mesma forma em Docker, Windows, macOS ou Linux: relança
    o próprio processo com o mesmo comando que o iniciou (sys.executable +
    sys.argv), sem precisar de privilégios elevados nem de um supervisor
    externo configurado com política de restart."""
    async def _do_restart():
        await asyncio.sleep(0.6)  # dá tempo à resposta HTTP chegar ao cliente
        os.execv(sys.executable, [sys.executable] + sys.argv)
    asyncio.create_task(_do_restart())
    return {"status": "restarting"}


# --- Auto-atualização (só Windows) ------------------------------------------
def _version_tuple(v: str):
    """Converte "2.10" em (2, 10) para comparar versões numericamente
    (evita o erro clássico de comparar strings, onde "2.10" < "2.9")."""
    parts = []
    for p in str(v).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


async def _fetch_latest_windows_release():
    """Lista Downloads/Windows no GitHub (repo público, sem precisar de
    token) e devolve o instalador com o número de versão mais alto, a
    partir do próprio nome do ficheiro (ex: "SyncPulse v2.1_Setup.exe")."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{UPDATE_REPO}/contents/{UPDATE_DIR}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        items = resp.json()

    best = None
    for item in items:
        if item.get("type") != "file":
            continue
        m = UPDATE_FILENAME_RE.match(item.get("name", ""))
        if not m:
            continue
        version = m.group(1)
        if best is None or _version_tuple(version) > _version_tuple(best["version"]):
            best = {
                "version": version,
                "name": item["name"],
                "download_url": item["download_url"],
                "size": item["size"],
                "sha": item["sha"],
            }
    return best


@app.get("/api/update/check")
async def check_update_endpoint(force: bool = False):
    """Verifica se há um instalador Windows mais recente do que o que está a
    correr. Nas outras plataformas devolve sempre "sem atualização", sem
    gastar pedidos à API pública do GitHub (que tem limite de 60/hora)."""
    if not IS_WINDOWS:
        return {"available": False}

    now = time.time()
    if not force and (now - UPDATE_STATE["checked_at"]) < UPDATE_CHECK_INTERVAL:
        return {k: v for k, v in UPDATE_STATE.items() if k != "checked_at"}

    UPDATE_STATE["checked_at"] = now
    try:
        latest = await _fetch_latest_windows_release()
    except Exception:
        # Falha de rede ou GitHub em baixo -- não incomoda o utilizador,
        # simplesmente reporta "sem atualização" e tenta de novo mais tarde.
        UPDATE_STATE.update({"available": False, "latest_version": None,
                              "download_url": None, "size": None, "sha": None, "filename": None})
        return {k: v for k, v in UPDATE_STATE.items() if k != "checked_at"}

    if latest and _version_tuple(latest["version"]) > _version_tuple(APP_VERSION):
        UPDATE_STATE.update({
            "available": True, "latest_version": latest["version"],
            "download_url": latest["download_url"], "size": latest["size"],
            "sha": latest["sha"], "filename": latest["name"],
        })
    else:
        UPDATE_STATE.update({"available": False, "latest_version": latest["version"] if latest else None,
                              "download_url": None, "size": None, "sha": None, "filename": None})

    return {k: v for k, v in UPDATE_STATE.items() if k != "checked_at"}


@app.post("/api/update/apply")
async def apply_update_endpoint():
    """Descarrega o instalador mais recente, confirma a integridade (tamanho
    + hash, ambos vindos diretamente da API do GitHub) e arranca-o em modo
    silencioso. O próprio instalador (Inno Setup) fecha a app a correr e
    relança-a no fim; por segurança relançamo-la nós também a partir de um
    pequeno script auxiliar, para não depender só da configuração do
    instalador."""
    if not IS_WINDOWS:
        return JSONResponse(status_code=400, content={"message": "A atualização automática só está disponível na versão Windows."})
    if not getattr(sys, "frozen", False):
        return JSONResponse(status_code=400, content={"message": "Isto só funciona na versão instalada (.exe), não em modo de desenvolvimento."})
    if not UPDATE_STATE.get("available") or not UPDATE_STATE.get("download_url"):
        return JSONResponse(status_code=400, content={"message": "Não há nenhuma atualização disponível de momento."})

    download_url = UPDATE_STATE["download_url"]
    expected_size = UPDATE_STATE.get("size")
    expected_sha = UPDATE_STATE.get("sha")
    filename = UPDATE_STATE.get("filename") or "SyncPulseSetup.exe"

    update_dir = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "SyncPulseUpdate")
    os.makedirs(update_dir, exist_ok=True)
    installer_path = os.path.join(update_dir, filename)

    hasher = hashlib.sha1()
    if expected_size:
        hasher.update(f"blob {expected_size}\0".encode())
    total = 0
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            async with client.stream("GET", download_url) as resp:
                resp.raise_for_status()
                with open(installer_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
                        total += len(chunk)
                        hasher.update(chunk)
    except Exception as e:
        try: os.remove(installer_path)
        except OSError: pass
        return JSONResponse(status_code=502, content={"message": f"Falha ao descarregar a atualização: {e}"})

    if expected_size and total != expected_size:
        try: os.remove(installer_path)
        except OSError: pass
        return JSONResponse(status_code=502, content={"message": "O ficheiro descarregado está incompleto. Tenta novamente."})

    if expected_size and expected_sha and hasher.hexdigest() != expected_sha:
        try: os.remove(installer_path)
        except OSError: pass
        return JSONResponse(status_code=502, content={"message": "A verificação de integridade da atualização falhou. Tenta novamente."})

    # Script auxiliar: dá tempo à nossa app fechar-se (liberta os ficheiros),
    # corre o instalador em silêncio (/CLOSEAPPLICATIONS garante que fecha
    # a app se por algum motivo ainda estiver a correr, /RESTARTAPPLICATIONS
    # pede-lhe para relançar sozinho no fim), e relança-a também nós próprios
    # como rede de segurança, apontando exatamente para o .exe atual
    # (sys.executable -- funciona seja qual for a pasta onde está instalado).
    current_exe = sys.executable
    helper_path = os.path.join(update_dir, "run_update.bat")
    with open(helper_path, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'"{installer_path}" /VERYSILENT /SUPPRESSMSGBOX /NORESTART /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS\r\n'
            "timeout /t 3 /nobreak >nul\r\n"
            f'start "" "{current_exe}"\r\n'
            'del "%~f0"\r\n'
        )

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd", "/c", helper_path],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )

    def _exit_soon():
        time.sleep(1.0)
        os._exit(0)
    threading.Thread(target=_exit_soon, daemon=True).start()

    return {"status": "ok", "message": "A atualização vai começar. A aplicação vai fechar e reabrir sozinha."}


# --- Acesso remoto via Tailscale (só Windows) -------------------------------
def _find_tailscale_exe():
    """Localiza o executável do Tailscale de forma robusta.

    Não confiamos só no PATH do processo (os.environ["PATH"] fica gravado
    no arranque do SyncPulse -- se o Tailscale for instalado DEPOIS, só um
    reinício da app veria o PATH atualizado) nem numa única pasta fixa (o
    Tailscale já instalou historicamente em "Tailscale IPN", "Tailscale",
    e em "Program Files (x86)" consoante a versão/arquitetura). Por isso
    tentamos, por ordem, várias fontes independentes:
      1. PATH do processo atual (caso já esteja lá).
      2. PATH lido em direto do registo do Windows (sempre atual, mesmo
         sem reiniciar o SyncPulse).
      3. O registo do serviço "Tailscale" do Windows, que aponta sempre
         para a pasta real de instalação, seja qual for a versão.
      4. Uma lista de pastas conhecidas, como último recurso.
    """
    found = shutil.which("tailscale")
    if found:
        return found

    if IS_WINDOWS:
        try:
            import winreg
        except ImportError:
            winreg = None

        if winreg:
            # 2. PATH atual do registo (Máquina + Utilizador), sem depender
            # do que este processo tinha em memória no arranque.
            for hive, subkey in (
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                (winreg.HKEY_CURRENT_USER, r"Environment"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        path_value, _ = winreg.QueryValueEx(key, "Path")
                    for folder in path_value.split(os.pathsep):
                        candidate = os.path.join(folder.strip('"'), "tailscale.exe")
                        if os.path.exists(candidate):
                            return candidate
                except OSError:
                    pass

            # 3. Pasta real de instalação, via registo do serviço do Windows
            # (tailscaled.exe corre como serviço "Tailscale"; tailscale.exe
            # vive sempre ao lado dele, seja qual for a pasta escolhida).
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\Tailscale") as key:
                    image_path, _ = winreg.QueryValueEx(key, "ImagePath")
                # O ImagePath vem tipicamente como '"C:\caminho\tailscaled.exe" /subproc'
                # -- as aspas fecham logo a seguir ao .exe, com argumentos depois.
                # Um simples .strip('"') não apanha isto (só limpa as pontas da
                # string toda), por isso isolamos o executável explicitamente.
                image_path = image_path.strip()
                if image_path.startswith('"'):
                    end_quote = image_path.find('"', 1)
                    exe_path = image_path[1:end_quote] if end_quote != -1 else image_path[1:]
                else:
                    exe_path = image_path.split(" ")[0]
                candidate = os.path.join(os.path.dirname(exe_path), "tailscale.exe")
                if os.path.exists(candidate):
                    return candidate
            except OSError:
                pass

    # 4. Últimos recursos: pastas conhecidas de instalações históricas.
    for default_path in (
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files\Tailscale IPN\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale IPN\tailscale.exe",
    ):
        if os.path.exists(default_path):
            return default_path

    return None


def _run_tailscale(args, timeout=20):
    """Corre um comando do Tailscale CLI. Devolve (resultado, motivo_erro)."""
    exe = _find_tailscale_exe()
    if not exe:
        return None, "not_installed"
    try:
        result = subprocess.run([exe] + list(args), capture_output=True, text=True,
                                 timeout=timeout, **_hidden_subprocess_kwargs())
        return result, None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


@app.get("/api/remote-access/status")
async def remote_access_status():
    """Estado do acesso remoto via Tailscale, para as Preferências saberem
    que passo mostrar: instalado? sessão iniciada? HTTPS (Serve) já ativo?"""
    if not IS_WINDOWS:
        return {"supported": False}

    exe = _find_tailscale_exe()
    if not exe:
        return {"supported": True, "installed": False, "logged_in": False, "https_active": False, "url": None, "port": MDNS_PORT}

    result, err = _run_tailscale(["status", "--json"], timeout=10)
    if err or result is None or result.returncode != 0:
        # Instalado mas o serviço pode ainda não ter arrancado -- não é um erro fatal.
        return {"supported": True, "installed": True, "logged_in": False, "https_active": False, "url": None, "port": MDNS_PORT}

    try:
        data = json.loads(result.stdout)
    except Exception:
        return {"supported": True, "installed": True, "logged_in": False, "https_active": False, "url": None, "port": MDNS_PORT}

    logged_in = data.get("BackendState") == "Running"
    dns_name = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")

    https_active, url = False, None
    if logged_in and dns_name:
        serve_result, serve_err = _run_tailscale(["serve", "status"], timeout=10)
        if not serve_err and serve_result and serve_result.returncode == 0:
            out = serve_result.stdout or ""
            if out.strip() and "No serve config" not in out:
                https_active = True
                url = f"https://{dns_name}"

    return {
        "supported": True, "installed": True, "logged_in": logged_in,
        "https_active": https_active, "url": url, "port": MDNS_PORT,
    }


@app.post("/api/remote-access/enable")
async def remote_access_enable():
    """Ativa o Tailscale Serve na porta do SyncPulse e devolve o URL HTTPS
    final (para colar na app mobile). Exige que o Tailscale já esteja
    instalado e com sessão iniciada -- ver /api/remote-access/status."""
    if not IS_WINDOWS:
        return JSONResponse(status_code=400, content={"message": "Só disponível na versão Windows."})

    exe = _find_tailscale_exe()
    if not exe:
        return JSONResponse(status_code=400, content={"message": "O Tailscale não está instalado."})

    status_result, err = _run_tailscale(["status", "--json"], timeout=10)
    if err or status_result is None or status_result.returncode != 0:
        return JSONResponse(status_code=400, content={"message": "Não foi possível falar com o Tailscale. Confirma que está aberto e com sessão iniciada."})

    try:
        data = json.loads(status_result.stdout)
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Resposta inesperada do Tailscale."})

    if data.get("BackendState") != "Running":
        return JSONResponse(status_code=400, content={"message": "Inicia sessão no Tailscale primeiro."})

    dns_name = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not dns_name:
        return JSONResponse(status_code=400, content={"message": "Não foi possível obter o nome do dispositivo no Tailscale."})

    serve_result, err = _run_tailscale(["serve", "--bg", str(MDNS_PORT)], timeout=25)
    if err or serve_result is None or serve_result.returncode != 0:
        return JSONResponse(status_code=502, content={
            "message": "Não foi possível ativar o acesso remoto. Confirma que o HTTPS está ativado em "
                        "https://login.tailscale.com/admin/dns (secção \"HTTPS Certificates\") e tenta novamente."
        })

    return {"status": "ok", "url": f"https://{dns_name}"}


@app.get("/api/discover")
async def discover_ping():
    """Endpoint leve e sem autenticação usado pela app mobile para confirmar
    que um endereço (mDNS, IP manual ou detetado) é mesmo um servidor SyncPulse
    válido antes de o guardar como servidor ativo."""
    return {
        "app": "syncpulse",
        "edition": "zimaos" if IS_CONTAINER else ("windows" if IS_WINDOWS else ("macos" if IS_MACOS else "linux")),
        "version": APP_VERSION if IS_WINDOWS else "1.1",
        "device_name": (platform.node() or "syncpulse").split(".")[0],
        "hwid_short": get_secure_hwid()[:12],
    }

if IS_CONTAINER:
    FRONTEND_FILE = "index.html"
elif IS_WINDOWS:
    FRONTEND_FILE = "index_win.html"
elif IS_MACOS:
    FRONTEND_FILE = "index_mac.html"
else:
    FRONTEND_FILE = "index_linux.html"

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(WWW_PATH, FRONTEND_FILE))

# Fora do container, a pasta "www" (frontend) tem de ser distribuída junto do
# main.py. Se faltar (ex. instalação incompleta), criamos uma pasta vazia em
# vez de deixar o StaticFiles rebentar o arranque do servidor todo.
if not os.path.isdir(WWW_PATH):
    print(f">>> [AVISO] Pasta do frontend não encontrada em: {WWW_PATH}")
    print(">>> [AVISO] Copia a pasta 'www' para esse caminho (ou define SYNCPULSE_WWW_PATH).")
    os.makedirs(WWW_PATH, exist_ok=True)
if not os.path.isfile(os.path.join(WWW_PATH, FRONTEND_FILE)):
    raise RuntimeError(f"Frontend '{FRONTEND_FILE}' não encontrado em: {WWW_PATH}")
app.mount("/", StaticFiles(directory=WWW_PATH), name="static")

def run_native_desktop_window():
    """Arranca o FastAPI em background e abre a interface numa janela nativa."""
    # O WebKitGTK pode renderizar uma janela totalmente branca em VMware quando
    # a composição acelerada está ativa. Esta opção afeta apenas Linux/GTK e
    # mantém o frontend funcional em máquinas físicas e virtuais.
    if not IS_MACOS and not IS_WINDOWS:
        os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    try:
        import webview
    except ImportError as error:
        raise RuntimeError("Falta pywebview. Corre: pip install pywebview") from error

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=MDNS_PORT, log_level="info"
    ))
    server_thread = threading.Thread(target=server.run, name="syncpulse-server", daemon=True)
    server_thread.start()

    # Só abre a janela quando o backend já está pronto a responder.
    deadline = time.monotonic() + 15
    url = f"http://127.0.0.1:{MDNS_PORT}"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.5):
                break
        except Exception:
            time.sleep(0.15)
    else:
        server.should_exit = True
        raise RuntimeError("O servidor local do SyncPulse não arrancou.")

    webview.create_window(
        "SyncPulse", url, width=1440, height=900, min_size=(960, 640)
    )
    try:
        # macOS usa Cocoa; Linux usa GTK/WebKit. Docker continua sem GUI.
        webview.start(gui="cocoa" if IS_MACOS else "gtk")
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)

if __name__ == "__main__":
    # Só relevante fora do Docker (ZimaOS arranca isto via "uvicorn main:app"
    # no CMD do Dockerfile, nunca chega a executar este bloco). Para
    # Windows, macOS e Linux nativos -- incluindo builds empacotados com
    # PyInstaller a apontar diretamente para este ficheiro -- isto é o que
    # efetivamente arranca o servidor.
    if IS_MACOS or (not IS_WINDOWS and not IS_CONTAINER):
        run_native_desktop_window()
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=MDNS_PORT)
