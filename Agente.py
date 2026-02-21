#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

import requests
from colorama import init, Fore, Style
from tqdm import tqdm
import psutil

init(autoreset=True)

# ========= CONFIG =========
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")  # usa 0.5b si va lento

WORKSPACE = Path(os.getenv("AGENT_WORKSPACE", str(Path.cwd() / "AGENT_WORKSPACE"))).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

MAX_MEMORY_TURNS = 6
REQUEST_TIMEOUT = 180
RETRIES = 2
APPROVAL_REQUIRED = True

# Apps permitidas (lista blanca)
ALLOWED_APPS = {
    "notepad": {"cmd": ["notepad.exe"], "desc": "Bloc de notas"},
    "calculator": {"cmd": ["calc.exe"], "desc": "Calculadora"},
    "explorer_workspace": {"cmd": ["explorer.exe", str(WORKSPACE)], "desc": "Explorador en workspace"},
    "cmd": {"cmd": ["cmd.exe"], "desc": "Símbolo del sistema"},
    "chrome": {"cmd": ["cmd.exe", "/c", "start", "chrome"], "desc": "Google Chrome (si está instalado)"},
}

EXT_GROUPS = {
    "imagenes": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "audio": {".mp3", ".wav", ".aac", ".m4a", ".ogg"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm"},
    "docs": {".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx"},
    "code": {".py", ".js", ".ts", ".html", ".css", ".json"},
}

TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".csv", ".log"}


# ========= LOGS =========
def log_info(msg: str) -> None:
    print(Fore.CYAN + "[INFO] " + Style.RESET_ALL + msg)

def log_ok(msg: str) -> None:
    print(Fore.GREEN + "[OK] " + Style.RESET_ALL + msg)

def log_warn(msg: str) -> None:
    print(Fore.YELLOW + "[WARN] " + Style.RESET_ALL + msg)

def log_err(msg: str) -> None:
    print(Fore.RED + "[ERROR] " + Style.RESET_ALL + msg)

def log_user(msg: str) -> None:
    print(Fore.MAGENTA + "Tú: " + Style.RESET_ALL + msg)

def log_ai(msg: str) -> None:
    print(Fore.WHITE + "IA: " + Style.RESET_ALL + msg)

def show_system_status() -> None:
    vm = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.2)
    log_info(f"CPU: {cpu:.0f}% | RAM libre: {vm.available/1024/1024:.0f} MB | RAM usada: {vm.percent:.0f}%")

def show_runtime_identity() -> None:
    log_info(f"Archivo en ejecución: {Path(__file__).resolve()}")
    log_info(f"Workspace: {WORKSPACE}")


# ========= SEGURIDAD =========
def safe_path(rel_path: str) -> Path:
    rel_path = rel_path.strip().lstrip("\\/")  # evita rutas absolutas por accidente
    p = (WORKSPACE / rel_path).resolve()
    if not str(p).startswith(str(WORKSPACE)):
        raise ValueError("Ruta inválida: fuera del workspace.")
    return p

def require_approval(action: str, details: str) -> bool:
    if not APPROVAL_REQUIRED:
        return True
    tqdm.write(Fore.YELLOW + f"\n¿Permitir acción? {action}\nDetalles: {details}\n(S/N): " + Style.RESET_ALL)
    ans = input().strip().lower()
    return ans == "s"

def double_confirm_delete(file_path: Path) -> bool:
    if not require_approval("DELETE_FILE (1/2)", str(file_path)):
        return False
    tqdm.write(Fore.RED + f"\nCONFIRMACIÓN FINAL (2/2): escribe EXACTAMENTE el nombre del archivo a borrar:\n{file_path.name}\n> " + Style.RESET_ALL)
    typed = input().strip()
    return typed == file_path.name


