#!/usr/bin/env python3
"""把仓库里的 README 同步到 Docker Hub 仓库页（短描述 + Overview）。

为什么要它：Hub 页面是别人看到的第一眼 —— 镜像更新了、说明还停在旧版就是误导。
所以每次构建推送成功后，由 CI 自动把项目 README 同步过去，避免"说明与镜像脱节"。

只用标准库，不引第三方依赖；凭据只从环境变量读，不落盘。

环境变量：
  DOCKERHUB_USERNAME  用户名（默认 xinxinenjoy）
  DOCKERHUB_TOKEN     Docker Hub Access Token（Read & Write）
                      ⚠️ 它【不能】当 Authorization: Bearer 用（会 401），
                         但可以当 /v2/users/login/ 的 password 换 JWT（实测 2026-10-05）
  HUB_REPO            仓库名，如 115offline
  README_PATH         要同步的文件，如 115offline/README.md
  SHORT_DESCRIPTION   短描述（搜索结果里显示的那行）。⛔ 上限 100 **字节**，中文约 33 字
  GITHUB_REPO         owner/repo，用于把 README 里的相对链接改写成绝对链接（默认 xinxinenjoy/Docker）
  GITHUB_BRANCH       分支名（默认 main）
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://hub.docker.com/v2"


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"❌ 缺少环境变量 {name}")
    return v


def request(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "sync-hub-description/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def absolutize_links(md, project_dir, owner_repo, branch):
    """README 在 GitHub 上能用的相对链接，直接搬到 Hub 会变成死链 ⇒ 改写成绝对地址。
    只处理 [文字](目标) 形式的目标，跳过 http(s):// 、mailto: 、#锚点、以及 data: 。"""
    base = f"https://github.com/{owner_repo}"
    # 项目目录（README 所在目录）相对于仓库根
    if project_dir in ("", "."):
        dir_prefix = ""
    else:
        dir_prefix = project_dir.strip("/") + "/"

    def repl(m):
        text, target = m.group(1), m.group(2)
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|//)", target):
            return m.group(0)
        # 仓库内部相对引用统一指到 blob（文件）或 tree（目录）
        clean = target.lstrip("./")
        path = dir_prefix + clean
        if clean in ("", "."):
            path = dir_prefix.rstrip("/") or ""
        kind = "tree" if clean.endswith("/") else "blob"
        if path.endswith("/"):
            path = path.rstrip("/")
            kind = "tree"
        return f"[{text}]({base}/{kind}/{branch}/{path})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", repl, md)


def main():
    user = env("DOCKERHUB_USERNAME", "xinxinenjoy")
    token = env("DOCKERHUB_TOKEN", required=True)
    repo = env("HUB_REPO", required=True)
    readme_path = env("README_PATH", required=True)
    short = env("SHORT_DESCRIPTION", "")
    owner_repo = env("GITHUB_REPO", "xinxinenjoy/Docker")
    branch = env("GITHUB_BRANCH", "main")

    # 短描述：Hub 上限 100 字节（中文一个字 3 字节），本地先卡住，别等远端报 validation error
    if short:
        nbytes = len(short.encode("utf-8"))
        print(f"短描述：{short}")
        print(f"  字符数 {len(short)} / 字节数 {nbytes}（上限 100）")
        if nbytes > 100:
            sys.exit(f"❌ 短描述超长：{nbytes} 字节 > 100 字节。请压到约 33 个汉字以内。")
    else:
        short = None
        print("短描述：未提供，保持 Hub 上的原值")

    with open(readme_path, encoding="utf-8") as f:
        md = f.read()
    project_dir = os.path.dirname(readme_path)
    md = absolutize_links(md, project_dir, owner_repo, branch)
    print(f"Overview：{readme_path}（{len(md)} 字符，相对链接已改写为绝对地址）")

    status, body = request(
        "POST",
        f"{API}/users/login/",
        {"username": user, "password": token},
    )
    if status != 200:
        sys.exit(f"❌ 登录失败 HTTP {status}：{body[:300]}")
    jwt = json.loads(body).get("token")
    if not jwt:
        sys.exit(f"❌ 登录成功但没拿到 token：{body[:200]}")
    print(f"登录成功：{user}（JWT {len(jwt)} 字符）")

    payload = {"full_description": md}
    if short is not None:
        payload["description"] = short

    status, body = request(
        "PATCH",
        f"{API}/repositories/{user}/{repo}/",
        payload,
        headers={"Authorization": f"JWT {jwt}"},
    )
    if status != 200:
        sys.exit(f"❌ 同步失败 HTTP {status}：{body[:400]}")

    got = json.loads(body)
    print(f"✅ 已同步 {user}/{repo}")
    print(f"   description      : {got.get('description')!r}")
    print(f"   full_description : {len(got.get('full_description') or '')} 字符")
    print(f"   last_updated     : {got.get('last_updated')}")


if __name__ == "__main__":
    main()
