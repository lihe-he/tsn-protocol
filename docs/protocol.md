# 协议参考

记录当前已还原的协议细节。这是项目的核心知识资产 —— 代码可以重写，这些细节要保留。

> 适用 App 版本：2.0.0 ~ 2.0.99（代号 v2023）
> 学校类型：公版（`sysType=2`）

---

## 1. 服务端

| 用途 | 域名 |
|---|---|
| 公共接口（学校列表、版本检查） | `https://m.boxkj.com` |
| 租户接口（认证、跑步、人脸） | `https://a.tsnkj.com` 或学校自建域名 |
| H5 页面 | `https://h.tsnkj.com` |

自建域名的学校会覆盖租户 base url（`school.lan_url` 优先）。

---

## 2. 两套签名

### 2.1 公共接口（md5 排序）

```
params += {appId: "move", appSecret: "<secret>"}
拼接 = "&".join(f"{k}={v}" for k in sorted(params))
sign = md5(拼接)
```

请求头：

```
Host: m.boxkj.com
channel: Android
version: 20230
type: 0
Content-Type: application/x-www-form-urlencoded
user-agent: okhttp/4.9.0
```

### 2.2 租户接口（三段式）

从 token 派生一对 key/iv：

```
digest = md5(token)          # 32 位 hex
key    = digest[0:16]
iv     = digest[16:32]
```

然后：

```
cbc(x) = base64(AES-CBC(x, key, iv))

appId_mix     = md5(cbc(tenant_appid + token + timestamp))
appSecret_mix = md5(cbc(token + timestamp))

authInfo = "&".join(f"{k}={v}" for k in sorted(params + {appId, appSecret}))
sign     = md5(cbc(url_unquote(authInfo)))
```

请求头：

```
model: {brand}-{model}
uniqueCode: md5(设备号)
school: {schoolCode}
Cookie: host={schoolCode}
sign: {sign}
timestamp: {毫秒}
Authorization: Bearer {token}
user-agent: okhttp/4.9.0
```

---

## 3. 参数加密（加密接口）

`getSetting` / `addExerciseRecord` 这类接口，参数整体加密：

```
session_key = md5(token + timestamp)[0:16]        # 每次请求都不同
v           = AES-ECB(appSignHash, session_key)   # App 身份证明
body        = AES-ECB(json.dumps(sorted_params))
key_field   = RSA-PKCS1v15(session_key)
```

请求体：`{key: key_field, param: body}`

- `addExerciseRecord`：POST，响应是普通 JSON
- `getSetting`：GET，**响应也是密文**，用同一个 session_key 做 AES-ECB 解密

`appSignHash` 由 APK 签名证书派生：

```
inner = ":".join(f"{b:02X}" for b in sha256(cert_der))
hash  = base64(sha256(inner))
```

当前值：`d2tnIyqximO/L8Y4MzfRELa1hSAtSRxzmvXlcOCzRyk=`

---

## 4. 登录

```
POST {cloud}/auth/oauth/token?username=&password=&grant_type=password&type=app&appType=stuApp
Authorization: Basic base64("{schoolCode}:pig")
```

密码加密：AES-CBC，key = iv = `"thanks,pig4cloud"`，**zero padding**（不是 PKCS7）。

响应：`access_token` / `refresh_token` / `expires_in` / `user_id`

刷新：`grant_type=refresh_token&refresh_token=...&scope=server`

---

## 5. 跑步规则（getSetting 下发）

| 字段 | 含义 |
|---|---|
| `identify` | 本次跑步标识，所有接口都要带 |
| `totalRange` | 要求距离（公里） |
| `startQualifiedTime` | 合格用时下限（分钟） |
| `endQualifiedTime` | 合格用时上限（分钟），`-1` 表示不限 |
| `step` | 要求步数 |
| `isStepRecursion` | 超距时步数是否按比例放大 |
| `geofence` | 地理围栏多边形 |
| `list` / `campusList` | 打卡点 |
| `isStartFace` / `isMidwayFace` / `isEndFace` | 人脸验证策略 |
| `middleFaces` | 中途人脸位置比例 |
| `limitSpeed` / `endStride` / `endLimitStepFrequency` | 限速/步幅/步频 |
| `userOpenDevelop` | 开发者模式标记 |

**合格条件**：

```
startQualifiedTime*60 <= 用时 <= endQualifiedTime*60
距离 >= totalRange
步数 >= step（isStepRecursion=1 且超距时按距离比例放大）
```

---

## 6. 提交记录（addExerciseRecord）

关键字段：

```
sportType        2=晨跑 / 1=阳光跑 / 0=自由跑
startTime        起跑毫秒时间戳
endTime          结束毫秒时间戳
sportTime        总用时（秒）
sportRange       距离（公里，两位小数字符串）
speed            配速，格式 5'12"
avgSpeed         平均速度 km/h
gitudeLatitude   轨迹点 JSON 数组 [{t, time, latitude, longitude}]
stepNumbers      60 秒步数桶 JSON 数组
pointList        打卡点
okPointList      已通过打卡点 [{latitude, longitude, content, id, time}]
isFaceStatus     人脸上传状态
identify         跑步标识
geofence         围栏（原样回传）
limitSpeed/limitStride/limitStepFrequency   原样回传
gpsDistance
d/f/m/h          风险标记计数（正常全 0）
isValid          客户端算的合格性
environment      设备环境上报（JSON 字符串）
```

提交后调 `getExerciseRecord`，`sportStatus == 1` 才算成功，否则 `remark` 是原因。

---

## 7. 设备环境上报（environment）

```json
{
  "deviceId": "...",
  "oppo": "{\"auth\":\"success\",\"root\":{\"result\":\"false\"},...,\"checksum\":\"md5\"}",
  "safe": "{\"sign\":\"true\",\"root\":\"false\",\"emulator\":\"false\",...,\"supported_abis\":[\"arm64-v8a\"]}"
}
```

`oppo` 里每个字段是 `{result: "false", detail: [...]}`，
带 `nonce` + `timestamp` + `checksum`（md5，checksum 自身不参与计算）。

字段清单：`root / selinux / xposed / proxy / vpn / separation / emulator / ptrace / frida / hook / breakpoint`

---

## 8. 人脸

```
POST {cloud}/exercise/exerciseRunningFace/{schoolId}/face
Content-Type: multipart/form-data

key   = RSA(AES key)
param = AES-ECB(排序参数 JSON)
file  = 图片二进制
```

参数含：`identify` / `type`（1 开始, 2 中途, 3 结束）/ `runType` / `coordinates` / `timeStamp` / `exception` / `environment`

素材来源：`/upms/basUserImage/listBasUserImageFace?basUserId={uid}`，
返回的 `imageRouteUrl` 就是账号自己上传过的照片。

---

## 9. 待验证

- [ ] App 2.0.24+ 是否改了字段（当前实现按 2.0.23）
- [ ] `isOpenEncry=1` 的学校（加密模式）是否有额外参数
- [ ] 私版（`sysType=1`）接口：`app/sportRecordSetting/getSetting`、`app/appSportRecord/appAddSportRecord`
- [ ] 服务端是否有「路线与围栏匹配度」校验（目前只看到距离/时间/步数）
- [ ] `remark` 的全部取值及其含义