# ========= TOOLS =========
def tool_write_file(path: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
    file_path = safe_path(path)

    if file_path.exists() and not overwrite:
        return {"ok": False, "error": "El archivo ya existe. Usa overwrite=true si quieres sobrescribir."}

    if not require_approval("WRITE_FILE", f"{file_path} (overwrite={overwrite})"):
        return {"ok": False, "error": "Acción cancelada por el usuario."}

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    # verificación real
    exists = file_path.exists()
    size = file_path.stat().st_size if exists else 0
    return {"ok": True, "path": str(file_path), "exists": exists, "size": size}

def tool_read_file(path: str, max_chars: int = 6000) -> Dict[str, Any]:
    file_path = safe_path(path)
    if not file_path.exists():
        return {"ok": False, "error": "Archivo no existe."}
    data = file_path.read_text(encoding="utf-8", errors="replace")
    if len(data) > max_chars:
        data = data[:max_chars] + "\n... (recortado)"
    return {"ok": True, "path": str(file_path), "content": data}

def tool_list_files(subdir: str = "") -> Dict[str, Any]:
    base = safe_path(subdir) if subdir else WORKSPACE
    if not base.exists():
        return {"ok": False, "error": "Directorio no existe."}
    files = []
    for p in base.rglob("*"):
        if p.is_file():
            files.append(str(p.relative_to(WORKSPACE)))
    files.sort()
    return {"ok": True, "files": files[:500], "count": len(files)}

def tool_open_app(app_key: str) -> Dict[str, Any]:
    if app_key not in ALLOWED_APPS:
        return {"ok": False, "error": f"App no permitida. Permitidas: {list(ALLOWED_APPS.keys())}"}

    app = ALLOWED_APPS[app_key]
    if not require_approval("OPEN_APP", f"{app_key} -> {app['desc']}"):
        return {"ok": False, "error": "Acción cancelada por el usuario."}

    try:
        subprocess.Popen(app["cmd"], shell=False)
        return {"ok": True, "opened": app_key, "desc": app["desc"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def tool_organize_folder(subdir: str = "", mode: str = "move") -> Dict[str, Any]:
    if mode not in {"move", "copy"}:
        return {"ok": False, "error": "mode debe ser 'move' o 'copy'."}

    base = safe_path(subdir) if subdir else WORKSPACE
    if not base.exists():
        return {"ok": False, "error": "Directorio no existe."}

    if not require_approval("ORGANIZE_FOLDER", f"{base} (mode={mode})"):
        return {"ok": False, "error": "Acción cancelada por el usuario."}

    for folder in list(EXT_GROUPS.keys()) + ["otros"]:
        (base / folder).mkdir(parents=True, exist_ok=True)

    moved = []
    skipped = 0

    for p in base.iterdir():
        if p.is_file():
            ext = p.suffix.lower()
            dest_folder = "otros"
            for group, exts in EXT_GROUPS.items():
                if ext in exts:
                    dest_folder = group
                    break

            dest = base / dest_folder / p.name
            if dest.resolve() == p.resolve():
                skipped += 1
                continue

            try:
                if mode == "move":
                    shutil.move(str(p), str(dest))
                else:
                    shutil.copy2(str(p), str(dest))
                moved.append({"from": str(p.name), "to": str(Path(dest_folder) / p.name)})
            except Exception:
                skipped += 1

    return {"ok": True, "moved_count": len(moved), "skipped": skipped, "moved": moved[:50]}

def tool_delete_file(path: str) -> Dict[str, Any]:
    file_path = safe_path(path)
    if not file_path.exists() or not file_path.is_file():
        return {"ok": False, "error": "Archivo no existe."}

    if not double_confirm_delete(file_path):
        return {"ok": False, "error": "Borrado cancelado por el usuario."}

    try:
        file_path.unlink()
        return {"ok": True, "deleted": str(file_path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def tool_rename_file(src: str, dst: str, overwrite: bool = False) -> Dict[str, Any]:
    src_path = safe_path(src)
    dst_path = safe_path(dst)

    if not src_path.exists() or not src_path.is_file():
        return {"ok": False, "error": "Archivo origen no existe."}

    if dst_path.exists() and not overwrite:
        return {"ok": False, "error": "Destino ya existe. Usa overwrite=true para reemplazar."}

    if not require_approval("RENAME_FILE", f"{src_path} -> {dst_path} (overwrite={overwrite})"):
        return {"ok": False, "error": "Acción cancelada por el usuario."}

    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        if overwrite and dst_path.exists():
            dst_path.unlink()
        src_path.rename(dst_path)
        return {"ok": True, "from": str(src_path), "to": str(dst_path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def tool_search_text(query: str, subdir: str = "", case_sensitive: bool = False, max_hits: int = 50) -> Dict[str, Any]:
    base = safe_path(subdir) if subdir else WORKSPACE
    if not base.exists():
        return {"ok": False, "error": "Directorio no existe."}

    if not require_approval("SEARCH_TEXT", f"Buscar '{query}' en {base}"):
        return {"ok": False, "error": "Acción cancelada por el usuario."}

    hits = []
    q = query if case_sensitive else query.lower()

    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXTS:
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        hay = text if case_sensitive else text.lower()
        if q in hay:
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                line_cmp = line if case_sensitive else line.lower()
                if q in line_cmp:
                    hits.append({
                        "file": str(p.relative_to(WORKSPACE)),
                        "line": idx + 1,
                        "text": line[:300]
                    })
                    if len(hits) >= max_hits:
                        return {"ok": True, "query": query, "hits": hits, "truncated": True}

    return {"ok": True, "query": query, "hits": hits, "truncated": False}

def tool_create_project_folder(project: str = "", include_date: bool = True, project_name: str = "", folder_name: str = "") -> Dict[str, Any]:
    # compatibilidad con modelos que se inventan args
    if not project and project_name:
        project = project_name
    if folder_name and not project:
        project = folder_name

    project_clean = "".join(c for c in project.strip() if c not in r'<>:"/\|?*').strip()
    if not project_clean:
        return {"ok": False, "error": "Nombre de proyecto inválido."}

    stamp = datetime.now().strftime("%Y-%m-%d") if include_date else ""
    folder_name_final = f"{stamp}_{project_clean}" if stamp else project_clean
    folder_rel = Path("proyectos") / folder_name_final
    folder_path = safe_path(str(folder_rel))

    if not require_approval("CREATE_PROJECT_FOLDER", str(folder_path)):
        return {"ok": False, "error": "Acción cancelada por el usuario."}

    try:
        folder_path.mkdir(parents=True, exist_ok=True)
        for sub in ["entrada", "salida", "notas", "assets"]:
            (folder_path / sub).mkdir(exist_ok=True)
        return {"ok": True, "folder": str(folder_path), "relative": str(folder_rel)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ========= TOOLS REGISTRY (OBLIGATORIO) =========
TOOLS = {
    "write_file": tool_write_file,
    "read_file": tool_read_file,
    "list_files": tool_list_files,
    "open_app": tool_open_app,
    "organize_folder": tool_organize_folder,
    "delete_file": tool_delete_file,
    "rename_file": tool_rename_file,
    "search_text": tool_search_text,
    "create_project_folder": tool_create_project_folder,
}

# ========= ROUTER (comandos críticos sin LLM) =========
def try_direct_command(user_input: str) -> Optional[str]:
    s = user_input.strip()

    # Crear archivo (fiable)
    m = re.match(r'(?i)^\s*crea\s+el\s+archivo\s+(.+?)\s+con\s+el\s+contenido:\s*(.+)\s*$', s)
    if m:
        path = m.group(1).strip().strip('"').strip("'")
        content = m.group(2)

        log_info("Router: detectado -> write_file")
        res = tool_write_file(path=path, content=content, overwrite=True)
        if res.get("ok"):
            return f"✅ Archivo creado en: {res['path']} | existe={res['exists']} | size={res['size']} bytes"
        return f"❌ No se pudo crear: {res.get('error')}"

    # Listar archivos
    if re.match(r'(?i)^\s*lista\s+los\s+archivos\s*$', s):
        log_info("Router: detectado -> list_files")
        res = tool_list_files()
        if res.get("ok"):
            if res["count"] == 0:
                return "No hay archivos en el workspace."
            return "Archivos:\n" + "\n".join(res["files"])
        return f"Error: {res.get('error')}"

    # Leer archivo
    m = re.match(r'(?i)^\s*(lee|leer)\s+(.+?)\s*$', s)
    if m:
        path = m.group(2).strip().strip('"').strip("'")
        log_info("Router: detectado -> read_file")
        res = tool_read_file(path=path)
        if res.get("ok"):
            return f"Contenido de {path}:\n{res['content']}"
        return f"❌ No se pudo leer: {res.get('error')}"

    return None


# ========= OLLAMA =========
def call_ollama(prompt: str) -> str:
    payload = {"model": MODEL, "prompt": prompt, "stream": False}
    last_err = None

    for attempt in range(RETRIES + 1):
        try:
            t0 = time.time()
            log_info(f"Llamando al modelo ({MODEL})... intento {attempt+1}/{RETRIES+1}")
            r = requests.post(OLLAMA_URL, json=payload, timeout=REQUEST_TIMEOUT)
            dt = time.time() - t0
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            out = r.json().get("response", "")
            log_ok(f"Respuesta recibida en {dt:.1f}s")
            return out
        except Exception as e:
            last_err = e
            log_warn(f"Fallo llamada al modelo: {e}")
            time.sleep(1.0)

    raise RuntimeError(f"No se pudo contactar con Ollama: {last_err}")

# ========= PROTOCOLO =========
SYSTEM_INSTRUCTIONS = f"""
Eres un asistente con herramientas. SIEMPRE responde con JSON en una sola línea.
No inventes acciones.

WORKSPACE: {WORKSPACE}

Formato:
{{ "action": "reply", "text": "..." }}
o
{{ "action": "tool", "tool_name": "{'|'.join(TOOLS.keys())}", "args": {{ ... }} }}

Reglas:
- Paths siempre relativos al workspace (ej: "notas/idea.txt")
- open_app: app_key debe ser una de: {list(ALLOWED_APPS.keys())}
- delete_file: requiere doble confirmación del usuario
- search_text: query obligatorio
- create_project_folder: project obligatorio
- Sé breve y directo.
"""

def parse_action(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None

# ========= AGENTE =========
memory: List[str] = []

def build_prompt(user_input: str) -> str:
    hist = "\n".join(memory[-MAX_MEMORY_TURNS:])
    return f"""{SYSTEM_INSTRUCTIONS}

HISTORIAL:
{hist}

USUARIO: {user_input}
RESPUESTA:"""

def run_agent_turn(user_input: str) -> str:
    show_system_status()

    # Comandos críticos sin LLM (creación real de archivos)
    direct = try_direct_command(user_input)
    if direct is not None:
        return direct

    for _ in tqdm(range(12), desc="Pensando", unit="paso", leave=False):
        time.sleep(0.02)

    raw = call_ollama(build_prompt(user_input))
    act = parse_action(raw)

    if not act:
        log_warn("El modelo no devolvió JSON válido. Respondo en texto.")
        return raw.strip()

    if act.get("action") == "reply":
        return str(act.get("text", "")).strip()

    if act.get("action") == "tool":
        tool_name = act.get("tool_name")
        args = act.get("args", {}) or {}
        if tool_name not in TOOLS:
            return f"No puedo: herramienta desconocida '{tool_name}'."

        log_info(f"Ejecutando herramienta: {tool_name} con args={args}")
        for _ in tqdm(range(10), desc=f"Herramienta {tool_name}", unit="paso", leave=False):
            time.sleep(0.02)

        try:
            result = TOOLS[tool_name](**args)
        except TypeError as e:
            return f"Error de argumentos para {tool_name}: {e}"
        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"

        followup = f"""{SYSTEM_INSTRUCTIONS}

El usuario pidió: {user_input}

Resultado de la herramienta ({tool_name}):
{json.dumps(result, ensure_ascii=False)}

Ahora responde con:
{{ "action": "reply", "text": "..." }}
"""
        raw2 = call_ollama(followup)
        act2 = parse_action(raw2)
        if act2 and act2.get("action") == "reply":
            return str(act2.get("text", "")).strip()

        return f"Resultado: {result}"

    return "No entendí la acción solicitada."

def main():
    log_ok("Agente iniciado.")
    show_runtime_identity()
    log_info(f"Modelo: {MODEL}")
    log_info("Tip: Para CREAR archivos usa: 'Crea el archivo notas/idea.txt con el contenido: ...'")
    log_info("Tip: 'lista los archivos' para verificar.")
    log_info("Escribe 'salir' para terminar.\n")

    while True:
        user = input(Fore.MAGENTA + "Tú: " + Style.RESET_ALL).strip()
        if not user:
            continue
        if user.lower() in {"salir", "exit", "quit"}:
            log_ok("Cerrando agente.")
            break

        log_user(user)
        try:
            reply = run_agent_turn(user)
            log_ai(reply)
            memory.append(f"Usuario: {user}")
            memory.append(f"Agente: {reply}")
        except Exception as e:
            log_err(str(e))

if __name__ == "__main__":
    main()