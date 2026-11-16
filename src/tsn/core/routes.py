"""明文路由表.

上一代实现用 md5(token) 当字典键做「混淆」，代价是可读性归零、
新增接口要去手算哈希。这里直接明文声明，混淆留给 minifier。

两类路由：
  1. 公共接口  —— 挂在 m.boxkj.com，用 md5(params + appId + appSecret) 签名
  2. 租户接口  —— 挂在 cloudUrl（默认 a.tsnkj.com），用 AES+RSA 加密参数
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter

PUBLIC_BASE = "https://m.boxkj.com"
DEFAULT_CLOUD_BASE = "https://a.tsnkj.com"


@dataclass(frozen=True, slots=True)
class Route:
    """一条接口."""

    method: str          # GET / POST
    path: str            # 相对路径，支持 {school_id} 占位
    base: str = "public" # public | cloud | absolute
    encrypted: bool = False  # 是否走 AES+RSA 参数加密

    def render(self, base_url: str, **kw: object) -> str:
        path = self.path
        if self.base == "absolute":
            return path

        fields = {f for _, f, _, _ in Formatter().parse(path) if f}
        if fields:
            missing = fields - set(kw)
            if missing:
                raise KeyError(f"路由 {path} 缺少参数: {sorted(missing)}")
            path = path.format(**{k: kw[k] for k in fields})

        root = PUBLIC_BASE if self.base == "public" else base_url
        return root.rstrip("/") + "/" + path.lstrip("/")


PUBLIC: dict[str, Route] = {
    # —— 公共接口（m.boxkj.com，md5 签名）——
    "province.all":        Route("POST", "app/province/findAllProvince"),
    "school.datas":        Route("GET",  "app/sch/listSchoolDatas"),
    "school.by_province":  Route("POST", "app/sch/listSchoolByProvinceId"),
    "school.by_id":        Route("POST", "app/sch/getSchoolById"),
    "app.version":         Route("POST", "admin/studentAppUpdate/stuAppVersion2"),
    "app.log":             Route("POST", "admin/userAppLogFile/addLogData"),
    "app.upload_record":   Route("POST", "admin/analysis/addUploadRecord"),
}

TENANT: dict[str, Route] = {
    # —— 认证 ——
    "auth.token":          Route("POST", "auth/oauth/token", encrypted=False),

    # —— 用户 ——
    "user.info":           Route("GET",  "upms/sysUser/getLoginUserInfo"),
    "user.info_third":     Route("GET",  "upms/sysUser/getLoginUserInfoThird"),
    "user.is_default_pass": Route("GET", "upms/sysUser/isDefaultPass"),
    "user.add_face":       Route("POST", "upms/basUserImage/addUserFace"),
    "user.list_face":      Route("GET",  "upms/basUserImage/listBasUserImageFace"),

    # —— 学校能力 ——
    "school.appid":        Route("GET",  "upms/sysSchool/getAppid"),
    "school.socket":       Route("GET",  "upms/sysSchool/getAppSocketServer"),

    # —— 跑步（{sid} = schoolId）——
    "run.sum":             Route("GET",  "exercise/exerciseRecord/sumExerciseRecord"),
    "run.setting":         Route("GET",  "exercise/exerciseSetting/{sid}/getSetting", encrypted=True),
    "run.start_time":      Route("GET",  "exercise/exerciseSetting/{sid}/getExerciseStartTime"),
    "run.check_timeout":   Route("GET",  "exercise/exerciseSetting/checkTimeOut"),
    "run.explanation":     Route("GET",  "exercise/exerciseExplanation/getExerciseExplanationV2"),
    "run.add_record":      Route("POST", "exercise/exerciseRecord/{sid}/addExerciseRecord", encrypted=True),
    "run.list_record":     Route("GET",  "exercise/exerciseRecord/{sid}/listExerciseRecord"),
    "run.get_record":      Route("GET",  "exercise/exerciseRecord/{sid}/getExerciseRecord"),
    "run.statistics":      Route("GET",  "exercise/exerciseRecord/statisticsExerciseRecord"),
    "run.face":            Route("POST", "exercise/exerciseRunningFace/{sid}/face"),
    "run.face_verify":     Route("POST", "exercise/exerciseRunningFace/faceVer"),
    "run.feedback":        Route("POST", "exercise/exerciseFeedback/addExerciseFeedback"),

    # —— 体育课打卡 ——
    "gym.clock":           Route("POST", "app/stuGymClockRecord/addStuGymClockRecord"),

    # —— 消息 ——
    "msg.unread":          Route("GET",  "message/messageNotice/latestUnreadNotice"),
    "msg.article_list":    Route("GET",  "message/messageArticle/listMessageArticleByType"),
}

ALL: dict[str, Route] = {**PUBLIC, **TENANT}


def get(name: str) -> Route:
    """按名字取路由，未知名字直接报错（别静默返回 None）."""
    try:
        return ALL[name]
    except KeyError:
        raise KeyError(f"未知路由: {name!r}；可用: {', '.join(sorted(ALL))}") from None
