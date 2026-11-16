"""提交前自校验.

服务端会拒掉「数据不自洽」的记录，但它的报错只有一句 remark。
与其提交后猜原因，不如提交前自己先查一遍。

校验的是**内部一致性**，不是「像不像真的」：
    · 每段距离 ≈ 速度 × dt
    · 累计距离单调
    · 时间戳严格递增
    · 步数桶合计 == 总步数
    · 配速/步频在人类范围内
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from tsn.core.models import ExerciseSetting, RunDraft, Track
from tsn.engine import geo
from tsn.engine.physics import stride as stride_mod
from tsn.engine.steps import BucketAllocation

DISTANCE_EPS_M = 0.5
SPEED_TOLERANCE_RATIO = 0.002
CHORD_WARN_RATIO = 0.15


@dataclass(slots=True)
class Issue:
    code: str
    message: str
    severity: str = "error"  # error | warning
    context: dict = field(default_factory=dict)


@dataclass(slots=True)
class ValidationReport:
    ok: bool
    issues: list[Issue] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def summary(self) -> str:
        head = "通过" if self.ok else "未通过"
        return f"校验{head}：error={len(self.errors)} warning={len(self.warnings)} {self.metrics}"

    def raise_if_failed(self) -> None:
        if self.ok:
            return
        detail = "; ".join(f"{i.code}: {i.message}" for i in self.errors)
        raise ValueError(f"轨迹校验未通过 -> {detail}")


def validate_track(
    track: Track,
    *,
    bucket: BucketAllocation | None = None,
    setting: ExerciseSetting | None = None,
) -> ValidationReport:
    issues: list[Issue] = []
    pts = track.points

    if not pts:
        return ValidationReport(ok=False, issues=[Issue("empty_track", "轨迹为空")])
    if len(pts) < 2:
        return ValidationReport(ok=False, issues=[Issue("too_few_points", "轨迹点少于 2 个")])

    # 1. 时间戳严格递增
    for i in range(1, len(pts)):
        dt = pts[i].t_ms - pts[i - 1].t_ms
        if not math.isfinite(dt) or dt <= 0:
            issues.append(Issue("timestamp_not_increasing", f"第 {i} 点时间戳未递增: dt={dt}", context={"i": i}))
            break

    # 2. 每段距离 ≈ 速度 × dt
    for i, p in enumerate(pts):
        if i == 0:
            continue
        dt = (pts[i].t_ms - pts[i - 1].t_ms) / 1000.0
        if dt <= 0:
            continue
        expected = p.speed_mps * dt
        tol = max(DISTANCE_EPS_M, abs(expected) * SPEED_TOLERANCE_RATIO)
        if abs(p.distance_m - expected) > tol:
            issues.append(
                Issue(
                    "speed_distance_mismatch",
                    f"第 {i} 段 |distance - speed×dt| = {abs(p.distance_m - expected):.4f} > {tol:.4f}",
                    context={"i": i, "d": p.distance_m, "v": p.speed_mps, "dt": dt},
                )
            )
            if len([x for x in issues if x.code == "speed_distance_mismatch"]) > 5:
                break

    # 3. 累计距离单调
    for i in range(1, len(pts)):
        if pts[i].cumulative_m < pts[i - 1].cumulative_m - DISTANCE_EPS_M:
            issues.append(Issue("cumulative_not_monotonic", f"第 {i} 点累计距离回退", context={"i": i}))
            break

    # 4. 弦长偏差（折返/密集折点属正常，只告警）
    deviations: list[tuple[int, float, float, float]] = []
    for i in range(1, len(pts)):
        chord = geo.haversine(pts[i - 1].lat, pts[i - 1].lng, pts[i].lat, pts[i].lng)
        if chord > 0.1:
            deviations.append((i, pts[i].distance_m, chord, abs(pts[i].distance_m - chord) / chord))
    if deviations:
        worst = max(deviations, key=lambda x: x[3])
        if worst[3] > CHORD_WARN_RATIO:
            issues.append(
                Issue(
                    "chord_deviation",
                    f"第 {worst[0]} 段推进 {worst[1]:.2f}m vs 弦长 {worst[2]:.2f}m（{worst[3] * 100:.0f}%）",
                    severity="warning",
                    context={"worst": worst[0], "segments": len(deviations)},
                )
            )

    # 5. 步数桶总和
    if bucket is not None:
        if bucket.buckets and sum(bucket.buckets) != bucket.total:
            issues.append(Issue("bucket_sum_mismatch", f"桶合计 {sum(bucket.buckets)} != {bucket.total}"))

    # 6. 人类合理性
    avg_speed = track.avg_speed_mps
    if avg_speed > 6.0:
        issues.append(Issue("speed_too_high", f"均速 {avg_speed:.2f} m/s 超过人类跑步上限", severity="warning"))
    if avg_speed < 0.5:
        issues.append(Issue("speed_too_low", f"均速 {avg_speed:.2f} m/s 过低", severity="warning"))

    if bucket is not None and bucket.buckets:
        minutes = len(bucket.buckets)
        elapsed_min = max(1.0, track.elapsed_s / 60.0)
        cadence = bucket.total / elapsed_min
        if cadence > 240:
            issues.append(Issue("cadence_too_high", f"平均步频 {cadence:.0f} spm 不合理", severity="warning"))
        if cadence < 60:
            issues.append(Issue("cadence_too_low", f"平均步频 {cadence:.0f} spm 不合理", severity="warning"))
        if minutes and max(bucket.buckets) > bucket.total * 0.6 and len(bucket.buckets) > 2:
            issues.append(Issue("bucket_skew", "单桶步数占比过高，分布过于集中", severity="warning"))

    # 7. 是否满足服务端规则
    if setting is not None:
        total_steps = bucket.total if bucket else 0
        if track.total_distance_m + DISTANCE_EPS_M < setting.total_range_m:
            issues.append(
                Issue(
                    "distance_below_requirement",
                    f"距离 {track.total_distance_m:.0f}m 未达要求 {setting.total_range_m:.0f}m",
                )
            )
        if setting.start_qualified_s and track.elapsed_s < setting.start_qualified_s:
            issues.append(
                Issue(
                    "time_below_minimum",
                    f"用时 {track.elapsed_s:.0f}s 低于下限 {setting.start_qualified_s:.0f}s",
                )
            )
        need_steps = setting.required_steps_for(track.total_distance_m)
        if need_steps and total_steps < need_steps:
            issues.append(Issue("steps_below_requirement", f"步数 {total_steps} 少于要求 {need_steps}"))
        if setting.limit_speed and avg_speed > setting.limit_speed:
            issues.append(
                Issue("speed_above_limit", f"均速 {avg_speed:.2f} 超过限制 {setting.limit_speed:.2f}")
            )

    metrics = {
        "points": float(len(pts)),
        "distance_m": round(track.total_distance_m, 1),
        "elapsed_s": round(track.elapsed_s, 1),
        "avg_speed_mps": round(avg_speed, 2),
        "steps": float(bucket.total if bucket else 0),
    }
    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues, metrics=metrics)


def validate_draft(draft: RunDraft) -> ValidationReport:
    return validate_track(draft.track, bucket=draft.steps, setting=draft.setting)
