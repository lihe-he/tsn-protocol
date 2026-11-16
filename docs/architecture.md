# 架构说明

## 分层与依赖方向

```
        cli / api / web              （表现层）
                 │
              client.py              （业务编排）
              ╱        ╲
      adapters        engine         （两条正交的轴）
          │              │
        core  ←──────────┘           （kernel，谁都可以依赖）
```

**依赖方向永远是向下的。** core 不知道 adapters 存在，engine 不知道协议长什么样。

这条约束的价值：App 升级时只动 adapters，轨迹算法升级时只动 engine，
两者的测试互不影响。

---

## core

无业务语义的协议内核。

| 模块 | 职责 | 不做 |
|---|---|---|
| `routes` | 明文路由表 | 不认识字段 |
| `crypto` | AES/RSA/签名 | 不知道签名用在哪 |
| `transport` | 发请求、重试、错误映射 | 不解析业务字段 |
| `models` | pydantic 领域模型 | 不含 IO |
| `config` | 配置 | 不读业务数据 |
| `errors` | 异常体系 | —— |

**异常设计**的出发点是「能不能重试」：

```
TSNError
├── ConfigError         配置问题，重试无用
├── ProtocolError       协议不符（通常意味着要更新 adapter）
│   └── UnsupportedVersion
├── TransportError      网络问题，可重试
├── RemoteRejected      服务端业务拒绝，重试无用（remark 是线索）
│   └── AuthError
└── EngineError         本地生成失败，请求都没发出去
```

---

## adapters

一套 App 版本 = 一个 adapter。

```python
class ProtocolAdapter(ABC):
    name: str
    version_range: tuple[str, str]

    async def login(username, password) -> LoginResult
    async def fetch_app_version() -> AppVersionInfo
    async def get_setting(run_type, lng, lat) -> ExerciseSetting
    async def get_start_time(identify) -> int
    async def submit_record(draft) -> (record_id, raw)
    async def get_record(record_id) -> RunResult
    async def upload_face(image, coords, identify, face_type)
    async def list_user_faces() -> list[dict]
```

版本选择：

```python
registry.pick("2.0.26")   # → V2023Adapter
```

匹配规则是「区间包含当前版本，取上界最高者」。
装了 2.0.30 而只有 v2023(→2.0.99) 时，仍会选 v2023 并正常工作。

**加一版新协议的完整流程**：

```bash
cp -r src/tsn/adapters/v2023 src/tsn/adapters/v2026
# 改 version_range、version_code、变化的字段
# 在 adapters/__init__.py 加一行 import
```

不需要改 core / engine / client / cli。

---

## engine

与协议无关的「造数据」层。

```
path/       路线来源（history / osm）
  ↓ 一条折线
noise/      GPS 误差（AR(1)）
physics/    配速、步幅
  ↓
track.py    重采样成逐点轨迹（1 秒一个点）
  ↓
steps.py    60 秒步数分桶
  ↓
validate.py 自校验
```

### 为什么 GPS 噪声要单独成模块

因为它是最容易被识别的特征。真实 GPS 的误差在时间上相关，
用独立高斯白噪声会让轨迹出现「高频抖动」，一看就假。

AR(1) 的实现只有十几行，但它决定了轨迹的统计特征是否可信。
`test_noise.py` 里专门验证 lag-1 自相关 > 0.7。

### 为什么 validate 要独立

服务端拒绝时只给一句 remark。提交前自查能把「距离≈速度×dt」这类
明显问题在本地拦掉，省掉一轮「提交-被拒-猜原因」的循环。

它有自己的测试，且是**真正的断言**——不是永远返回通过的摆设。

---

## client

只做编排，不含算法：

```
login      → 选 adapter、拿 token
prepare    → setting → 路线 → 轨迹 → 步数 → 补步数 → 校验
execute    → 人脸 → 等待 → 提交 → 查判定
```

顺序不能乱：setting 里的 geofence 决定路线范围，路线首点决定人脸坐标。

---

## 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 支持新版 App | `adapters/` |
| 新的路线来源（别人的路线、手工坐标） | `engine/path/`，实现 `PathSource` |
| 更真实的物理模型 | `engine/physics/` |
| 更强的 GPS 模型（加高程、加建筑遮挡） | `engine/noise/` |
| Web 界面 | 新建 `services/api`，复用 `client.py` |
| 多账号并发 | 每个账号一个 `TSNClient` 实例 |
