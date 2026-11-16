"""业务组合层.

把 core（协议）、adapters（版本）、engine（造数据）串起来，对外只暴露
「登录 / 跑一次 / 查结果」这几个动作。

编排顺序有讲究：
    setting → 路线 → 轨迹 → 人脸 → 等待 → 提交 → 查判定
因为 setting 里的 geofence 决定路线范围，路线首点又决定人脸验证的坐标。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

import numpy as np

from tsn.adapters import AdapterContext, create
from tsn.adapters.base import ProtocolAdapter
from tsn.core import routes
from tsn.core.config import AppConfig
from tsn.core.errors import EngineError, RemoteRejected
from tsn.core.models import (
    Account,
    ExerciseSetting,
    FaceType,
    RunDraft,
    RunResult,
    RunType,
    School,
    now_ms,
)
from tsn.core.transport import Transport, unwrap_data
from tsn.engine import face as face_mod
from tsn.engine import geo, track as track_mod, validate as validate_mod
from tsn.engine.path import HistoryPathSource, OSMPathSource, RoutePlan, osm_available

log = logging.getLogger("tsn.client")

PACE_SEC_PER_KM = (300.0, 390.0)  # 5'00" ~ 6'30"


@dataclass(slots=True)
class RunOptions:
    """一次跑步的可调项."""

    distance_km: float = 2.0
    run_type: RunType = RunType.SUN
    pace_sec_per_km: float | None = None   # 不填就在合理区间随机
    wait: bool = True                       # 是否真的等到 endTime
    dry_run: bool = False                   # 只生成不提交
    upload_faces: bool = True
    seed: int | None = None


class TSNClient:
    """一个学校的会话."""

    def __init__(self, cfg: AppConfig | None = None) -> None:
        self.cfg = cfg or AppConfig.from_env()
        self._transport: Transport | None = None
        self._schools: list[School] = []

    # ── 生命周期 ──

    async def __aenter__(self) -> TSNClient:
        tc = self.cfg.transport
        self._transport = Transport(
            timeout=tc.timeout,
            retries=tc.retries,
            backoff=tc.backoff,
            user_agent=tc.user_agent,
            debug=tc.debug,
        )
        await self._transport.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._transport is not None:
            await self._transport.__aexit__(*exc)
            self._transport = None

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            raise RuntimeError("TSNClient 必须在 async with 中使用")
        return self._transport

    # ── 学校 ──

    async def fetch_schools(self) -> list[School]:
        """拉全量学校（公开接口，不需要登录）."""
        url = routes.get("school.datas").render("")
        body = await self.transport.get(url, {"pageIndex": 1, "pageSize": 5000})
        raw = unwrap_data(body)
        items: list[Any] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            for key in ("data", "list", "rows"):
                if isinstance(raw.get(key), list):
                    items = raw[key]
                    break

        self._schools = [_to_school(x) for x in items if isinstance(x, dict)]
        log.info("拉到 %d 所学校", len(self._schools))
        return self._schools

    async def find_school(self, keyword: str) -> list[School]:
        if not self._schools:
            await self.fetch_schools()
        key = keyword.strip()
        if key.isdigit():
            return [s for s in self._schools if str(s.school_id) == key]
        return [s for s in self._schools if key in s.name]

    # ── 会话 ──

    def adapter_for(self, school: School, *, app_version: str | None = None) -> ProtocolAdapter:
        version = app_version or self.cfg.__dict__.get("app_version", "") or ""
        device = self.cfg.device.model_copy(deep=True)
        ctx = AdapterContext(
            school=school,
            device=device,
            transport=self.transport,
            app_version=version,
            cloud_base=school.cloud_base,
        )
        return create(version, ctx)

    async def login(self, school: School, username: str, password: str) -> tuple[Account, ProtocolAdapter]:
        adapter = self.adapter_for(school)
        result = await adapter.login(username, password)
        adapter.ctx.uid = result.uid
        adapter.ctx.token = result.token
        account = Account(
            username=username,
            password=password,
            school=school,
            uid=result.uid,
            token=result.token,
            device=adapter.ctx.device,
        )
        log.info("登录成功 uid=%s school=%s", result.uid, school.name)
        return account, adapter

    # ── 跑步编排 ──

    async def prepare(
        self,
        adapter: ProtocolAdapter,
        school: School,
        options: RunOptions,
    ) -> RunDraft:
        rng = np.random.default_rng(options.seed)
        setting = await self._load_setting(adapter, school, options.run_type, rng)
        log.info(
            "规则：要求 %.2fkm / 最少 %d 步 / 合格时间 %.0fs+",
            setting.total_range_m / 1000,
            setting.step_required,
            setting.start_qualified_s,
        )

        route = await self._pick_route(adapter, school, options.distance_km * 1000, setting)
        log.info("选用路线：%s (%s, %.0fm)", route.name, route.source, route.length_m)

        # 距离要覆盖服务端要求，否则必然被拒
        target_m = max(options.distance_km * 1000, setting.total_range_m * 1.02)
        pace = options.pace_sec_per_km or float(rng.uniform(*PACE_SEC_PER_KM))
        plan_s = target_m / 1000.0 * pace

        start_ms = await adapter.get_start_time(setting.identify)
        # 服务端给的 startTime 是「允许起跑时刻」，稍微延后一点更自然
        start_ms += random.randint(14_000, 20_000)

        opts = track_mod.TrackOptions.from_config(self.cfg.engine)
        opts.apply_noise = True

        trk, bucket = track_mod.generate(
            route.points,
            target_distance_m=target_m,
            start_ts_ms=start_ms,
            plan_use_time_s=plan_s,
            options=opts,
            rng=rng,
        )

        # 步数不达标时按比例补，补齐后仍然是按时间分布的
        need_steps = setting.required_steps_for(trk.total_distance_m)
        if bucket.total < need_steps:
            log.warning("自然步数 %d < 要求 %d，按比例补齐", bucket.total, need_steps)
            bucket = track_mod.build_steps(trk, force_total=need_steps)

        end_ms = start_ms + trk.elapsed_s * 1000.0
        draft = RunDraft(
            school=school,
            run_type=options.run_type,
            setting=setting,
            track=trk,
            steps=bucket,
            start_ts_ms=start_ms,
            end_ts_ms=end_ms,
            identify=setting.identify,
        )

        report = validate_mod.validate_draft(draft)
        if not report.ok:
            for issue in report.errors:
                log.error("校验失败 %s: %s", issue.code, issue.message)
            report.raise_if_failed()
        for issue in report.warnings:
            log.warning("校验提示 %s: %s", issue.code, issue.message)
        log.info("预热完成：%s", draft.summary())
        return draft

    async def execute(self, adapter: ProtocolAdapter, draft: RunDraft, options: RunOptions) -> RunResult:
        if options.dry_run:
            log.info("dry-run，不提交")
            return RunResult(sport_status="1", remark="dry-run")

        if options.upload_faces:
            await self._upload_faces(adapter, draft)

        if options.wait:
            wait_s = draft.end_ts_ms / 1000.0 - __import__("time").time()
            if wait_s > 0:
                log.info("等待 %.0f 秒直到 endTime（可 Ctrl-C 中止）", wait_s)
                await asyncio.sleep(wait_s)

        record_id, raw = await adapter.submit_record(draft)
        log.info("已提交，record_id=%s", record_id)

        if not record_id:
            return RunResult(sport_status="", remark="服务端未返回记录 id", raw=raw)

        # 服务端异步判定，稍等再查
        for attempt in range(3):
            await asyncio.sleep(2 + attempt * 2)
            try:
                result = await adapter.get_record(record_id)
            except RemoteRejected as exc:
                log.warning("查判定失败: %s", exc)
                continue
            log.info("判定结果：%s", result.explain())
            if result.sport_status:
                return result

        return RunResult(record_id=record_id, sport_status="", remark="判定未返回", raw=raw)

    # ── 内部步骤 ──

    async def _load_setting(
        self,
        adapter: ProtocolAdapter,
        school: School,
        run_type: RunType,
        rng: np.random.Generator,
    ) -> ExerciseSetting:
        """先拿一份 setting 才知道围栏和规则；初始坐标用学校附近."""
        probe = await self._probe_point(adapter, school, rng)
        setting = await adapter.get_setting(int(run_type), probe[0], probe[1])

        if not setting.identify:
            raise EngineError("服务端未返回 identify，无法发起跑步")
        return setting

    async def _probe_point(
        self,
        adapter: ProtocolAdapter,
        school: School,
        rng: np.random.Generator,
    ) -> tuple[float, float]:
        """探测点：优先用历史路线首点，其次围栏中心，最后学校坐标."""
        source = HistoryPathSource(fetch=adapter.list_records)
        try:
            candidates = await source.get_routes(school, target_m=2000.0, limit=3)
            if candidates:
                lng, lat = candidates[0].points[0]
                return (lng, lat)
        except Exception as exc:  # noqa: BLE001
            log.debug("历史路线不可用: %s", exc)

        lat = getattr(school, "latitude", None)
        lng = getattr(school, "longitude", None)
        if lat and lng:
            return (float(lng), float(lat))

        raise EngineError("无法确定起点：既没有历史记录，学校也没有坐标")

    async def _pick_route(
        self,
        adapter: ProtocolAdapter,
        school: School,
        target_m: float,
        setting: ExerciseSetting,
    ) -> RoutePlan:
        """路线优先级：历史记录 > OSM > 报错."""
        history = HistoryPathSource(fetch=adapter.list_records)
        try:
            candidates = await history.get_routes(school, target_m=target_m, limit=10)
            if candidates:
                return random.choice(candidates[:3])
        except Exception as exc:  # noqa: BLE001
            log.warning("历史路线失败: %s", exc)

        if osm_available():
            center = geo.polygon_center(setting.geofence) or self._school_point(school)
            osm = OSMPathSource(center=center)
            try:
                candidates = await osm.get_routes(school, target_m=target_m, limit=10)
                if candidates:
                    return random.choice(candidates[:3])
            except Exception as exc:  # noqa: BLE001
                log.warning("OSM 路线失败: %s", exc)

        raise EngineError(
            "没有可用路线：账号无历史记录且 OSM 不可用。"
            "先跑一次，或安装 osmnx（pip install 'tsn-protocol[osm]'）"
        )

    @staticmethod
    def _school_point(school: School) -> tuple[float, float] | None:
        lat = getattr(school, "latitude", None)
        lng = getattr(school, "longitude", None)
        if lat and lng:
            return (float(lng), float(lat))
        return None

    async def _upload_faces(self, adapter: ProtocolAdapter, draft: RunDraft) -> None:
        """按 setting 的要求在对应位置传人脸."""
        setting = draft.setting
        need = setting.is_start_face or setting.is_midway_face or setting.is_end_face
        if not need:
            log.info("该校不要求人脸验证")
            return

        images = await self._face_materials(adapter)
        if not images:
            raise EngineError("该校要求人脸验证，但账号没有可用人脸素材；请先在官方 App 上传")

        points = draft.track.points
        if setting.is_start_face:
            first = points[0]
            await adapter.upload_face(
                face_mod.perturb(face_mod.pick_face(images), rng=random.Random(draft.start_ts_ms)),
                coords=f"{first.lat:.6f},{first.lng:.6f}",
                identify=draft.identify,
                face_type=int(FaceType.START),
            )
            draft.face_uploaded += 1
            log.info("开始点人脸已上传")

        if setting.is_midway_face:
            positions = setting.middle_face_positions or [0.75]
            for pos in positions:
                idx = max(1, min(len(points) - 2, int(len(points) * float(pos))))
                p = points[idx]
                await adapter.upload_face(
                    face_mod.perturb(face_mod.pick_face(images), rng=random.Random(int(p.t_ms))),
                    coords=f"{p.lat:.6f},{p.lng:.6f}",
                    identify=draft.identify,
                    face_type=int(FaceType.MIDDLE),
                )
                draft.face_uploaded += 1
                log.info("中途人脸已上传 @%.0f%%", float(pos) * 100)

        if setting.is_end_face:
            last = points[-1]
            await adapter.upload_face(
                face_mod.perturb(face_mod.pick_face(images), rng=random.Random(int(last.t_ms))),
                coords=f"{last.lat:.6f},{last.lng:.6f}",
                identify=draft.identify,
                face_type=int(FaceType.END),
            )
            draft.face_uploaded += 1
            log.info("结束点人脸已上传")

    async def _face_materials(self, adapter: ProtocolAdapter) -> list[bytes]:
        """本地缓存优先，没有就从服务端拉."""
        import httpx
        import io

        cache = self.cfg.face_dir
        cache.mkdir(parents=True, exist_ok=True)
        files = sorted([p for p in cache.glob("*.jpg")] + [p for p in cache.glob("*.png")])
        if files:
            return [p.read_bytes() for p in files]

        try:
            items = await adapter.list_user_faces()
        except RemoteRejected as exc:
            log.warning("拉取人脸列表失败: %s", exc)
            return []

        out: list[bytes] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for idx, item in enumerate(items):
                url = item.get("imageRouteUrl") or item.get("url")
                if not url:
                    continue
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    data = resp.content
                    (cache / f"{item.get('id', idx)}.jpg").write_bytes(data)
                    out.append(data)
                except httpx.HTTPError as exc:
                    log.debug("下载人脸失败: %s", exc)
        log.info("获取到 %d 张人脸素材", len(out))
        return out


def _to_school(raw: dict[str, Any]) -> School:
    from tsn.core.models import SysType

    return School(
        school_id=int(raw.get("school_id") or raw.get("schoolId") or 0),
        name=str(raw.get("school_name") or raw.get("schoolName") or ""),
        code=str(raw.get("schoolCode") or ""),
        url=str(raw.get("school_url") or raw.get("schoolUrl") or ""),
        lan_url=(raw.get("lan_url") or raw.get("lanUrl") or None),
        open_id=str(raw.get("openId") or ""),
        sys_type=SysType(int(raw.get("sysType") or 2)),
        is_open_keep=str(raw.get("isOpenKeep")) == "1",
        is_open_live=str(raw.get("isOpenLive")) == "1",
        is_open_encry=str(raw.get("isOpenEncry")) == "1",
        province=raw.get("provinceName"),
    )
