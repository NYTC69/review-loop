#!/usr/bin/env python3
"""Resolve and pin review inputs; no clone or checkout yet (LG2-d1).

Each head/base records its OID, source and fetch ref for later materialization.
Pins have exactly oid, source and fetch_ref (null for unnamed commits).
Resolution is read-only; errors refuse rather than selecting a local diff.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


class ResolutionError(ValueError):
    pass


PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)/?\Z")
REPO = re.compile(r"[\w.-]+/[\w.-]+\Z")
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# Mirror paired_session.candidate_tree.GIT_NO_EXEC for direct script execution.
GIT_NO_EXEC = ('-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.pager=cat', '-c', 'diff.external=', '-c', 'core.sshCommand=false')


def command(argv: list[str], cwd: Path, *, network: bool = False) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for key in ("SSH_ASKPASS", "GH_REPO", "GH_HOST"):
        env.pop(key, None)
    if argv[0] == "git":
        env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                   GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_SSH_COMMAND="ssh -o BatchMode=yes", GIT_OPTIONAL_LOCKS="0")
        argv = ["git", *GIT_NO_EXEC, "-c", "core.askPass=", *argv[1:]]
        if network:
            offset = 1 + len(GIT_NO_EXEC)
            argv[offset:offset] = ["-c", "credential.helper=", "-c",
                                   "credential.helper=!gh auth git-credential"]
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                                capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolutionError(f"{argv[0]} unavailable or timed out") from exc
    if result.returncode:
        # Only this compatibility failure permits a gh API fallback.
        if argv[:3] == ["gh", "pr", "view"] and re.search(
                r'Unknown JSON field: ["\']baseRefOid["\']', result.stderr):
            raise ResolutionError("unsupported baseRefOid")
        raise ResolutionError(f"{argv[0]} resolution failed")
    return result.stdout.strip()


def gh(repo: Path, *args: str) -> dict:
    try:
        value = json.loads(command(["gh", *args], repo))
    except json.JSONDecodeError as exc:
        raise ResolutionError("invalid gh JSON") from exc
    if not isinstance(value, dict):
        raise ResolutionError("invalid gh response")
    return value


def pin(oid: str, source: str, ref: str | None) -> dict:
    if not isinstance(oid, str) or not OID.fullmatch(oid):
        raise ResolutionError("invalid or missing OID")
    if ref is not None and (not isinstance(ref, str) or not ref or ref.startswith("-")):
        raise ResolutionError("invalid or missing fetch ref")
    return {"oid": oid, "source": source, "fetch_ref": ref}


def remote_pin(repo: Path, remote: str, branch: str | None = None) -> dict:
    if branch == "":
        raise ResolutionError("missing remote branch")
    url = command(["git", "remote", "get-url", "--", remote], repo)
    if not url or url.startswith("-"):
        raise ResolutionError("invalid remote URL")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ResolutionError("invalid remote URL") from exc
    if parsed.password is not None or (parsed.username is not None and parsed.scheme in ("http", "https")):
        raise ResolutionError("remote URL contains userinfo")
    scp = re.fullmatch(r"(?:[^/@:]+@)?[^/:@]+:.+", url)
    if "://" not in url and not scp:
        url = str((repo / url).resolve())
    pattern = "refs/heads/" + branch if branch else "HEAD"
    rows = command(["git", "ls-remote", "--symref", "--", url, pattern],
                   repo, network=True).splitlines()
    if branch is None:
        names = [r.split()[1] for r in rows if r.startswith("ref: ")
                 and r.split()[-1] == "HEAD"]
        if len(names) != 1 or not names[0].startswith("refs/heads/"):
            raise ResolutionError("no unique remote default branch")
        pattern = names[0]
    oids = [r.split()[0] for r in rows if len(r.split()) == 2
            and r.split()[1] == ("HEAD" if branch is None else pattern)]
    if len(oids) != 1:
        raise ResolutionError("remote ref unresolved or ambiguous")
    return pin(oids[0], url, pattern)


def ref_pin(repo: Path, ref: str, remotes: list[str]) -> dict:
    if not ref or ref.startswith("-"):
        raise ResolutionError("invalid ref")
    if ref.startswith(("refs/remotes/", "remotes/")):
        raise ResolutionError("use remote/branch instead of a tracking ref")
    anchor = re.split(r"@\{|[~^:]", ref, maxsplit=1)[0] or "HEAD"
    candidates = {anchor, "refs/" + anchor, "refs/remotes/" + anchor + "/HEAD",
                  *("refs/" + kind + "/" + anchor for kind in ("heads", "tags", "remotes"))}
    names = set(command(["git", "for-each-ref", "--format=%(refname)"], repo).splitlines())
    matches = candidates & names
    remote, sep, branch = ref.partition("/")
    if sep and remote in remotes:
        if any(name in matches for name in ("refs/heads/" + anchor, "refs/tags/" + anchor)):
            raise ResolutionError("ambiguous local and remote ref")
        return remote_pin(repo, remote, branch)
    if anchor in ("HEAD", "@", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "REBASE_HEAD",
                  "REVERT_HEAD", "CHERRY_PICK_HEAD", "AUTO_MERGE", "BISECT_HEAD"):
        pseudo = "HEAD" if anchor == "@" else anchor
        if (repo / command(["git", "rev-parse", "--git-path", pseudo], repo)).is_file():
            matches.add(pseudo)
    if len(matches) > 1:
        raise ResolutionError("ambiguous ref")
    if matches and re.fullmatch(r"[0-9a-fA-F]{4,64}", anchor) and command(
            ["git", "rev-parse", "--disambiguate=" + anchor], repo):
        raise ResolutionError("ambiguous object and ref")
    if any(name.startswith("refs/remotes/") for name in matches):
        raise ResolutionError("use remote/branch instead of a tracking ref")
    selector = re.split(r"[~^:]", ref, maxsplit=1)[0]
    name = command(["git", "rev-parse", "--symbolic-full-name", "--verify",
                    "--end-of-options", selector], repo) if selector else ""
    if name.startswith("refs/remotes/"):
        raise ResolutionError("use remote/branch instead of a tracking ref")
    oid = command(["git", "rev-parse", "--verify", "--end-of-options",
                   ref if ref.startswith(":/") else ref + "^{commit}"], repo)
    return pin(oid, str(repo), name if name.startswith("refs/") and (anchor == ref or ref == "@") else None)


def resolve(repo: str | Path, value: str, base: str | None = None,
            repository: str | None = None) -> dict:
    repo = Path(command(["git", "rev-parse", "--show-toplevel"], Path(repo))).resolve()
    remotes = command(["git", "remote"], repo).splitlines()
    url = PR_URL.fullmatch(value)
    if "://" in value and not url:
        raise ResolutionError("only GitHub PR URLs are supported")
    if url or re.fullmatch(r"[1-9][0-9]*", value):
        number = int(url[2] if url else value)
        target = url[1] if url else repository
        if url and repository and repository.casefold() != target.casefold():
            raise ResolutionError("PR URL and repository disagree")
        if target is None:
            target = gh(repo, "repo", "view", "--json", "nameWithOwner").get("nameWithOwner")
        if not isinstance(target, str) or not REPO.fullmatch(target):
            raise ResolutionError("missing or invalid target repository")
        api_target = None
        try:
            data = gh(repo, "pr", "view", str(number), "-R", target, "--json",
                      "number,url,headRefOid,baseRefName,baseRefOid,isCrossRepository")
        except ResolutionError as exc:
            if str(exc) != "unsupported baseRefOid":
                raise
            data = gh(repo, "api", f"repos/{target}/pulls/{number}")
            try:
                api_target = data["base"]["repo"]["full_name"]
                if not isinstance(api_target, str) or not REPO.fullmatch(api_target):
                    raise ResolutionError("missing API target repository")
                head_repo = data["head"]["repo"]
                data = {"number": data["number"], "url": data["html_url"],
                        "headRefOid": data["head"]["sha"], "baseRefOid": data["base"]["sha"],
                        "baseRefName": data["base"]["ref"],
                        "isCrossRepository": head_repo is None or
                        head_repo["full_name"].casefold() != api_target.casefold()}
            except (KeyError, TypeError, AttributeError) as err:
                raise ResolutionError("incomplete PR API response") from err
        source = gh(repo, "repo", "view", target, "--json", "url").get("url")
        canonical = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)",
                                 source if isinstance(source, str) else "")
        if not canonical:
            raise ResolutionError("target repository URL mismatch")
        target = canonical[1]
        if api_target is not None and (not isinstance(api_target, str)
                                      or api_target.casefold() != target.casefold()):
            raise ResolutionError("API target repository mismatch")
        expected = f"https://github.com/{target}/pull/{number}"
        if (type(data.get("number")) is not int or data["number"] != number
                or not isinstance(data.get("url"), str) or data["url"].casefold() != expected.casefold()
                or type(data.get("isCrossRepository")) is not bool):
            raise ResolutionError("PR identity missing or mismatched")
        head = pin(data.get("headRefOid"), source, f"refs/pull/{number}/head")
        branch = data.get("baseRefName")
        if not isinstance(branch, str) or not branch:
            raise ResolutionError("missing PR base branch")
        base_pin = pin(data.get("baseRefOid"), source, "refs/heads/" + branch)
        if base is not None:
            base_pin = ref_pin(repo, base, remotes)
        return {"kind": "pr", "operator_repo": str(repo), "target_url": source,
                "repository": target, "number": number, "url": expected,
                "is_cross_repository": data["isCrossRepository"], "head": head, "base": base_pin}
    if repository is not None:
        raise ResolutionError("--repository requires a PR input")
    head = ref_pin(repo, value, remotes)
    if base is not None:
        base_pin = ref_pin(repo, base, remotes)
    else:
        remote = value.partition("/")[0]
        if remote not in remotes:
            if len(remotes) != 1:
                raise ResolutionError("--base required without a unique remote")
            remote = remotes[0]
        base_pin = remote_pin(repo, remote)
    return {"kind": "ref", "operator_repo": str(repo),
            "target_url": base_pin["source"], "head": head, "base": base_pin}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--repository", "-R")
    parser.add_argument("--base")
    args = parser.parse_args()
    try:
        result = resolve(args.repo, args.input, args.base, args.repository)
    except ResolutionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
