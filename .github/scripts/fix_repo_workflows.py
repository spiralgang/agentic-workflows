#!/usr/bin/env python3
"""
fix_repo_workflows.py — Copilot-free GitHub Actions repair agent.

Invoked by .github/workflows/repair-workflows.yml. For a single target repo:
  1. Pull its workflow files + most-recent run logs per workflow.
  2. Send the diagnosis + raw YAML to an OpenAI-compatible LLM (OpenRouter)
     and ask for corrected .yml/.yaml content.
  3. Apply safe edits, push a branch, open a PR. For fixes that need a
     human-provided secret, open an issue instead of guessing.

No GitHub Copilot is used anywhere. The model is chosen via OPENROUTER_MODEL.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = os.environ.get("OPENROUTER_MODEL", "cognitivecomputations/dolphin-mistral-24b-venice-edition:free")
OWNER = os.environ["REPO_OWNER"]
TARGET = os.environ.get("TARGET_REPO", f"{OWNER}/{os.environ.get('REPO_NAME', '')}")
# accept either explicit TARGET_REPO (owner/repo) or REPO_OWNER + REPO_NAME
if "/" not in TARGET:
    TARGET = f"{OWNER}/{os.environ.get('REPO_NAME', TARGET)}"
GH_PAT = os.environ["GH_PAT"]
API_HEADERS = {
    "Authorization": f"Bearer {GH_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "spiralgang-repair-bot",
}
OUT_DIR = os.environ.get("GITHUB_WORKSPACE", ".")

# ---- GitHub REST helpers (token = GH_PAT, full scopes) ----


def gh(method, path, data=None, accept=None):
    url = f"https://api.github.com{path}"
    headers = dict(API_HEADERS)
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, method=method, headers=headers)
    if data is not None:
        req.data = json.dumps(data).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:
                continue
            sys.stderr.write(f"HTTP {e.code} {method} {path}: {e.read().decode()[:500]}\n")
            raise


def get_file(path):
    try:
        r = gh("GET", f"/repos/{TARGET}/contents/{path}")
    except urllib.error.HTTPError:
        return None
    if not isinstance(r, dict) or "content" not in r:
        return None
    return base64.b64decode(r["content"]).decode("utf-8", "replace"), r["sha"]


def run_logs(branch, path):
    """Fetch the latest run for one workflow file and return parsed steps."""
    wf = gh("GET", f"/repos/{TARGET}/actions/workflows/{path}/runs?per_page=1")
    runs = wf.get("workflow_runs", [])
    if not runs:
        return "(no runs yet)"
    run_id = runs[0]["id"]
    jobs = gh("GET", f"/repos/{TARGET}/actions/runs/{run_id}/jobs").get("jobs", [])
    out = [f"## run {run_id} ({runs[0]['conclusion']})"]
    for job in jobs:
        out.append(f"### job: {job['name']} -> {job['conclusion']}")
        for step in job.get("steps", []):
            out.append(f"  - {step['name']} [{step['conclusion']}]")
        if job.get("conclusion") != "success":
            try:
                log = urllib.request.urlopen(job["logs_url"], timeout=60).read().decode("utf-8", "replace")
                out.append("    --- tail of job log ---")
                out.extend("    " + ln for ln in log.splitlines()[-40:])
            except Exception as e:
                out.append(f"    (log fetch failed: {e})")
    return "\n".join(out)


# ---- LLM call (OpenAI-compatible chat completions) ----


def complete(system, user, attempts=5):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        f"{API}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    last_err = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last_err = f"OpenRouter HTTP {e.code}: {e.read().decode('utf-8','replace')[:400]}"
            # 429 (free-tier throttle) and 5xx are transient — back off and retry
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(last_err) from e
        except Exception as e:  # network/timeout
            last_err = f"OpenRouter request failed: {e}"
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"OpenRouter failed after {attempts} attempts: {last_err}")


# ---- Workflow-file editing primitives ----

WF_RE = re.compile(r"^(\s+)?(-?\s*name:.*|on:|jobs:|permissions:)", re.M)


def extract_blocks(yaml_text):
    """Return list of (filename_guess, content) for each top-level workflow? No —
    here each path is its own file. We edit whole files."""
    return yaml_text


def fenced(text):
    m = re.search(r"```ya?ml\n(.*?)```", text, re.S)
    if m:
        return m.group(1)
    m = re.search(r"```\n(.*?)```", text, re.S)
    return m.group(1) if m else text


def main():
    print(f"[repair] target={TARGET} model={MODEL}")
    # list workflow files
    contents = gh("GET", f"/repos/{TARGET}/contents/.github/workflows")
    wf_files = [
        c["name"] for c in contents
        if c["name"].endswith((".yml", ".yaml")) and not c["name"].endswith(".lock.yml")
    ]
    if not wf_files:
        print("[repair] no workflow files found"); return

    fixes = {}
    issues = []
    for wf in wf_files:
        print(f"[repair] analyzing {wf}")
        raw = get_file(f".github/workflows/{wf}")
        if not raw:
            continue
        logs = run_logs("main", wf)
        # skip if last run passed
        if "success" in logs and "failure" not in logs and "-> failure" not in logs:
            # crude: still inspect; but avoid churn on green workflows
            if "-> failure" not in logs:
                print(f"[repair] {wf} appears green, skipping")
                continue
        system = (
            "You repair GitHub Actions workflow YAML. You receive a workflow file "
            "and its latest failing run log. Return ONLY the corrected full YAML inside "
            "a ```yaml fenced block. Make minimal surgical edits. Never add or enable "
            "GitHub Copilot. Do NOT invent secret values. If a fix requires a secret you "
            "do not have, return the original file unchanged and instead output a separate "
            "section starting with 'NEED_SECRET:' followed by which secret and where."
        )
        user = f"WORKFLOW FILE: .github/workflows/{wf}\n\n```yaml\n{raw}\n```\n\nLATEST RUN LOG:\n{logs}\n"
        try:
            resp = complete(system, user)
        except Exception as e:
            print(f"[repair] {wf}: LLM call failed ({e}); skipping")
            continue
        if "NEED_SECRET:" in resp:
            issues.append(f"{wf}: {resp.split('NEED_SECRET:',1)[1].strip()[:300]}")
            continue
        new_yaml = fenced(resp).strip()
        if new_yaml and new_yaml != raw.strip():
            fixes[wf] = new_yaml
            print(f"[repair] {wf}: produced fix")

    if not fixes and not issues:
        print("[repair] nothing to do")
        return

    branch = f"ci-repair-{os.urandom(3).hex()}"
    gh("POST", f"/repos/{TARGET}/git/refs",
       {"ref": f"refs/heads/{branch}", "sha": gh("GET", f"/repos/{TARGET}/git/ref/heads/main")["object"]["sha"]})
    for wf, content in fixes.items():
        path = f".github/workflows/{wf}"
        cur = get_file(path)
        sha = cur[1] if cur else None
        gh("PUT", f"/repos/{TARGET}/contents/{path}",
           {"message": f"ci: repair {wf} (Copilot-free, OpenRouter)", "content": base64.b64encode(content.encode()).decode(),
            "branch": branch, **({"sha": sha} if sha else {})})
    pr_body = "Automated CI repair via OpenRouter LLM. Copilot-free.\n\n" + (
        "\n### Fixes\n" + "\n".join(f"- {w}" for w in fixes) if fixes else "")
    if issues:
        pr_body += "\n### Needs human secrets\n" + "\n".join(f"- {i}" for i in issues)
    pr = gh("POST", f"/repos/{TARGET}/pulls",
            {"title": "ci: repair failing workflows (Copilot-free)", "head": branch, "base": "main", "body": pr_body})
    print(f"[repair] PR opened: {pr.get('html_url')}")
    for iss in issues:
        gh("POST", f"/repos/{TARGET}/issues",
           {"title": f"ci: secret required to fix {iss.split(':')[0]}", "body": iss})


if __name__ == "__main__":
    main()
