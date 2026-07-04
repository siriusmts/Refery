from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPO = "siriusmts/Agents"
USER_AGENT = "eva-release-runner/1.0"
URL_RE = re.compile(
    r"(https?://[^\s\"'<>)\]]+|(?:t|telegram)\.me/[^\s\"'<>)\]]+)",
    re.IGNORECASE,
)
TOKEN_KEYS = ("GITHUB_TOKEN", "GH_TOKEN")
REPO_KEYS = ("GITHUB_REPO", "EVA_REPO", "GITHUB_REPOSITORY")
ENV_FILE_NAMES = (".env", "github.env")
TOKEN_FILE_NAMES = (".github_token", "github_token.txt")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_child(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    root = parent.resolve()
    if resolved == root or root not in resolved.parents:
        raise RuntimeError(f"Refusing to modify path outside {root}: {resolved}")


def safe_rmtree(path: Path, parent: Path) -> None:
    ensure_child(path, parent)
    if path.exists():
        shutil.rmtree(path)


def safe_unlink(path: Path, parent: Path) -> None:
    ensure_child(path, parent)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in read_text_auto(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        values[key] = strip_env_value(value)
    return values


def read_plain_token(path: Path) -> str | None:
    if not path.exists():
        return None
    for raw_line in read_text_auto(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in TOKEN_KEYS:
                return strip_env_value(value)
            continue
        return line
    return None


def find_github_config(project_root: Path, eva_dir: Path) -> dict[str, str | None]:
    token: str | None = None
    token_source: str | None = None
    repo: str | None = None
    repo_source: str | None = None

    for key in TOKEN_KEYS:
        if os.environ.get(key):
            token = os.environ[key]
            token_source = f"env:{key}"
            break
    for key in REPO_KEYS:
        if os.environ.get(key):
            repo = os.environ[key]
            repo_source = f"env:{key}"
            break

    for base in (project_root, eva_dir):
        for name in ENV_FILE_NAMES:
            path = base / name
            values = read_env_file(path)
            if token is None:
                for key in TOKEN_KEYS:
                    if values.get(key):
                        token = values[key]
                        token_source = f"file:{path.name}:{key}"
                        break
            if repo is None:
                for key in REPO_KEYS:
                    if values.get(key):
                        repo = values[key]
                        repo_source = f"file:{path.name}:{key}"
                        break

    if token is None:
        for base in (project_root, eva_dir):
            for name in TOKEN_FILE_NAMES:
                path = base / name
                found = read_plain_token(path)
                if found:
                    token = found
                    token_source = f"file:{path.name}"
                    break
            if token is not None:
                break

    if token is None:
        gh_path = shutil.which("gh")
        if gh_path:
            try:
                result = subprocess.run(
                    ["gh", "auth", "token"],
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                found = result.stdout.strip()
                if result.returncode == 0 and found:
                    token = found
                    token_source = "github-cli:gh auth token"
            except (OSError, subprocess.SubprocessError):
                pass

    return {
        "token": token,
        "token_source": token_source or "none",
        "repo": repo,
        "repo_source": repo_source or "default",
    }


def split_prompt_blocks(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block.strip() for block in re.split(r"\n\s*\n", normalized) if block.strip()]
    prompts: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) == 1 and lines[0].endswith(":") and len(lines[0]) <= 80:
            continue
        prompts.append(block)
    return prompts


def select_prompt(prompt_file: Path, index: int, mode: str) -> tuple[str, dict[str, Any]]:
    text = read_text_auto(prompt_file)
    blocks = split_prompt_blocks(text)
    if mode == "all":
        prompt = text.strip()
        selected_index: int | None = None
    else:
        if not blocks:
            raise RuntimeError(f"No prompt blocks found in {prompt_file}")
        if index < 0 or index >= len(blocks):
            raise RuntimeError(
                f"Prompt index {index} is out of range. Found {len(blocks)} prompt blocks."
            )
        prompt = blocks[index]
        selected_index = index
    return prompt, {
        "prompt_file": str(prompt_file),
        "prompt_mode": mode,
        "prompt_index": selected_index,
        "prompt_blocks_found": len(blocks),
        "prompt_chars": len(prompt),
        "prompt_preview": prompt[:300],
    }


def github_headers(token: str | None = None, accept: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": accept or "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(url: str, token: str | None) -> Any:
    req = urllib.request.Request(url, headers=github_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        hint = ""
        if exc.code == 404:
            if token:
                hint = (
                    " Token was found, but GitHub still returned 404. Check repository name, "
                    "selected repository access, organization approval, and Contents: read."
                )
            else:
                hint = " If the repository is private, add GITHUB_TOKEN to .env/github_token.txt."
        if exc.code == 403:
            if token:
                hint = " Token was found, but GitHub returned 403. Check token permissions or org policy."
            else:
                hint = " GitHub may be rate limiting you; add GITHUB_TOKEN to .env/github_token.txt."
        raise RuntimeError(f"GitHub API error {exc.code} for {url}.{hint} {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach GitHub API at {url}: {exc.reason}. "
            "Check network/DNS/proxy settings, and add GITHUB_TOKEN to .env/github_token.txt "
            "if the repository is private."
        ) from exc


def get_latest_release(repo: str, token: str | None) -> dict[str, Any]:
    api_base = f"https://api.github.com/repos/{repo}"
    try:
        release = request_json(f"{api_base}/releases/latest", token)
        if isinstance(release, dict):
            return release
    except RuntimeError as first_error:
        try:
            releases = request_json(f"{api_base}/releases", token)
        except RuntimeError:
            if token is None:
                raise RuntimeError(
                    f"GitHub did not return releases for {repo}, and no token was found. "
                    "Create C:\\Users\\User\\Desktop\\MCP\\.env with GITHUB_TOKEN=your_token, "
                    "or create C:\\Users\\User\\Desktop\\MCP\\github_token.txt containing the token."
                ) from first_error
            raise first_error
        if isinstance(releases, list) and releases:
            return releases[0]
        raise first_error
    raise RuntimeError(f"Unexpected GitHub response for {repo}")


def download(url: str, path: Path, token: str | None, accept: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=github_headers(token, accept))
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            with path.open("wb") as file:
                shutil.copyfileobj(response, file)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not download {url}: {exc.reason}. "
            "Check network/DNS/proxy settings, and add GITHUB_TOKEN to .env/github_token.txt "
            "if the repository is private."
        ) from exc


def safe_extract_zip(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (dest / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Unsafe archive member path: {member.filename}")
        archive.extractall(dest)


def copy_extracted_tree(extract_dir: Path, source_dir: Path) -> None:
    entries = [entry for entry in extract_dir.iterdir() if entry.name not in {".", ".."}]
    if len(entries) == 1 and entries[0].is_dir():
        root = entries[0]
    else:
        root = extract_dir
    source_dir.mkdir(parents=True, exist_ok=True)
    for item in root.iterdir():
        target = source_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def find_skill_file(source_dir: Path) -> Path | None:
    for candidate in source_dir.rglob("*"):
        if candidate.is_file() and candidate.name.lower() == "skill.md":
            return candidate
    return None


def has_runnable_project(source_dir: Path) -> bool:
    if not source_dir.exists() or not source_dir.is_dir():
        return False
    candidates = [
        "main.py",
        "app.py",
        "bot.py",
        "eva.py",
        "run.py",
        "agent.py",
        "server.py",
        "package.json",
    ]
    return any((source_dir / name).exists() for name in candidates) or len(list(source_dir.glob("*.py"))) == 1


def prepare_local_project(eva_dir: Path) -> dict[str, Any]:
    eva_dir.mkdir(parents=True, exist_ok=True)
    source_candidates = [eva_dir / "source", eva_dir]
    for source_dir in source_candidates:
        if has_runnable_project(source_dir):
            skill_path = eva_dir / "SKILL.md"
            return {
                "repo": None,
                "skipped": True,
                "mode": "local",
                "source_dir": str(source_dir),
                "skill_path": str(skill_path) if skill_path.exists() else None,
            }
    raise RuntimeError(
        f"No runnable EVA code found. Put main.py/app.py/package.json in {eva_dir} "
        f"or in {eva_dir / 'source'}."
    )


def download_release(repo: str, eva_dir: Path, token: str | None, skip_download: bool) -> dict[str, Any]:
    eva_dir.mkdir(parents=True, exist_ok=True)
    source_dir = eva_dir / "source"
    downloads_dir = eva_dir / "downloads"
    skill_path = eva_dir / "SKILL.md"

    if skip_download:
        if not source_dir.exists():
            raise RuntimeError(f"--skip-download was set, but {source_dir} does not exist")
        return {
            "repo": repo,
            "skipped": True,
            "source_dir": str(source_dir),
            "skill_path": str(skill_path) if skill_path.exists() else None,
        }

    release = get_latest_release(repo, token)
    tag = release.get("tag_name") or release.get("name") or "latest"
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tag)).strip("_") or "latest"
    archive_url = release.get("zipball_url")
    if not archive_url:
        raise RuntimeError("Latest release does not expose zipball_url")

    replaced_existing_source = source_dir.exists()
    replaced_existing_downloads = downloads_dir.exists()
    replaced_existing_skill = skill_path.exists()
    safe_rmtree(source_dir, eva_dir)
    safe_rmtree(downloads_dir, eva_dir)
    safe_unlink(skill_path, eva_dir)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    archive_path = downloads_dir / f"{safe_tag}.zip"
    download(archive_url, archive_path, token, "application/octet-stream")

    with tempfile.TemporaryDirectory(prefix="eva-release-") as tmp:
        extract_dir = Path(tmp)
        safe_extract_zip(archive_path, extract_dir)
        copy_extracted_tree(extract_dir, source_dir)

    skill_asset_path: str | None = None
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower() == "skill.md" and asset.get("url"):
            download(asset["url"], skill_path, token, "application/octet-stream")
            skill_asset_path = str(skill_path)
            break

    if skill_asset_path is None:
        found_skill = find_skill_file(source_dir)
        if found_skill:
            shutil.copy2(found_skill, skill_path)
            skill_asset_path = str(skill_path)

    return {
        "repo": repo,
        "skipped": False,
        "tag_name": release.get("tag_name"),
        "name": release.get("name"),
        "html_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "archive_path": str(archive_path),
        "source_dir": str(source_dir),
        "skill_path": skill_asset_path,
        "replaced_existing_source": replaced_existing_source,
        "replaced_existing_downloads": replaced_existing_downloads,
        "replaced_existing_skill": replaced_existing_skill,
    }


def list_prompts(prompt_file: Path) -> None:
    text = read_text_auto(prompt_file)
    blocks = split_prompt_blocks(text)
    for idx, block in enumerate(blocks):
        one_line = " ".join(block.split())
        print(f"{idx}: {one_line[:180]}")


def detect_command(source_dir: Path, run_command: str | None) -> tuple[str | list[str], bool, str]:
    explicit = run_command or os.environ.get("EVA_RUN_COMMAND")
    if explicit:
        return explicit, True, "explicit"

    candidates = [
        "main.py",
        "app.py",
        "bot.py",
        "eva.py",
        "run.py",
        "agent.py",
        "server.py",
    ]
    for name in candidates:
        path = source_dir / name
        if path.exists():
            return [sys.executable, str(path)], False, f"python:{name}"

    py_files = sorted(source_dir.glob("*.py"))
    if len(py_files) == 1:
        return [sys.executable, str(py_files[0])], False, f"python:{py_files[0].name}"

    package_json = source_dir / "package.json"
    if package_json.exists():
        package = json.loads(read_text_auto(package_json))
        scripts = package.get("scripts") or {}
        for script_name in ("start", "dev", "serve"):
            if script_name in scripts:
                return ["npm", "run", script_name], False, f"npm:{script_name}"

    raise RuntimeError(
        "Could not detect how to run EVA code. Set EVA_RUN_COMMAND or pass --run-command."
    )


def install_dependencies(source_dir: Path) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if (source_dir / "requirements.txt").exists():
        command = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
        result = subprocess.run(command, cwd=source_dir, text=True, capture_output=True)
        actions.append(
            {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        )
        if result.returncode != 0:
            raise RuntimeError("pip install failed. See report for details.")
    if (source_dir / "package.json").exists() and not (source_dir / "node_modules").exists():
        command = ["npm", "ci"] if (source_dir / "package-lock.json").exists() else ["npm", "install"]
        result = subprocess.run(command, cwd=source_dir, text=True, capture_output=True)
        actions.append(
            {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        )
        if result.returncode != 0:
            raise RuntimeError("npm install failed. See report for details.")
    return actions


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def parse_tokens(log_text: str, prompt: str) -> dict[str, Any]:
    prompt_estimate = estimate_tokens(prompt)
    output_estimate = estimate_tokens(log_text)
    usage: dict[str, Any] = {
        "source": "estimate",
        "prompt_tokens_estimate": prompt_estimate,
        "output_tokens_estimate": output_estimate,
        "total_tokens_estimate": prompt_estimate + output_estimate,
        "total_tokens": None,
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    patterns = {
        "total_tokens": [
            r'"total_tokens"\s*:\s*(\d+)',
            r"\btotal[_\s-]?tokens\b\D{0,30}(\d+)",
            r"\btokens[_\s-]?used\b\D{0,30}(\d+)",
        ],
        "prompt_tokens": [
            r'"prompt_tokens"\s*:\s*(\d+)',
            r"\b(?:prompt|input)[_\s-]?tokens\b\D{0,30}(\d+)",
        ],
        "completion_tokens": [
            r'"completion_tokens"\s*:\s*(\d+)',
            r"\b(?:completion|output)[_\s-]?tokens\b\D{0,30}(\d+)",
        ],
    }
    for key, key_patterns in patterns.items():
        values: list[int] = []
        seen_spans: set[tuple[int, int]] = set()
        for pattern in key_patterns:
            for match in re.finditer(pattern, log_text, re.IGNORECASE):
                span = match.span(1)
                if span in seen_spans:
                    continue
                seen_spans.add(span)
                values.append(int(match.group(1)))
        if values:
            usage[key] = sum(values)
            usage["source"] = "eva_output"
            usage[f"{key}_occurrences"] = len(values)
    if usage["total_tokens"] is None and (
        usage["prompt_tokens"] is not None or usage["completion_tokens"] is not None
    ):
        usage["total_tokens"] = (usage["prompt_tokens"] or 0) + (usage["completion_tokens"] or 0)
    return usage


def write_stdin(proc: subprocess.Popen[str], prompt: str) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(prompt)
        if not prompt.endswith("\n"):
            proc.stdin.write("\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def run_eva(
    command: str | list[str],
    shell: bool,
    source_dir: Path,
    env: dict[str, str],
    prompt: str,
    timeout_seconds: int,
    stop_after_link: bool,
    link_grace_seconds: float,
) -> dict[str, Any]:
    started_at = utc_now()
    started_monotonic = time.monotonic()
    output_events: list[dict[str, Any]] = []
    lock = threading.Lock()
    first_link: dict[str, Any] | None = None

    proc = subprocess.Popen(
        command,
        cwd=source_dir,
        env=env,
        shell=shell,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def reader(stream_name: str, stream: Any) -> None:
        nonlocal first_link
        while True:
            line = stream.readline()
            if line == "":
                break
            elapsed = time.monotonic() - started_monotonic
            clean = line.rstrip("\n")
            with lock:
                output_events.append(
                    {
                        "stream": stream_name,
                        "elapsed_seconds": round(elapsed, 3),
                        "line": clean,
                    }
                )
                match = URL_RE.search(line)
                if match and first_link is None:
                    url = match.group(1).rstrip(".,;")
                    if url.lower().startswith(("t.me/", "telegram.me/")):
                        url = f"https://{url}"
                    first_link = {
                        "url": url,
                        "elapsed_seconds": elapsed,
                        "detected_at": utc_now(),
                        "stream": stream_name,
                    }
            print(line, end="")

    threads = [
        threading.Thread(target=reader, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    write_stdin(proc, prompt)

    timed_out = False
    terminated_after_link = False
    deadline = started_monotonic + timeout_seconds
    while proc.poll() is None:
        now = time.monotonic()
        with lock:
            link = first_link
        if link and stop_after_link and now >= started_monotonic + link["elapsed_seconds"] + link_grace_seconds:
            terminate_process(proc)
            terminated_after_link = True
            break
        if now >= deadline:
            terminate_process(proc)
            timed_out = True
            break
        time.sleep(0.2)

    for thread in threads:
        thread.join(timeout=5)

    finished_monotonic = time.monotonic()
    with lock:
        events_snapshot = list(output_events)
        link_snapshot = dict(first_link) if first_link else None

    combined_log = "\n".join(event["line"] for event in events_snapshot)
    return {
        "command": command,
        "shell": shell,
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_total_seconds": round(finished_monotonic - started_monotonic, 3),
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "terminated_after_link": terminated_after_link,
        "bot_link": link_snapshot["url"] if link_snapshot else None,
        "elapsed_to_link_seconds": round(link_snapshot["elapsed_seconds"], 3)
        if link_snapshot
        else None,
        "link_detected_at": link_snapshot["detected_at"] if link_snapshot else None,
        "output_events": events_snapshot,
        "token_usage": parse_tokens(combined_log, prompt),
    }


def save_report(eva_dir: Path, report: dict[str, Any]) -> Path:
    logs_dir = eva_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = logs_dir / f"run-report-{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (eva_dir / "last-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path


def print_summary(report: dict[str, Any], report_path: Path) -> None:
    run = report.get("run") or {}
    release = report.get("release") or {}
    usage = run.get("token_usage") or {}
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = f"~{usage.get('total_tokens_estimate')} (estimate)"
    print("\n=== EVA RESULT ===")
    if report.get("mode") == "local":
        print(f"Source: local ({release.get('source_dir')})")
    else:
        print(f"Release: {release.get('tag_name') or release.get('name') or 'unknown'}")
    if run.get("skipped"):
        print("Run: skipped")
        print(f"Report: {report_path}")
        return
    print(f"Bot link: {run.get('bot_link') or 'not detected'}")
    print(f"Time to link: {run.get('elapsed_to_link_seconds')}")
    print(f"Total runtime: {run.get('elapsed_total_seconds')}")
    print(f"Token source: {usage.get('source')}")
    print(f"Total tokens: {total_tokens}")
    print(f"Prompt tokens estimate: {usage.get('prompt_tokens_estimate')}")
    print(f"Report: {report_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run local EVA code with a prompt, wait for a bot link, and measure output."
    )
    parser.add_argument("--repo", help="GitHub owner/repo")
    parser.add_argument("--eva-dir", default="EVA", help="Directory where EVA code is stored")
    parser.add_argument("--prompt-file", default="prompts.txt", help="Prompt file path")
    parser.add_argument("--prompt-index", type=int, default=0, help="Prompt block index")
    parser.add_argument("--prompt-mode", choices=("one", "all"), default="one")
    parser.add_argument("--list-prompts", action="store_true", help="List prompt blocks and exit")
    parser.add_argument("--download", action="store_true", help="Download latest release from GitHub first")
    parser.add_argument("--skip-download", action="store_true", help="Deprecated; local mode is default")
    parser.add_argument("--no-run", action="store_true", help="Prepare prompt/project but do not run EVA")
    parser.add_argument("--run-command", help="Command used to start EVA code")
    parser.add_argument(
        "--prompt-delivery",
        choices=("auto", "argv", "stdin"),
        default="auto",
        help="How to pass the selected prompt into EVA",
    )
    parser.add_argument("--timeout", type=int, default=900, help="Run timeout in seconds")
    parser.add_argument("--keep-running", action="store_true", help="Do not stop after detecting a link")
    parser.add_argument("--link-grace-seconds", type=float, default=5.0)
    parser.add_argument("--install-deps", action="store_true", help="Install Python/Node dependencies first")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    eva_dir = (project_root / args.eva_dir).resolve()
    prompt_file = (project_root / args.prompt_file).resolve()

    if args.list_prompts:
        list_prompts(prompt_file)
        return 0

    report: dict[str, Any] = {
        "created_at": utc_now(),
        "project_root": str(project_root),
        "mode": "download" if args.download else "local",
    }

    try:
        prompt, prompt_meta = select_prompt(prompt_file, args.prompt_index, args.prompt_mode)
        if args.download:
            github_config = find_github_config(project_root, eva_dir)
            token = github_config["token"]
            repo = args.repo or github_config["repo"] or DEFAULT_REPO
            report["github"] = {
                "repo": repo,
                "repo_source": "cli" if args.repo else github_config["repo_source"],
                "token_source": github_config["token_source"],
                "token_present": bool(token),
            }
            release_meta = download_release(repo, eva_dir, token, False)
        else:
            release_meta = prepare_local_project(eva_dir)
        report["prompt"] = prompt_meta
        report["release"] = release_meta

        if args.no_run:
            report["run"] = {"skipped": True}
        else:
            source_dir = Path(release_meta["source_dir"])
            install_report: list[dict[str, Any]] = []
            if args.install_deps:
                install_report = install_dependencies(source_dir)
            command, shell, detector = detect_command(source_dir, args.run_command)
            prompt_as_argv = args.prompt_delivery == "argv" or (
                args.prompt_delivery == "auto"
                and not args.run_command
                and isinstance(command, list)
                and len(command) >= 2
                and Path(command[1]).name == "main.py"
            )
            if prompt_as_argv:
                if shell:
                    command = f"{command} {json.dumps(prompt, ensure_ascii=False)}"
                elif isinstance(command, list):
                    command = [*command, prompt]
            env = os.environ.copy()
            env.update(
                {
                    "EVA_PROMPT": prompt,
                    "EVA_PROMPT_FILE": str(prompt_file),
                    "EVA_SKILL_FILE": str(eva_dir / "SKILL.md"),
                    "PYTHONUTF8": "1",
                }
            )
            run_report = run_eva(
                command=command,
                shell=shell,
                source_dir=source_dir,
                env=env,
                prompt=prompt,
                timeout_seconds=args.timeout,
                stop_after_link=not args.keep_running,
                link_grace_seconds=args.link_grace_seconds,
            )
            run_report["command_detector"] = detector
            run_report["install"] = install_report
            report["run"] = run_report
    except Exception as exc:
        report["error"] = str(exc)
        report_path = save_report(eva_dir, report)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Report: {report_path}", file=sys.stderr)
        return 1

    report_path = save_report(eva_dir, report)
    print_summary(report, report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
