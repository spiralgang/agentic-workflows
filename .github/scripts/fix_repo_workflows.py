#!/usr/bin/env python3
"""
fix_repo_workflows.py — Copilot-free GitHub Actions repair agent.

Invoked by .github/workflows/repair-workflows.yml. For a single target repo:
  1. Pull its workflow files + most-recent run logs per workflow.
  2. Send the diagnosis + raw YAML to an OpenAI-compatible LLM
     (Mistral by default; OpenRouter fallback) and ask for corrected
     .yml/.yaml content.
  3. Apply safe edits, push a branch, open a PR. For fixes that need a
     human-provided secret, open an issue instead of guessing.

No GitHub Copilot is used anywhere.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# LLM backend: pick Mistral if MISTRAL_API_KEY set, else OpenRouter.
MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

if MISTRAL_KEY:
    API = "https://api.mistral.ai/v1"
    KEY = MISTRAL_KEY
    MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
    BACKEND = "mistral"
elif OPENROUTER_KEY:
    API = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    KEY = OPENROUTER_KEY
    MODEL = os.environ.get(
        "OPENROUTER_MODEL", "cognitivecomputations/dolphin-mistral-24b-venice-edition:free"
    )
    BACKEND = "openrouter"
else:
    sys.stderr.write("Neither MISTRAL_API_KEY nor OPENROUTER_API_KEY set\n")
    sys.exit(2)

OWNER = os.environ["REPO_OWNER"]
TARGET = os.environ.get("TARGET_REPO", f"{OWNER}/{os.environ.get('REPO_NAME', '')}")
if "/" not in TARGET:
    TARGET = f"{OWNER}/{os.environ.get('REPO_NAME', TARGET)}"
GH_PAT = os.environ["GH_PAT"]
API_HEADERS = {
    "Authorization": f"Bearer {GH_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "spiralgang-repair-bot",
}


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
                time.sleep(2 * (attempt + 1))
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
    """Fetch latest run for a workflow file; return (text_log, latest_conclusion)."""
    wf = gh("GET", f"/repos/{TARGET}/actions/workflows/{path}/runs?per_page=1")
    runs = wf.get("workflow_runs", [])
    if not runs:
        return "(no runs yet)", None
    run_id = runs[0]["id"]
    conclusion = runs[0].get("conclusion")
    jobs = gh("GET", f"/repos/{TARGET}/actions/runs/{run_id}/jobs").get("jobs", [])
    out = [f"## run {run_id} ({conclusion})"]
    any_failed = False
    for job in jobs:
        jc = job.get("conclusion")
        out.append(f"### job: {job['name']} -> {jc}")
        if jc and jc != "success":
            any_failed = True
        for step in job.get("steps", []):
            out.append(f"  - {step['name']} [{step.get('conclusion')}]")
        if jc != "success":
            try:
                log = urllib.request.urlopen(job["logs_url"], timeout=60).read().decode("utf-8", "replace")
                out.append("    --- tail of job log ---")
                out.extend("    " + ln for ln in log.splitlines()[-40:])
            except Exception as e:
                out.append(f"    (log fetch failed: {e})")
    # if the run itself reports success and no job failed, it's green
    if conclusion == "success" and not any_failed:
        return "\n".join(out), "success"
    return "\n".join(out), conclusion


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
            last_err = f"{BACKEND} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}"
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(last_err) from e
        except Exception as e:
            last_err = f"{BACKEND} request failed: {e}"
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{BACKEND} failed after {attempts} attempts: {last_err}")


# ---- helpers ----


def fenced(text):
    """Extract the LAST/most-complete ```yaml fenced block. Fall back to first.
    If none, return ''. Never return prose or a partial non-YAML blob."""
    blocks = re.findall(r"```ya?ml\n(.*?)```", text, re.S)
    if not blocks:
        return ""
    # pick the longest block (LLMs sometimes wrap a summary then the full file)
    return max(blocks, key=len).strip()


def looks_like_full_workflow(orig, new):
    """Guard: reject edits that would truncate/delete the workflow.
    The new YAML must contain the structural anchors and not be a tiny
    fraction of the original size."""
    if not new or "on:" not in new or "jobs:" not in new:
        return False
    if len(new.strip()) < 0.6 * len(orig.strip()):
        return False
    return True


def main():
    print(f"[repair] target={TARGET} backend={BACKEND} model={MODEL}")
    contents = gh("GET", f"/repos/{TARGET}/contents/.github/workflows")
    wf_files = [
        c["name"] for c in contents
        if c["name"].endswith((".yml", ".yaml")) and not c["name"].endswith(".lock.yml")
    ]
    if not wf_files:
        print("[repair] no workflow files found")
        return

    fixes = {}
    issues = []
    for wf in wf_files:
        print(f"[repair] analyzing {wf}")
        raw = get_file(f".github/workflows/{wf}")
        if not raw:
            continue
        raw_text = raw[0]
        logs, conclusion = run_logs("main", wf)
        # skip workflows whose latest run fully passed (avoid churn)
        if conclusion == "success":
            print(f"[repair] {wf} appears green, skipping")
            continue
        system = (
            "You are a GitHub Actions YAML repair tool. INPUT: a workflow file and its "
            "latest failing run log. OUTPUT RULES: You MUST respond with EXACTLY one fenced "
            "code block tagged ```yaml containing the COMPLETE, valid, corrected workflow "
            "file (every line, not a diff, not a partial snippet). Preserve ALL original "
            "jobs, steps, and structure; apply only the minimal surgical fix for the failure. "
            "Do NOT add GitHub Copilot. Do NOT delete steps. Do NOT write any prose, explanation, "
            "or text outside the ```yaml block. If you cannot fix it without a secret you lack, "
            "return the ORIGINAL file verbatim inside the ```yaml block."
        )
        user = f"WORKFLOW FILE: .github/workflows/{wf}\n\n```yaml\n{raw_text}\n```\n\nLATEST RUN LOG:\n{logs}\n"
        try:
            resp = complete(system, user)
        except Exception as e:
            print(f"[repair] {wf}: LLM call failed ({e}); skipping")
            continue
        new_yaml = fenced(resp).strip()
        if not new_yaml or new_yaml == raw_text.strip():
            # no change (LLM returned original or refused) -> skip
            continue
        if not looks_like_full_workflow(raw_text, new_yaml):
            print(f"[repair] {wf}: LLM output failed sanity guard (truncated/incomplete); skipping")
            continue
        fixes[wf] = new_yaml
        print(f"[repair] {wf}: produced fix")

    if not fixes and not issues:
        print("[repair] nothing to do")
        return

    branch = f"ci-repair-{os.urandom(3).hex()}"
    base_sha = gh("GET", f"/repos/{TARGET}/git/ref/heads/main")["object"]["sha"]
    gh("POST", f"/repos/{TARGET}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
    for wf, content in fixes.items():
        path = f".github/workflows/{wf}"
        cur = get_file(path)
        sha = cur[1] if cur else None
        gh("PUT", f"/repos/{TARGET}/contents/{path}",
           {"message": f"ci: repair {wf} (Copilot-free, {BACKEND})",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch, **({"sha": sha} if sha else {})})
    pr_body = f"Automated CI repair via {BACKEND} LLM ({MODEL}). Copilot-free.\n\n"
    if fixes:
        pr_body += "### Fixes\n" + "\n".join(f"- {w}" for w in fixes)
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
