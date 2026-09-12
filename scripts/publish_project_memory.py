"""只在 main 的 push 工作流更新固定状态页；分支摘要绝不能覆盖主线记忆。"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
from pathlib import Path
import sys

from scripts.refresh_project_memory import SHA, git, runtime_report

REPO = 'chasen2041maker/BidRadar'
ISSUE = 4
MARKER = '<!-- bidradar-memory-status -->'


def api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    """固定访问 GitHub API，不跟随重定向、不输出令牌；HTTP 失败不伪装为刷新成功。"""
    connection = http.client.HTTPSConnection('api.github.com', timeout=20)
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'BidRadar-memory', 'Content-Type': 'application/json'}
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read(2_000_001)
        if not 200 <= response.status < 300 or len(raw) > 2_000_000:
            raise ValueError(f'GitHub API 失败或响应过大: HTTP {response.status}')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('GitHub API 返回结构不是对象')
        return result
    finally:
        connection.close()


def publish(root: Path) -> bool:
    """返回是否写入；较旧的排队任务跳过，不用旧提交覆盖更新的 main 快照。"""
    if (os.environ.get('GITHUB_REPOSITORY') != REPO or os.environ.get('GITHUB_EVENT_NAME') != 'push' or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        raise ValueError('只有本仓库 main push 可以更新固定记忆页')
    token = os.environ.get('GH_TOKEN', '')
    head = git(root, 'rev-parse', 'HEAD').strip()
    if not token or not SHA.fullmatch(head) or head != os.environ.get('GITHUB_SHA'):
        raise ValueError('缺少令牌或实际 checkout 与事件 SHA 不一致')
    prefix = f'/repos/{REPO}'
    current = api('GET', prefix + '/git/ref/heads/main', token)
    remote_sha = (current.get('object') or {}).get('sha')
    if not isinstance(remote_sha, str) or not SHA.fullmatch(remote_sha):
        raise ValueError('远端 main 返回的 SHA 不可核对')
    if remote_sha != head:
        print('SKIP: main 已变化；由更新提交的任务发布快照')
        return False
    issue = api('GET', prefix + f'/issues/{ISSUE}', token)
    # Marker 是误写保护，不是权限系统；真正的写权限只给受控 main 发布任务。
    if issue.get('state') != 'open' or 'pull_request' in issue or MARKER not in (issue.get('body') or ''):
        raise ValueError('固定状态页已关闭、类型错误或缺少标记，停止覆盖')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not run_id.isdigit():
        raise ValueError('缺少可核对的运行编号')
    body = MARKER + '\n' + runtime_report(root)
    body += f'\n[本次运行证据](https://github.com/{REPO}/actions/runs/{run_id}) · [对应版本学习索引](https://github.com/{REPO}/blob/{head}/docs/learning/INDEX.md)\n'
    body += '\n此页是自动观察快照，不是业务完成证明。源笔记随 Git 保留；关闭本 Issue 可停止自动覆盖。\n'
    api('PATCH', prefix + f'/issues/{ISSUE}', token, {'body': body})
    print(f'UPDATED: issue #{ISSUE} at {head}')
    return True


def main() -> int:
    try:
        publish(Path(__file__).resolve().parents[1])
        return 0
    except (ValueError, OSError, http.client.HTTPException, subprocess.SubprocessError) as exc:
        print(f'记忆发布失败: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
