"""
Sandbox 컨테이너 관리 모듈

유저당 1개 컨테이너 정책:
- 컨테이너명: sandbox-{user_id}
- 포트 범위: 9000-9999
- 유휴 10분 후 자동 중지
- 포트 매핑은 JSON 파일로 영속 저장
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import time as _time
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

# ── 상수 ────────────────────────────────────────────────
PORT_RANGE_START = 9000
PORT_RANGE_END = 9999
IDLE_TIMEOUT_SEC = 600  # 10분
PORT_MAP_FILE = Path(os.environ.get("PORT_MAP_FILE", "/app/data/sandbox_ports.json"))
_SANDBOX_META_FILE = Path(os.environ.get("SANDBOX_META_FILE", "/app/data/sandbox_meta.json"))
HOST_WORKSPACE = os.environ.get("HOST_WORKSPACE_DIR", "./workspace")
CONTAINER_WORKSPACE = os.environ.get("WORKSPACE_DIR", "/app/workspace")


def _container_to_host_path(container_path: str) -> str:
    """컨테이너 내부 경로를 호스트 경로로 변환 (docker.sock 경유 시 필요)"""
    if container_path.startswith(CONTAINER_WORKSPACE):
        rel = os.path.relpath(container_path, CONTAINER_WORKSPACE)
        return os.path.join(HOST_WORKSPACE, rel)
    return container_path


# 유저별 마지막 activity 시각 (in-memory)
_activity: dict[str, float] = {}


def _ensure_gradle_wrapper(project_dir: Path) -> None:
    """gradlew가 있는데 gradle-wrapper.jar가 없으면 자동 보충"""
    search_dirs = [project_dir] + [p for p in project_dir.iterdir() if p.is_dir()]
    for d in search_dirs:
        gradlew = d / "gradlew"
        if not gradlew.is_file():
            continue
        wrapper_dir = d / "gradle" / "wrapper"
        wrapper_jar = wrapper_dir / "gradle-wrapper.jar"
        wrapper_props = wrapper_dir / "gradle-wrapper.properties"
        if wrapper_jar.is_file():
            continue
        if not wrapper_props.is_file():
            continue

        # gradle-wrapper.properties에서 버전 파싱
        gradle_version = "8.5"
        try:
            import re as _re
            props_text = wrapper_props.read_text(encoding="utf-8")
            for line in props_text.splitlines():
                if "distributionUrl" in line:
                    m = _re.search(r"gradle-(\d+\.\d+(?:\.\d+)?)-", line)
                    if m:
                        gradle_version = m.group(1)
                    break
        except Exception:
            pass

        # gradle wrapper jar 다운로드
        log.info(f"gradle-wrapper.jar 누락 → 자동 다운로드 시도 (gradle {gradle_version})")
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        jar_url = f"https://raw.githubusercontent.com/gradle/gradle/v{gradle_version}/gradle/wrapper/gradle-wrapper.jar"
        try:
            result = subprocess.run(
                ["curl", "-fsSL", "-o", str(wrapper_jar), jar_url],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and wrapper_jar.is_file() and wrapper_jar.stat().st_size > 1000:
                log.info(f"gradle-wrapper.jar 다운로드 성공: {wrapper_jar}")
            else:
                # 실패 시 jar 파일 정리
                if wrapper_jar.is_file():
                    wrapper_jar.unlink()
                log.warning(f"gradle-wrapper.jar 다운로드 실패 (gradle {gradle_version})")
        except Exception as e:
            if wrapper_jar.is_file():
                wrapper_jar.unlink()
            log.warning(f"gradle-wrapper.jar 다운로드 실패: {e}")


# ── 포트 매핑 영속 저장 ─────────────────────────────────

def _load_port_map() -> dict[str, int]:
    if PORT_MAP_FILE.exists():
        try:
            data = json.loads(PORT_MAP_FILE.read_text(encoding="utf-8"))
            return {k: int(v) for k, v in data.items()}
        except Exception:
            return {}
    return {}


def _save_port_map(port_map: dict[str, int]) -> None:
    PORT_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORT_MAP_FILE.write_text(
        json.dumps(port_map, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Sandbox 메타 (프로젝트 추적) ─────────────────────────

def _load_sandbox_meta() -> dict:
    """유저별 sandbox 프로젝트 연결 정보 로드"""
    if _SANDBOX_META_FILE.is_file():
        try:
            return json.loads(_SANDBOX_META_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_sandbox_meta(meta: dict) -> None:
    _SANDBOX_META_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SANDBOX_META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── 포트 할당 ───────────────────────────────────────────

def _is_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(("127.0.0.1", port))
            return result != 0  # 연결 실패 = 포트 사용 가능
    except OSError:
        return True


def allocate_port(user_id: str) -> int:
    """유저에게 포트 할당 (70000-79999). 기존 할당 있으면 재사용."""
    port_map = _load_port_map()
    if user_id in port_map:
        return port_map[user_id]

    used_ports = set(port_map.values())
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port in used_ports:
            continue
        if _is_port_available(port):
            port_map[user_id] = port
            _save_port_map(port_map)
            log.info(f"포트 할당: user={user_id}, port={port}")
            return port

    raise RuntimeError(f"사용 가능한 포트 없음 (범위: {PORT_RANGE_START}-{PORT_RANGE_END})")


# ── Docker CLI 래퍼 ─────────────────────────────────────

def _docker_run(*args: str, timeout: int = 30) -> tuple[bool, str]:
    cmd = ["docker"] + list(args)
    log.info(f"Docker CMD: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        # stdout + stderr 합쳐서 전체 출력 확보 (buildkit은 stderr에 에러를 씀)
        output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        if result.returncode != 0:
            log.warning(f"Docker CMD 실패 (rc={result.returncode}): {output[:500]}")
            return False, output
        return True, output
    except subprocess.TimeoutExpired:
        return False, "명령 시간 초과"
    except Exception as e:
        return False, str(e)


def _container_exists(container_name: str) -> bool:
    ok, output = _docker_run(
        "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}",
    )
    return ok and container_name in output


def _container_running(container_name: str) -> bool:
    ok, output = _docker_run(
        "ps", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}",
    )
    return ok and container_name in output


# ── Activity 추적 ───────────────────────────────────────

def touch_activity(user_id: str) -> None:
    _activity[user_id] = _time.monotonic()


# ── 핵심 API ────────────────────────────────────────────

def _auto_start_service(user_id: str, project_path: str) -> str | None:
    """컨테이너 기동 후 프로젝트 유형에 맞는 웹 서비스를 자동 시작.
    /app/project 내 프로젝트 파일을 검사하여 적절한 서버 명령어를 백그라운드 실행.
    """
    container_name = f"sandbox-{user_id}"
    if not _container_running(container_name):
        return None

    # 이미 서비스가 기동 중인지 확인 (8080 포트 리스닝)
    ok, output = _docker_run(
        "exec", container_name, "sh", "-c",
        "grep -qi ':1F90' /proc/net/tcp 2>/dev/null && echo 'PORT_ACTIVE' || echo 'PORT_INACTIVE'",
        timeout=5,
    )
    if ok and "PORT_ACTIVE" in output:
        log.info(f"서비스 이미 기동 중: {container_name}:8080")
        return "already_running"

    # 프로젝트 파일 목록 확인
    ok, files_output = _docker_run(
        "exec", container_name, "sh", "-c",
        "ls /app/project/ 2>/dev/null",
        timeout=5,
    )
    if not ok:
        return None

    files = files_output.split()
    service_cmd = None

    # 프레임워크별 서비스 시작 명령어 결정
    if "build.gradle" in files or "build.gradle.kts" in files:
        # Java/Spring Boot — gradlew가 있으면 사용
        if "gradlew" in files:
            service_cmd = "cd /app/project && chmod +x gradlew && nohup ./gradlew bootRun > /tmp/service.log 2>&1 &"
        elif "pom.xml" in files:
            service_cmd = "cd /app/project && nohup mvn spring-boot:run > /tmp/service.log 2>&1 &"
    elif "pom.xml" in files:
        service_cmd = "cd /app/project && nohup mvn spring-boot:run > /tmp/service.log 2>&1 &"
    elif "package.json" in files:
        # Node.js
        service_cmd = "cd /app/project && npm install --silent 2>/dev/null; nohup npm start > /tmp/service.log 2>&1 &"
    elif "requirements.txt" in files or "app.py" in files or "main.py" in files:
        # Python (Flask/Django/FastAPI)
        if "app.py" in files:
            service_cmd = "cd /app/project && pip install -q -r requirements.txt 2>/dev/null; nohup python3 app.py > /tmp/service.log 2>&1 &"
        elif "main.py" in files:
            service_cmd = "cd /app/project && pip install -q -r requirements.txt 2>/dev/null; nohup python3 main.py > /tmp/service.log 2>&1 &"
        elif "manage.py" in files:
            service_cmd = "cd /app/project && pip install -q -r requirements.txt 2>/dev/null; nohup python3 manage.py runserver 0.0.0.0:8080 > /tmp/service.log 2>&1 &"
        else:
            # 정적 파일 서빙
            service_cmd = "cd /app/project && nohup python3 -m http.server 8080 > /tmp/service.log 2>&1 &"
    elif any(f.endswith(".html") for f in files):
        # 정적 HTML
        service_cmd = "cd /app/project && nohup python3 -m http.server 8080 > /tmp/service.log 2>&1 &"

    if not service_cmd:
        log.info(f"서비스 자동 시작 건너뜀: 프로젝트 유형 감지 불가 ({container_name})")
        return None

    log.info(f"서비스 자동 시작: {container_name} → {service_cmd[:80]}...")
    ok, output = _docker_run(
        "exec", "-d", container_name, "sh", "-c", service_cmd,
        timeout=15,
    )
    if ok:
        log.info(f"서비스 시작 명령 전송 완료: {container_name}")
        return "started"
    else:
        log.warning(f"서비스 시작 실패: {container_name} → {output[:200]}")
        return None


async def get_or_create_sandbox(
    user_id: str,
    project_path: str,
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """
    유저 Sandbox 컨테이너 조회/생성.
    프로젝트가 변경되면 기존 컨테이너/이미지를 완전 제거 후 신규 생성.
    실행 중이면 재사용, 중지면 재시작, 없으면 신규 빌드&기동.
    시작 후 프로젝트 유형에 맞는 웹 서비스도 자동 기동.
    """
    async def _notify(msg: str) -> None:
        if progress_callback:
            await progress_callback(msg)

    container_name = f"sandbox-{user_id}"
    port = allocate_port(user_id)
    touch_activity(user_id)

    # ── 프로젝트 변경 감지 → 기존 sandbox 완전 제거 ──
    meta = _load_sandbox_meta()
    existing_project = meta.get(user_id, {}).get("project_path")
    if existing_project and existing_project != project_path:
        log.info(f"프로젝트 변경 감지 ({existing_project} → {project_path}), 기존 sandbox 제거")
        await _notify(f"🔄 프로젝트 변경 감지 ({existing_project} → {project_path}) — 기존 Sandbox 제거 중...")
        remove_sandbox(user_id)
        port = allocate_port(user_id)  # 포트 재할당
        await _notify("✅ 기존 Sandbox 제거 완료")

    # Case 1: 이미 실행 중
    if _container_running(container_name):
        log.info(f"Sandbox 재사용: {container_name} (port={port})")
        await _notify(f"♻️ 기존 Sandbox 컨테이너 재사용 (port: {port})")
        await _notify("🌐 웹 서비스 상태 확인 중...")
        _auto_start_service(user_id, project_path)
        _update_sandbox_meta(user_id, port, project_path)
        await _notify("✅ 서비스 확인 완료")
        return {
            "container_name": container_name,
            "status": "running",
            "port": port,
            "url": f"http://localhost:{port}",
            "message": f"기존 Sandbox 컨테이너 사용 중 (port: {port})",
        }

    # Case 2: 존재하지만 중지 → 재시작
    if _container_exists(container_name):
        log.info(f"Sandbox 재시작: {container_name}")
        await _notify("🔁 중지된 Sandbox 컨테이너 재시작 중...")
        ok, output = _docker_run("start", container_name)
        if ok:
            await _notify("✅ 컨테이너 재시작 완료")
            await _notify("🌐 웹 서비스 자동 시작 중...")
            _auto_start_service(user_id, project_path)
            _update_sandbox_meta(user_id, port, project_path)
            await _notify("✅ 서비스 시작 명령 전송 완료")
            return {
                "container_name": container_name,
                "status": "running",
                "port": port,
                "url": f"http://localhost:{port}",
                "message": f"Sandbox 컨테이너 재시작 완료 (port: {port})",
            }
        await _notify("⚠️ 재시작 실패 — 컨테이너 재생성 진행")
        _docker_run("rm", "-f", container_name)

    # Case 3: 신규 생성 — 이전 이미지 잔여물 정리 후 생성
    await _notify("🗑️ 이전 이미지 정리 중...")
    old_img = f"sandbox-img-{user_id}"
    _docker_run("rmi", "-f", old_img, timeout=15)
    result = await _create_sandbox(user_id, project_path, container_name, port, progress_callback=progress_callback)
    if result.get("status") == "running":
        _update_sandbox_meta(user_id, port, project_path)
    return result


def _update_sandbox_meta(user_id: str, port: int, project_path: str) -> None:
    """Sandbox 메타 정보 업데이트"""
    meta = _load_sandbox_meta()
    meta[user_id] = {"port": port, "project_path": project_path}
    _save_sandbox_meta(meta)


async def _create_sandbox(
    user_id: str,
    project_path: str,
    container_name: str,
    port: int,
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """프로젝트 Dockerfile 기반으로 Sandbox 컨테이너를 빌드하고 기동"""
    async def _notify(msg: str) -> None:
        if progress_callback:
            await progress_callback(msg)

    project_rel = project_path.lstrip("/")
    host_project_dir = os.path.join(HOST_WORKSPACE, project_rel)
    container_project_dir = os.path.join(CONTAINER_WORKSPACE, project_rel)
    project_dir = Path(container_project_dir)

    # Dockerfile 탐색 (루트 + 1단계 하위)
    await _notify("🔍 Dockerfile 탐색 중...")
    dockerfile_path = None
    dockerfile_context = None
    if project_dir.is_dir():
        search_dirs = [project_dir] + [p for p in project_dir.iterdir() if p.is_dir()]
        for d in search_dirs:
            df = d / "Dockerfile"
            if df.is_file():
                dockerfile_path = str(df)
                dockerfile_context = str(d)
                break

    # gradle-wrapper.jar 누락 보충 (gradlew가 있는데 jar가 없는 경우)
    if project_dir.is_dir():
        _ensure_gradle_wrapper(project_dir)

    if not dockerfile_path:
        # Dockerfile 없음 → 범용 이미지
        log.info(f"Dockerfile 없음 → 범용 이미지로 Sandbox 생성: {container_name}")
        await _notify("📦 Dockerfile 없음 — 범용 이미지(python:3.11-slim)로 컨테이너 생성 중...")
        ok, output = await asyncio.to_thread(
            _docker_run,
            "run", "-d",
            "--name", container_name,
            "-p", f"{port}:8080",
            "-v", f"{host_project_dir}:/app/project",
            "-w", "/app/project",
            "--label", f"sandbox.user={user_id}",
            "--label", "sandbox.managed=true",
            "python:3.11-slim",
            "tail", "-f", "/dev/null",
            timeout=60,
        )
    else:
        # Dockerfile 빌드 — CLI가 context를 직접 읽으므로 컨테이너 내부 경로 사용
        # 호스트 아키텍처에 맞춰 네이티브 빌드 (ARM64/x86 자동 대응)
        image_name = f"sandbox-img-{user_id}"
        log.info(f"Sandbox 빌드: {image_name} from {dockerfile_path} (context: {dockerfile_context})")
        await _notify(f"📄 Dockerfile 발견: {dockerfile_path}")
        await _notify("🔨 Docker 이미지 빌드 중... (최대 5분 소요)")
        ok, output = await asyncio.to_thread(
            _docker_run,
            "build",
            "-t", image_name,
            "-f", dockerfile_path,
            dockerfile_context,
            timeout=300,
        )
        if not ok:
            # Dockerfile 빌드 실패 → dangling 이미지 정리 후 범용 이미지로 fallback
            build_error_output = output  # 빌드 에러 로그 보존
            log.warning(f"Dockerfile 빌드 실패 → 범용 이미지로 fallback: {output[:500]}")
            await _notify("⚠️ Dockerfile 빌드 실패 — 범용 이미지로 전환...")
            # ★ 빌드 실패 시 dangling 이미지 즉시 정리
            await _notify("🧹 Dangling 이미지 정리 중...")
            await asyncio.to_thread(_docker_run, "image", "prune", "-f", timeout=30)
            await _notify("🚀 범용 이미지로 컨테이너 기동 중...")
            ok, output = await asyncio.to_thread(
                _docker_run,
                "run", "-d",
                "--name", container_name,
                "-p", f"{port}:8080",
                "-v", f"{host_project_dir}:/app/project",
                "-w", "/app/project",
                "--label", f"sandbox.user={user_id}",
                "--label", "sandbox.managed=true",
                "python:3.11-slim",
                "tail", "-f", "/dev/null",
                timeout=60,
            )
            # fallback 성공 시 빌드 실패 정보 포함
            if ok:
                await _notify("🌐 웹 서비스 자동 시작 중...")
                _auto_start_service(user_id, project_path)  # 서비스 자동 기동
                await _notify("✅ 서비스 시작 명령 전송 완료")
                return {
                    "container_name": container_name,
                    "status": "running",
                    "port": port,
                    "url": f"http://localhost:{port}",
                    "message": "⚠️ Dockerfile 빌드 실패 — 범용 이미지로 실행 중",
                    "build_fallback": True,
                    "build_output": build_error_output[:2000],
                }
        else:
            # 빌드 성공 → 기동
            await _notify("✅ Docker 이미지 빌드 완료")
            await _notify("🚀 컨테이너 기동 중...")
            ok, output = await asyncio.to_thread(
                _docker_run,
                "run", "-d",
                "--name", container_name,
                "-p", f"{port}:8080",
                "-v", f"{host_project_dir}:/app/project",
                "--label", f"sandbox.user={user_id}",
                "--label", "sandbox.managed=true",
                image_name,
                timeout=60,
            )

    if ok:
        await _notify("🌐 웹 서비스 자동 시작 중...")
        _auto_start_service(user_id, project_path)  # 서비스 자동 기동
        await _notify("✅ 서비스 시작 명령 전송 완료")
        return {
            "container_name": container_name,
            "status": "running",
            "port": port,
            "url": f"http://localhost:{port}",
            "message": f"Sandbox 컨테이너 생성 완료 (port: {port})",
        }
    # ★ 기동 실패 시 빌드된 이미지 정리
    await _notify("❌ 컨테이너 기동 실패 — 이미지 정리 중...")
    image_name_cleanup = f"sandbox-img-{user_id}"
    await asyncio.to_thread(_docker_run, "rmi", "-f", image_name_cleanup, timeout=15)
    return {
        "container_name": container_name,
        "status": "error",
        "port": port,
        "url": "",
        "message": f"컨테이너 생성 실패: {output[:300]}",
    }


def stop_sandbox(user_id: str) -> dict[str, Any]:
    """Sandbox 컨테이너 중지 (제거 안 함 — 재시작 가능)"""
    container_name = f"sandbox-{user_id}"
    if _container_running(container_name):
        ok, output = _docker_run("stop", container_name, timeout=15)
        return {
            "container_name": container_name,
            "status": "stopped" if ok else "error",
            "message": "컨테이너 중지 완료" if ok else f"중지 실패: {output}",
        }
    return {
        "container_name": container_name,
        "status": "stopped",
        "message": "이미 중지 상태입니다",
    }


def get_sandbox_status(user_id: str) -> dict[str, Any]:
    """Sandbox 컨테이너 실제 상태 조회"""
    container_name = f"sandbox-{user_id}"
    port_map = _load_port_map()
    port = port_map.get(user_id)

    if _container_running(container_name):
        status = "running"
    elif _container_exists(container_name):
        status = "stopped"
    else:
        status = "not_created"

    return {
        "container_name": container_name,
        "status": status,
        "port": port,
        "url": f"http://localhost:{port}" if port and status == "running" else None,
        "idle_timeout_minutes": IDLE_TIMEOUT_SEC // 60,
    }


def remove_sandbox(user_id: str) -> dict[str, Any]:
    """Sandbox 컨테이너 + 이미지 완전 제거 + 포트/메타 매핑 삭제"""
    container_name = f"sandbox-{user_id}"
    image_name = f"sandbox-img-{user_id}"
    _docker_run("rm", "-f", container_name, timeout=15)
    _docker_run("rmi", "-f", image_name, timeout=15)
    # 포트맵 정리
    port_map = _load_port_map()
    port_map.pop(user_id, None)
    _save_port_map(port_map)
    # 메타 정리
    meta = _load_sandbox_meta()
    meta.pop(user_id, None)
    _save_sandbox_meta(meta)
    _activity.pop(user_id, None)
    # dangling 이미지 정리
    _docker_run("image", "prune", "-f", timeout=15)
    return {"container_name": container_name, "status": "removed", "message": "Sandbox 완전 제거 완료"}


# ── Idle 자동 중지 (백그라운드) ──────────────────────────

# ── Sandbox 도구 함수 (통합 테스트 Agent용) ────────────

_BLOCKED_COMMANDS = [
    "rm -rf /", "mkfs", "dd if=", ":(){ :", "shutdown", "reboot",
    "init 0", "halt", "poweroff", "kill -9 1", "chmod -R 777 /",
]


def sandbox_exec(user_id: str, command: str, timeout: int = 30) -> str:
    """Sandbox 컨테이너 내부에서 명령어 실행"""
    container_name = f"sandbox-{user_id}"

    if not _container_running(container_name):
        return f"❌ Sandbox 컨테이너 '{container_name}'이 실행 중이 아닙니다."

    if not command or not command.strip():
        return "❌ 실행할 명령어가 비어 있습니다."

    cmd_lower = command.lower()
    for pattern in _BLOCKED_COMMANDS:
        if pattern in cmd_lower:
            return f"❌ 보안상 차단된 명령어: {pattern}"

    touch_activity(user_id)

    ok, output = _docker_run(
        "exec", container_name, "sh", "-c", command,
        timeout=min(timeout, 60),
    )

    if len(output) > 5000:
        output = output[:5000] + "\n...(출력 생략, 5000자 초과)"

    if ok:
        return f"✅ 명령 실행 완료:\n{output}" if output else "✅ 명령 실행 완료 (출력 없음)"
    return f"❌ 명령 실행 실패:\n{output}"


def sandbox_logs(user_id: str, tail: int = 100) -> str:
    """Sandbox 컨테이너 로그 수집"""
    container_name = f"sandbox-{user_id}"

    if not _container_exists(container_name):
        return f"❌ Sandbox 컨테이너 '{container_name}'이 존재하지 않습니다."

    touch_activity(user_id)

    ok, output = _docker_run(
        "logs", "--tail", str(min(tail, 500)), container_name,
        timeout=15,
    )

    if len(output) > 5000:
        output = "...(이전 로그 생략)\n" + output[-5000:]

    if ok:
        return f"📋 컨테이너 로그 ({container_name}):\n{output}" if output else "📋 로그가 비어 있습니다."
    return f"❌ 로그 수집 실패:\n{output}"


def sandbox_health_check(user_id: str, path: str = "/", timeout: int = 10) -> str:
    """Sandbox 컨테이너 내부에서 HTTP 헬스체크 실행"""
    container_name = f"sandbox-{user_id}"

    if not _container_running(container_name):
        return f"❌ Sandbox 컨테이너 '{container_name}'이 실행 중이 아닙니다."

    touch_activity(user_id)

    # 컨테이너 내부에서 curl (localhost:8080 = 컨테이너 내부 포트)
    check_cmd = (
        f'if command -v curl > /dev/null 2>&1; then '
        f'curl -sf -o /dev/null -w "%{{http_code}}" --connect-timeout {min(timeout, 15)} '
        f'"http://localhost:8080{path}"; '
        f'elif command -v wget > /dev/null 2>&1; then '
        f'wget -qO /dev/null --timeout={min(timeout, 15)} '
        f'"http://localhost:8080{path}" && echo "200" || echo "000"; '
        f'else echo "NO_HTTP_CLIENT"; fi'
    )

    ok, output = _docker_run(
        "exec", container_name, "sh", "-c", check_cmd,
        timeout=min(timeout, 15) + 5,
    )

    output = output.strip()
    if output == "NO_HTTP_CLIENT":
        return "⚠️ 컨테이너에 curl/wget이 없습니다. sandbox_exec로 직접 확인하세요."

    if ok and output.startswith("2"):
        return f"✅ 헬스체크 성공 (HTTP {output}) — http://localhost:8080{path}"
    elif ok and output.startswith(("3", "4", "5")):
        return f"⚠️ 헬스체크 응답 HTTP {output} — http://localhost:8080{path}"
    else:
        return f"❌ 헬스체크 실패 — 앱이 아직 기동되지 않았거나 경로가 잘못되었습니다. (응답: {output})"


def sandbox_touch(user_id: str) -> str:
    """Sandbox 활동 갱신 (idle auto-stop 방지)"""
    container_name = f"sandbox-{user_id}"
    if not _container_running(container_name):
        return f"❌ Sandbox 컨테이너 '{container_name}'이 실행 중이 아닙니다."
    touch_activity(user_id)
    return "✅ Sandbox activity 갱신 완료 (idle 타이머 리셋)"


async def wait_for_sandbox_ready(user_id: str, max_wait: int = 60, interval: int = 3) -> dict[str, Any]:
    """Sandbox 컨테이너 앱 기동 대기 (헬스체크 반복)"""
    container_name = f"sandbox-{user_id}"
    elapsed = 0

    while elapsed < max_wait:
        touch_activity(user_id)

        if not _container_running(container_name):
            return {"ready": False, "message": "컨테이너가 중지되었습니다.", "elapsed": elapsed}

        result = sandbox_health_check(user_id, path="/", timeout=3)
        if "✅" in result or "⚠️" in result:
            return {"ready": True, "message": f"앱 기동 완료 ({elapsed}초)", "elapsed": elapsed}

        await asyncio.sleep(interval)
        elapsed += interval

    return {"ready": False, "message": f"앱 기동 대기 시간 초과 ({max_wait}초)", "elapsed": elapsed}


# ── 서버 시작 시 sandbox 초기 정리 ──────────────────────

async def startup_cleanup_sandboxes() -> None:
    """서버 시작 시 기존 중지된 sandbox 컨테이너/이미지 정리"""
    try:
        ok, output = await asyncio.to_thread(
            _docker_run,
            "ps", "-a",
            "--filter", "label=sandbox.managed=true",
            "--filter", "status=exited",
            "--format", "{{.Names}}",
            timeout=15,
        )
        if ok and output.strip():
            for cname in output.strip().split("\n"):
                cname = cname.strip()
                if not cname:
                    continue
                log.info(f"서버 시작 정리: 중지된 sandbox 삭제 → {cname}")
                await asyncio.to_thread(_docker_run, "rm", "-f", cname, timeout=15)
                orphan_uid = cname.replace("sandbox-", "", 1)
                orphan_img = f"sandbox-img-{orphan_uid}"
                await asyncio.to_thread(_docker_run, "rmi", "-f", orphan_img, timeout=15)
                # 포트맵 정리
                pm = _load_port_map()
                if orphan_uid in pm:
                    pm.pop(orphan_uid)
                    _save_port_map(pm)

        # dangling 이미지 정리
        await asyncio.to_thread(_docker_run, "image", "prune", "-f", timeout=30)
        log.info("서버 시작 sandbox 정리 완료")
    except Exception as e:
        log.error(f"서버 시작 sandbox 정리 오류: {e}", exc_info=True)


# ── Idle 자동 중지 + 완전 삭제 (백그라운드) ──────────────

REMOVE_TIMEOUT_SEC = 1800  # 30분 유휴 시 컨테이너+이미지 완전 삭제

# 컨테이너 중지 시각 기록 (완전 삭제 타이머용)
_stopped_at: dict[str, float] = {}


async def cleanup_idle_sandboxes() -> None:
    """60초마다 유휴 Sandbox 확인 → 자동 중지 / 장시간 중지 시 완전 삭제"""
    while True:
        await asyncio.sleep(60)
        try:
            now = _time.monotonic()
            port_map = _load_port_map()

            # ── 1단계: 실행 중 유휴 컨테이너 → 중지 ──
            for user_id in list(port_map.keys()):
                container_name = f"sandbox-{user_id}"
                if not _container_running(container_name):
                    # 중지 상태 컨테이너의 삭제 타이머 시작
                    if _container_exists(container_name) and user_id not in _stopped_at:
                        _stopped_at[user_id] = now
                    continue
                last = _activity.get(user_id, 0)
                idle = now - last if last > 0 else IDLE_TIMEOUT_SEC + 1
                if idle > IDLE_TIMEOUT_SEC:
                    log.info(f"Idle 자동 중지: {container_name} (유휴 {idle:.0f}초)")
                    await asyncio.to_thread(_docker_run, "stop", container_name, timeout=15)
                    _stopped_at[user_id] = now

            # ── 2단계: 장시간 중지 상태 → 완전 삭제 (컨테이너+이미지+포트맵) ──
            for user_id in list(_stopped_at.keys()):
                stopped_duration = now - _stopped_at[user_id]
                if stopped_duration > REMOVE_TIMEOUT_SEC:
                    container_name = f"sandbox-{user_id}"
                    image_name = f"sandbox-img-{user_id}"
                    log.info(f"장시간 중지 → 완전 삭제: {container_name} (중지 {stopped_duration:.0f}초)")
                    await asyncio.to_thread(_docker_run, "rm", "-f", container_name, timeout=15)
                    await asyncio.to_thread(_docker_run, "rmi", "-f", image_name, timeout=15)
                    # 포트맵 정리
                    pm = _load_port_map()
                    if user_id in pm:
                        pm.pop(user_id)
                        _save_port_map(pm)
                    _stopped_at.pop(user_id, None)
                    _activity.pop(user_id, None)

            # ── 3단계: orphaned 컨테이너 감지 (포트맵에 없지만 존재하는 sandbox) ──
            ok, output = await asyncio.to_thread(
                _docker_run,
                "ps", "-a",
                "--filter", "label=sandbox.managed=true",
                "--format", "{{.Names}}",
                timeout=15,
            )
            if ok and output.strip():
                known_containers = {f"sandbox-{uid}" for uid in port_map.keys()}
                for cname in output.strip().split("\n"):
                    cname = cname.strip()
                    if cname and cname not in known_containers:
                        log.warning(f"Orphaned 컨테이너 발견 → 삭제: {cname}")
                        await asyncio.to_thread(_docker_run, "rm", "-f", cname, timeout=15)
                        # orphaned 이미지도 정리
                        orphan_uid = cname.replace("sandbox-", "", 1)
                        orphan_img = f"sandbox-img-{orphan_uid}"
                        await asyncio.to_thread(_docker_run, "rmi", "-f", orphan_img, timeout=15)

            # ── 4단계: dangling 이미지 정리 (빌드 실패 잔여물) ──
            await asyncio.to_thread(
                _docker_run, "image", "prune", "-f", timeout=30,
            )

        except Exception as e:
            log.error(f"Idle 정리 오류: {e}", exc_info=True)
