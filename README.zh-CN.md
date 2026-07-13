# xBloom Studio Brew

[English](README.md)

[![validate-skill](https://github.com/HomoLand/xbloom-studio-brew/actions/workflows/test.yml/badge.svg)](https://github.com/HomoLand/xbloom-studio-brew/actions/workflows/test.yml)
[![GitHub Release](https://img.shields.io/github/v/release/HomoLand/xbloom-studio-brew)](https://github.com/HomoLand/xbloom-studio-brew/releases)

一个可移植的 Agent Skill：为 xBloom Studio 设计咖啡/茶配方，并通过受控的本地
Bluetooth LE 使用机器的电子秤、研磨器、冲煮器和自动配方能力。

它把离线配方知识、可选的有来源网络检索、私有 App 可见配方目录、确定性配方校验和内置
BLE 控制组合在一起，可用于 Hermes 及其他兼容 Agent Skills 的客户端。

> [!WARNING]
> 这是非官方社区项目。BLE 协议来自逆向工程，能控制电机以及释放近沸水的机器。配方装载、
> 称重与物理启动互相分离；研磨和热水动作需要部署级与当次操作两层安全确认。

## 核心能力

- 根据豆子和口味目标设计热手冲与冰美式风格闪冲配方。
- 可检索烘焙商、咖啡馆和具名咖啡从业者公开发布的方案。
- 将精确匹配的 xPod/NFC Recipe Card 作为高价值的一方参考，但会明确适配 xPod 与 Omni
  不同的冲煮结构，不会原样套用。
- 内置 xBloom 公布的五套 Omni Tea Brewer 茶配方，并使用专用茶协议和校验器；账号同步还可
  导入所在地区当前返回的茶方案。
- 可将获得授权的 xBloom App/API 或已解码 MMKV JSON 导入私有咖啡/茶目录；可选只读同步
  覆盖本人账号可见的官方、自建、Product/xPod 与共享列表。
- 可把本地咖啡/茶配方预览为 App 精确表单，并在显式确认后以幂等、只新增方式同步到账号；
  凭据和会话令牌都不会落盘。
- 同时列出保守的 Skill 基线和有引用的适配方案，交给用户选择。
- 写入前确定性校验粉量、萃取/成品比例、bypass、水量、研磨、温度模式、水型、四态振动时机、
  流速、RPM 和 BLE opcode。
- 支持扫描、固件探测、配方装载、遥测、取消、A/B/C 预设及受控远程启动。
- 支持带明确“进入即归零”语义的 FreeSolo 电子秤、独立研磨，以及水箱/直供水源下 RT 或
  40-98 C 的定量出水。
- 内置仅监听本机回环地址的常驻 BLE bridge，可串行处理咖啡、茶、电子秤、研磨、出水、
  A/B/C 预设、持久设置与机械调校，并提供状态快照和有界事件遥测。
- 分开呈现配方目标水量、机器本次累计出水和杯秤净增量，不会把它们误称为供水余量。
- 可读取已隐藏序列号的机器信息、持久设置与机械调校，并为单位、屏幕、水源、注水半径和
  振动幅度提供独立门禁、读回校验和回滚。
- 配方设计、目录导入/查询和 BLE 能力都可完全本地运行；可选账号同步/新增使用临时凭据，
  凭据和原始会话都不会写入目录。

## 配方如何生成

```text
豆子信息
  -> Skill 内置配方知识
  -> 可选的第一方网络资料
  -> Agent 推理与用户选择
  -> 受控 YAML 校验器
  -> 本地 BLE 控制器
```

网络补全以证据为先：原烘焙商或作者发布的 xBloom 原生配方优先于人工手冲指南；平底滤杯
方案会明确适配，锥形滤杯方案只作为风味与方法参考。输出必须显示来源、原始器具、匹配度、
适配内容和置信度。没有可靠来源或网络工具时，回退到内置离线模型。
原生 xPod 配方会保留烘焙商意图，但不会被默认为 Omni 原生配方。
因此联网补全需要宿主 Agent 已配置 Web 搜索工具；本 Skill 不保存搜索凭据，而且缺少网络能力
不会影响离线配方生成或本地 BLE 控制。

详见[网络补全规则](skills/xbloom-studio-brew/references/web-enrichment.md)。

Hermes 可用下面的无密钥搜索后端；若网关已运行，配置后重启网关：

```text
hermes config set web.search_backend ddgs
```

端到端验证命令见[部署说明](skills/xbloom-studio-brew/references/deployment.md)。

## 私有配方目录

APK 本身没有内置一份全球静态配方库，而是按地区、账号和设备拉取并缓存记录。因此本项目
可以收集获得授权的 JSON/缓存导出中全部配方，或用户本人账号与地区当时返回的官方、
自建、Product/xPod 与 Shared 记录；不能把它说成全球所有私有或历史 xBloom 配方。

```text
python scripts/xbloom.py catalog status
python scripts/xbloom.py catalog import-json app-response.json
python scripts/xbloom.py catalog import-mmkv decoded-mmkv.json
python scripts/xbloom.py catalog list --kind coffee --executable
python scripts/xbloom.py catalog list --kind tea
python scripts/xbloom.py catalog export <id> recipe.yaml
python scripts/xbloom.py catalog login-sync --region china --language zh-cn
python scripts/xbloom.py catalog push recipe.yaml --region china
```

目录默认保存在安装目录外的 `~/.xbloom-studio-brew/catalog/catalog.json`，不会保留原始
响应或凭据。xPod 与 J20 记录保持只读参考；通过校验的 Studio 咖啡和茶分别导出为受控 YAML。
临时登录、默认五类读取以及两次经所有者明确批准的只新增写入，已于 2026-07-14 在中国区
现网验证，包括云端回读与零写入幂等复验；凭据和会话只存在于进程内。宿主通过
`XBLOOM_ACCOUNT_EMAIL` 与 `XBLOOM_ACCOUNT_PASSWORD` 提供账号，密码不接受命令行参数。
`catalog push` 默认只预览；只有同时给出 `--apply` 与
`--confirm-write own-account-cloud-recipe` 才会写远端，发布测试从不调用现网写接口。详见
[目录与 A/B/C 说明](skills/xbloom-studio-brew/references/catalog.md)。

## 安装

### Hermes

```text
hermes skills install HomoLand/xbloom-studio-brew/skills/xbloom-studio-brew
```

### 其他 Agent Skills 客户端

将 `skills/xbloom-studio-brew/` 安装或复制到客户端的 Skills 目录，并保持目录名不变。

### 初始化本地 BLE 环境

在安装后的 Skill 目录运行：

```text
python scripts/bootstrap.py
python scripts/xbloom.py doctor --scan
python scripts/xbloom.py probe
```

蓝牙命令必须运行在咖啡机附近的本地电脑上；云端沙箱无法直接访问家里的 BLE 适配器。
内置 bridge 也只运行在该主机，不作为局域网服务暴露。
初始化产生的虚拟环境默认放在 Skill 外部的 `~/.xbloom-studio-brew/runtime`，因此只读的
Agent 缓存和 Skill 升级不会破坏运行时。

## 使用示例

```text
用 xbloom-studio-brew 给这包豆子做一套花果调清晰的热冲方案。
搜索这支豆子的可靠公开配方，列出几个来源让我选，再生成 xBloom 配方。
导入我授权提供的 xBloom 配方 JSON，列出 Studio 咖啡和茶方案，并导出其中一份。
做一套冰美式风格闪冲，校验后装载到机器，但不要启动。
使用官方绿茶模板，但只装载不要启动。
预览这份本地配方同步到我的 xBloom 账号会写什么，但先不要上传。
帮我称这个空杯：进入称重前保持秤盘为空，显示 ready 后提醒我再放杯子。
```

可执行配方保存为本地 YAML。公开资料的引用放在回复或配套说明中，不塞进机器配方字段。

基础命令示例：

```text
python scripts/xbloom.py scale --duration 30
python scripts/xbloom.py settings
python scripts/xbloom.py advanced
python scripts/xbloom.py catalog status
python scripts/xbloom.py tea-validate assets/tea-green-official.yaml
python scripts/xbloom.py tea-load assets/tea-green-official.yaml
python scripts/xbloom.py bridge start
python scripts/xbloom.py bridge status
python scripts/xbloom.py bridge scale-start --duration 90
python scripts/xbloom.py bridge tea-load assets/tea-green-official.yaml
python scripts/xbloom.py bridge stop
```

茶水量分两层：每段 80/90 ml 是可编程的壶内注水量，App 的
`约 120 / 240 / 360 ml` 是虹吸完成后的估算成品量。浸泡结束后机器进入固件负责的收尾阶段，
遥测名称虽为 `bypass`，却不是咖啡配方可配置的 bypass，也不能把它编码成用户可控的额外
30 ml 注水。

进入称重模式会自动把当时已有负载设为零。测物体绝对重量时先保持秤盘为空，收到 `ready`
后再放物体；测杯中净重时则可以预先放好空杯。`--tare` 是进入后的额外再次去皮，并不能
关闭首次自动归零。FreeSolo 室温出水使用 `water --temp RT`，仍受物理出水门禁保护；
`--water-source auto` 会沿用机器当前的水箱/直供设置，无法读取时必须显式指定。

`grind`、`water`、咖啡 `start`、`tea-start` 和单连接 `tea-brew` 已内置，但在部署者启用
对应安全开关前保持
禁用。详见[独立工具说明](skills/xbloom-studio-brew/references/standalone-tools.md)与
[茶冲煮说明](skills/xbloom-studio-brew/references/tea-brewing.md)。

设备操作优先使用常驻 bridge；它覆盖咖啡、茶、称重、研磨、FreeSolo 出水、预设、设置与
调校。bridge 运行期间，一次性 BLE 命令会拒绝抢占连接。FreeSolo 运行中温度目标和水型切换
也已按协议实现，并受独立部署者门禁
保护；固件 `V12.0D.500` 上运行中从 `center` 切到 `spiral` 已完成真机验证，运行中调温仍待
温度计测量；调温指令编码与 BLE 写入路径本身已经验证。它们不能运行中修改总水量/流速，
也不会改写咖啡配方步骤。

A/B/C 写入是一次原子化的三配方操作，应先对每份输入运行
`validate <recipe.yaml> --slot`。AUTO 槽位保存注水、研磨、比例与称重开关，实际使用时由机器
测量粉量。它不能表示冲煮后的 bypass，也不能存茶配方，因此从 CLI、bridge 到底层帧生成器
都会拒绝旁路配方，绝不会静默丢水。写入时会临时切到 PRO，依次写 A/B/C，确认保存后回到
AUTO，全程不会启动冲煮；可用 `--scale on off on` 按 A/B/C 顺序设置三个槽位的冲煮称重开关。

## 安全模型

- `load` 只发送受控装载帧并停在 `armed`，不会启动冲煮。
- `tea-load` 只上传专用茶配方，不会执行；`scale` 会报告自动归零基线，并在结束或中断时退出称重模式。
- 配方及预设写入前自动检查固件和机器状态。
- 远程启动要求部署者开关、当次物理就绪确认、相同配方哈希与机器，以及五分钟内的 armed 状态。
- 冲煮遥测默认聚合为每秒一条；只有收到机器终态才清除工作流记录。监控超时会保留恢复状态，
  后续 `monitor` 或 `cancel` 直接复用已记录的机器，不会重新扫描。
- 当前已验证固件为 `V12.0D.500`；其他固件必须由部署者显式接受兼容性风险。
- 所有网络来源或模型生成的配方都经过同一个校验器。
- 独立研磨单次最多 30 秒，并持久化 60 秒休息锁；普通中断时仍会尝试发送停止和退出。
- 常驻 bridge 只绑定回环地址，以随机令牌验证本机请求，只持有一个 BLE 连接并串行写入；
  单独启动 bridge 不会扫描、连接、研磨或出水。
- 交互研磨在 ACK 丢失时会保守执行 STOP/QUIT；交互出水带主机侧安全超时，且只有水量遥测
  峰值落在目标容差内才会报告自然完成。显式 STOP 回显与自然完成报告会分别判断。
- 持久设置写入使用独立的部署者和当次操作门禁，要求机器空闲且固件受支持，必须精确读回，
  失败时尝试恢复基线；这些指令已有测试覆盖，但本项目尚未在真机上实际改写设置。

完整规则见[设备安全策略](skills/xbloom-studio-brew/references/device-safety.md)。

## APK 能力覆盖

官方 Android App 不只有 Studio BLE：还包含云账号、NFC 查询、商城内容、高风险维护功能，
以及 xBloom Original（`J20`）的路径。[APK 逐项能力矩阵](skills/xbloom-studio-brew/references/apk-capability-matrix.md)
明确区分了直接支持或通过内置 bridge 支持、仍待真机验证、刻意不开放，以及本就不属于
Studio 设备控制的能力。

## 开发与测试

```text
cd skills/xbloom-studio-brew
python scripts/bootstrap.py --dev
```

发布测试仅使用脚本化 BLE，不会启动研磨器或出水。电子秤进入/读数/退出以及 FreeSolo
运行中水型切换在固件 `V12.0D.500` 上有独立的受控真机证据；各能力的具体证据等级见矩阵，
后续受控测试见[实机验证清单](skills/xbloom-studio-brew/references/hardware-validation.md)。

## 致谢

衷心感谢两个上游项目，本 Skill 正是建立在它们的开源工作之上：

- [ryunana/xbloom-studio-recipe-skill](https://github.com/ryunana/xbloom-studio-recipe-skill)：
  提供配方工程基础、豆子类型、调参逻辑与 C40 换算。
- [Janczykkkko/xbloom-ble](https://github.com/Janczykkkko/xbloom-ble)：
  提供逆向得到的 xBloom Studio BLE 协议、客户端、遥测解析和协议测试。

两者均依照 MIT 许可引用或改编。固定上游 commit、修改说明、版权归属和完整许可文本见
[第三方通知](skills/xbloom-studio-brew/THIRD_PARTY_NOTICES.md)。

## 许可

MIT。xBloom 和 xBloom Studio 是其权利人的商标。本项目与 xBloom 无关联，也未获其背书。
