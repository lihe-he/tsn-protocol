"""CLI.

刻意保持极简：argparse + 标准输入，不引入 click/typer。
需要 Web 界面的人去用 services/api，不要把 CLI 做成半个前端。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from pathlib import Path

from tsn import __version__
from tsn.adapters import available as list_adapters
from tsn.client import PACE_SEC_PER_KM, RunOptions, TSNClient
from tsn.core.config import AppConfig
from tsn.core.errors import TSNError
from tsn.core.models import RunType

STATE_VERSION = 1


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ─────────────────────────  state  ─────────────────────────


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("v") != STATE_VERSION:
        return {}
    return data


def _save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**data, "v": STATE_VERSION}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ─────────────────────────  子命令  ─────────────────────────


async def cmd_schools(args: argparse.Namespace, cfg: AppConfig) -> int:
    async with TSNClient(cfg) as client:
        if args.keyword:
            schools = await client.find_school(args.keyword)
        else:
            schools = await client.fetch_schools()

    if not schools:
        print("没有匹配的学校")
        return 1

    print(f"{'ID':<8}{'学校':<28}{'版本':<6}{'加密':<6}{'代号'}")
    print("-" * 70)
    for s in schools[: args.limit]:
        ver = "公版" if s.is_public else "私版"
        print(f"{s.school_id:<8}{s.name:<28}{ver:<6}{'是' if s.is_open_encry else '否':<6}{s.code}")
    if len(schools) > args.limit:
        print(f"... 共 {len(schools)} 所，用 --limit 查看更多")
    return 0


async def cmd_login(args: argparse.Namespace, cfg: AppConfig) -> int:
    async with TSNClient(cfg) as client:
        schools = await client.find_school(args.school)
        if not schools:
            print(f"找不到学校: {args.school}")
            return 1
        if len(schools) > 1:
            print("匹配到多所学校，请用更精确的名字或 ID：")
            for s in schools[:20]:
                print(f"  {s.school_id}  {s.name}")
            return 1

        school = schools[0]
        print(f"学校: {school.name} ({'公版' if school.is_public else '私版'})")
        username = args.username or input("学号/用户名: ").strip()
        password = args.password or getpass.getpass("密码: ")
        if not username or not password:
            print("账号密码不能为空")
            return 1

        account, adapter = await client.login(school, username, password)
        _save_state(
            cfg.state_file,
            {
                "school": school.model_dump(mode="json"),
                "username": username,
                "uid": account.uid,
                "token": account.token,
                "device": account.device.model_dump(mode="json"),
            },
        )
        print(f"登录成功，uid={account.uid}")
        print(f"状态已保存到 {cfg.state_file}")
        print("提醒：同一账号不要在手机上同时登录，会互相踢下线")
    return 0


async def cmd_run(args: argparse.Namespace, cfg: AppConfig) -> int:
    state = _load_state(cfg.state_file)
    if not state.get("token"):
        print("未登录，先执行: tsn login")
        return 1

    from tsn.adapters import AdapterContext, create
    from tsn.core.models import School
    from tsn.core.transport import unwrap_data
    from tsn.engine import validate as validate_mod

    school = School.model_validate(state["school"])

    async with TSNClient(cfg) as client:
        if state.get("device"):
            cfg.device.model_validate(state["device"])

        cloud = await client.resolve_cloud_base(school)
        ctx = AdapterContext(
            school=school,
            device=cfg.device,
            transport=client.transport,
            token=state["token"],
            uid=state.get("uid", ""),
            cloud_base=cloud,
        )
        adapter = create("", ctx)

        opts = RunOptions(
            distance_km=args.km,
            run_type=RunType[args.type],
            pace_sec_per_km=args.pace,
            wait=not args.no_wait,
            dry_run=args.dry_run,
            seed=args.seed,
        )

        draft = await client.prepare(adapter, school, opts)
        print("\n" + "=" * 70)
        print(f"目标      : {opts.distance_km:.2f} km ({opts.run_type.name})")
        print(f"实际生成  : {draft.summary()}")
        print(f"起点      : {draft.track.points[0].lat:.6f}, {draft.track.points[0].lng:.6f}")
        print(f"起止时间  : {draft.start_ts_ms / 1000:.0f} → {draft.end_ts_ms / 1000:.0f}")
        print(f"步数桶    : {draft.steps.summary()}")
        print(f"规则要求  : {draft.setting.total_range_m / 1000:.2f} km / {draft.setting.step_required} 步")
        print("=" * 70 + "\n")

        if args.dry_run:
            print("dry-run 模式，不提交")
            return 0
        if not args.yes and sys.stdin.isatty():
            if input("确认提交？(y/N): ").strip().lower() != "y":
                print("已取消")
                return 1

        result = await client.execute(adapter, draft, opts)
        print("\n" + result.explain())
        return 0 if result.accepted else 2


async def cmd_status(args: argparse.Namespace, cfg: AppConfig) -> int:
    state = _load_state(cfg.state_file)
    if not state.get("token"):
        print("未登录，先执行: tsn login")
        return 1

    from tsn.adapters import AdapterContext, create
    from tsn.core.models import School

    school = School.model_validate(state["school"])
    async with TSNClient(cfg) as client:
        cloud = await client.resolve_cloud_base(school)
        ctx = AdapterContext(
            school=school,
            device=cfg.device,
            transport=client.transport,
            token=state["token"],
            uid=state.get("uid", ""),
            cloud_base=cloud,
        )
        adapter = create("", ctx)
        summary = await adapter.get_summary()
        stats = await adapter.statistics()

    print(f"账号  : {state.get('username')}")
    print(f"学校  : {school.name}")
    print("\n跑步汇总:")
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:2000])
    print("\n统计:")
    print(json.dumps(stats, ensure_ascii=False, indent=2)[:2000])
    return 0


def cmd_adapters(args: argparse.Namespace, cfg: AppConfig) -> int:
    rows = list_adapters()
    if not rows:
        print("没有已注册的适配器")
        return 1
    print(f"{'名称':<12}{'支持版本'}")
    print("-" * 40)
    for row in rows:
        print(f"{row['name']:<12}{row['range']}")
    return 0


# ─────────────────────────  参数  ─────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tsn",
        description="运动的服务端协议 SDK —— 协议兼容性研究工具",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    p.add_argument("--version", action="version", version=f"tsn-protocol {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("schools", help="列出/搜索学校")
    sp.add_argument("keyword", nargs="?", help="学校名或 ID")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_schools)

    sp = sub.add_parser("login", help="登录并保存状态")
    sp.add_argument("school", help="学校名或 ID")
    sp.add_argument("--username")
    sp.add_argument("--password")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("run", help="生成并提交一次跑步记录")
    sp.add_argument("--km", type=float, default=2.0, help="目标距离（公里）")
    sp.add_argument("--type", choices=[t.name for t in RunType], default="SUN")
    sp.add_argument("--pace", type=float, default=None, help=f"配速秒/公里，默认在 {PACE_SEC_PER_KM} 区间随机")
    sp.add_argument("--no-wait", action="store_true", help="不等真实时间，立刻提交")
    sp.add_argument("--dry-run", action="store_true", help="只生成不提交")
    sp.add_argument("--seed", type=int, default=None)
    sp.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="查询跑步汇总与统计")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("adapters", help="列出已注册的协议适配器")
    sp.set_defaults(func=cmd_adapters)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    cfg = AppConfig.from_env()
    cfg.ensure_dirs()

    try:
        result = args.func(args, cfg)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return int(result or 0)
    except TSNError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
