# tsn-protocol

运动数据服务端协议 SDK：版本适配层 + 轨迹生成引擎 + 100 个测试。

> 协议兼容性研究项目。研究 HTTP 协议逆向、加密参数还原、以及「服务端怎么校验一条运动记录」。
> 不提供任何账号、学校列表或凭据。

## 这个项目是什么

一个带服务端校验的 App，逆向时会撞上两堵墙：

1. **客户端墙**：加固、反调试、动态代码自校验 —— 运行时 hook 会被打死
2. **服务端墙**：就算 hook 成功，数据提交上去照样被拒，因为校验在云端

这个项目是撞墙之后的产物：**放弃运行时对抗，改为离线还原协议 + 在本地把数据做到自洽**。

技术上有三块：

- **协议层**：AES/RSA 参数加密、三段式签名、按 App 版本隔离的适配器
- **数据层**：GPS 误差的 AR(1) 建模、配速/步幅/步频回归、60 秒步数分桶
- **验证层**：提交前自校验（距离≈速度×dt、累计单调、步数精确守恒）

配套的 `app-recon-toolkit` 是同期沉淀的通用逆向工具链，不依赖任何具体 App。

## 这个项目解决什么问题

同类需求通常有两种做法：

| 做法 | 结果 |
|---|---|
| 在模拟器/真机上 hook App，改内存里的数字 | 数据在服务端，改本地没用；而且会被加固层反调试打死 |
| **直接构造合规记录，调服务端接口提交** | 客户端检测完全不参与；只需要把数据做"自洽" |

本项目走第二条路。App 是上传端，真正决定成败的是服务端对数据的校验，
而校验是可以用算法满足的。

## 设计原则

**1. 版本隔离**

```
core/      协议内核：路由、加密、传输、模型   ← 不含任何版本判断
adapters/  版本适配：v2023 等                ← App 升级只改这里
engine/    轨迹引擎：造数据                  ← 与协议无关
client/    业务编排
```

新增一个 App 版本支持，只需要 `cp -r adapters/v2023 adapters/v2026`，
改版本号和变化字段，import 一下自动注册。**core / engine / client 一行不用动。**

**2. 数据必须自洽**

服务端拒请求时只给一句 remark。与其提交后猜原因，不如提交前自己校验：

- 每段 `距离 ≈ 速度 × dt`（容差 0.2%）
- 累计距离单调递增
- 时间戳严格递增
- 步数桶合计 == 总步数（精确相等）
- 配速/步频落在人类范围内

`engine/validate.py` 就是干这个的，有自己的测试。

**3. 真实度 > 随机度**

伪造轨迹最容易露馅的地方不是"路线不像"，而是**统计特征不像**：

| 维度 | 劣质实现 | 本项目 |
|---|---|---|
| GPS 误差 | 无，坐标精确落在路中心 | AR(1) 马尔可夫过程，3~8m，时间相关 |
| 速度 | 均匀 + 高斯抖动 | 基础配速 + 高频抖动 + 低频间歇（过路口）+ 首尾斜坡 |
| 步频 | 固定值 | 步幅-速度回归，落在 150~190 spm |
| 停留 | 无 | 尾部静止 + 间歇降速 |

`engine/noise/gps.py` 里的 AR(1) 是关键：真实 GPS 的误差在时间上是相关的，
上一秒偏左 3 米，下一秒大概率还在左边。

## 安装

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

可选：没有历史记录时用 OSM 合成路线

```bash
pip install -e ".[osm]"
```

## 使用

```bash
# 1. 查学校
tsn schools 四川

# 2. 登录（密码交互输入，不落盘）
tsn login 西华大学

# 3. 生成一次跑步（--dry-run 只生成不提交）
tsn run --km 2.0 --type SUN --dry-run

# 4. 正式提交
tsn run --km 2.0 --type SUN

# 5. 查状态
tsn status

# 查看已注册的协议适配器
tsn adapters
```

## 作为库使用

```python
import asyncio
from tsn.client import TSNClient, RunOptions
from tsn.core.models import RunType

async def main():
    async with TSNClient() as client:
        schools = await client.find_school("西华大学")
        account, adapter = await client.login(schools[0], "学号", "密码")

        opts = RunOptions(distance_km=2.0, run_type=RunType.SUN, dry_run=True)
        draft = await client.prepare(adapter, schools[0], opts)
        print(draft.summary())

asyncio.run(main())
```

## 结构

```
src/tsn/
├── core/           协议内核
│   ├── routes.py       明文路由表
│   ├── crypto.py       AES / RSA / 三段式签名
│   ├── transport.py    HTTP + 重试 + 退避
│   ├── models.py       领域模型（pydantic）
│   ├── config.py       配置
│   └── errors.py       异常体系
├── adapters/
│   ├── base.py         ProtocolAdapter 抽象
│   ├── registry.py     注册表 + 版本选择
│   └── v2023/          当前在用的一版协议
├── engine/
│   ├── geo.py          球面距离 / 局部投影 / 点在多边形内
│   ├── noise/gps.py    AR(1) 定位误差
│   ├── physics/        配速模型、步幅模型
│   ├── steps.py        60 秒步数分桶（最大余数法）
│   ├── track.py        轨迹生成主流程
│   ├── validate.py     提交前自校验
│   ├── face.py         人脸素材扰动
│   └── path/           路线来源：history / osm
├── client.py       业务编排
└── cli/            命令行
```

## 测试

```bash
pytest -q
```

100 个测试，覆盖加密签名、分桶、噪声统计特性、轨迹自洽性、适配器版本匹配、字段构造。

重点测试：
- `test_crypto.py` —— 签名逻辑锁死，改错会立刻红
- `test_noise.py` —— 验证 AR(1) 的 lag-1 自相关 > 0.7（证明噪声是时间相关的）
- `test_track.py` —— 生成的轨迹必须过自己的校验器

## 已知限制

- **只实现了一版协议**（App 2.0.0 ~ 2.0.99）。App 升级后可能需要新增 adapter。
- **需要账号有人脸素材**（如果学校要求人脸验证），因为素材是从服务端拉账号已上传的照片。
- **路线依赖历史记录**，新账号需要 OSM 兜底（`pip install -e ".[osm]"`）。
- **进程会真实等待**：为避免 endTime 落在未来被拒，程序会按跑步时长 sleep。`--no-wait` 可以跳过，但有被拒风险。

## 免责

本项目用于协议兼容性与 HTTP 逆向的学习研究。使用者需自行确保行为符合所在学校规定与当地法规。
作者不对任何滥用后果负责。
