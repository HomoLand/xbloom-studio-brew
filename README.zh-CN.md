# xBloom Studio Brew

[English](README.md)

一个可移植的 Agent Skill：为 xBloom Studio 设计咖啡/茶配方，并通过受控的本地
Bluetooth LE 使用机器的电子秤、研磨器、冲煮器和自动配方能力。

它把离线配方知识、可选的有来源网络检索、确定性配方校验和内置 BLE 控制组合在一起，
可用于 Hermes 及其他兼容 Agent Skills 的客户端。

> [!WARNING]
> 这是非官方社区项目。BLE 协议来自逆向工程，能控制电机以及释放近沸水的机器。配方装载、
> 称重与物理启动互相分离；研磨和热水动作需要部署级与当次操作两层安全确认。

## 核心能力

- 根据豆子和口味目标设计热手冲与冰美式风格闪冲配方。
- 可检索烘焙商、咖啡馆和具名咖啡从业者公开发布的方案。
- 将精确匹配的 xPod/NFC Recipe Card 作为高价值的一方参考，但会明确适配 xPod 与 Omni
  不同的冲煮结构，不会原样套用。
- 内置 xBloom 公布的五套 Omni Tea Brewer 茶配方，并使用专用茶协议和校验器。
- 同时列出保守的 Skill 基线和有引用的适配方案，交给用户选择。
- 写入前确定性校验粉量、萃取/成品比例、bypass、水量、研磨、温度模式、流速、RPM 和 BLE opcode。
- 支持扫描、固件探测、配方装载、遥测、取消、A/B/C 预设及受控远程启动。
- 支持带明确“进入即归零”语义的 FreeSolo 电子秤、独立研磨，以及水箱/直供水源下 RT 或
  40-98 C 的定量出水。
- 诊断时可读取已隐藏序列号的 Studio 机器信息和设置快照。
- 本地运行，不需要 xBloom 云端凭据或 App 账号。

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

## 使用示例

```text
用 xbloom-studio-brew 给这包豆子做一套花果调清晰的热冲方案。
搜索这支豆子的可靠公开配方，列出几个来源让我选，再生成 xBloom 配方。
做一套冰美式风格闪冲，校验后装载到机器，但不要启动。
使用官方绿茶模板，但只装载不要启动。
帮我称这个空杯：进入称重前保持秤盘为空，显示 ready 后提醒我再放杯子。
```

可执行配方保存为本地 YAML。公开资料的引用放在回复或配套说明中，不塞进机器配方字段。

基础命令示例：

```text
python scripts/xbloom.py scale --duration 30
python scripts/xbloom.py tea-validate assets/tea-green-official.yaml
python scripts/xbloom.py tea-load assets/tea-green-official.yaml
```

进入称重模式会自动把当时已有负载设为零。测物体绝对重量时先保持秤盘为空，收到 `ready`
后再放物体；测杯中净重时则可以预先放好空杯。`--tare` 是进入后的额外再次去皮，并不能
关闭首次自动归零。FreeSolo 室温出水使用 `water --temp RT`，仍受物理出水门禁保护；
`--water-source auto` 会沿用机器当前的水箱/直供设置，无法读取时必须显式指定。

`grind`、`water`、咖啡 `start` 和 `tea-start` 已内置，但在部署者启用对应安全开关前保持
禁用。详见[独立工具说明](skills/xbloom-studio-brew/references/standalone-tools.md)与
[茶冲煮说明](skills/xbloom-studio-brew/references/tea-brewing.md)。

## 安全模型

- `load` 只发送受控装载帧并停在 `armed`，不会启动冲煮。
- `tea-load` 只上传专用茶配方，不会执行；`scale` 会报告自动归零基线，并在结束或中断时退出称重模式。
- 配方及预设写入前自动检查固件和机器状态。
- 远程启动要求部署者开关、当次物理就绪确认、相同配方哈希与机器，以及五分钟内的 armed 状态。
- 当前已验证固件为 `V12.0D.500`；其他固件必须由部署者显式接受兼容性风险。
- 所有网络来源或模型生成的配方都经过同一个校验器。
- 独立研磨单次最多 30 秒，并持久化 60 秒休息锁；普通中断时仍会尝试发送停止和退出。

完整规则见[设备安全策略](skills/xbloom-studio-brew/references/device-safety.md)。

## APK 能力覆盖

官方 Android App 不只有 Studio BLE：还包含云账号、NFC 查询、商城内容、高风险维护功能，
以及 xBloom Original（`J20`）的路径。[APK 逐项能力矩阵](skills/xbloom-studio-brew/references/apk-capability-matrix.md)
明确区分了已经支持、需要常驻 BLE bridge、刻意不开放，以及本就不属于 Studio 设备控制的能力。

## 开发与测试

```text
cd skills/xbloom-studio-brew
python scripts/bootstrap.py --dev
```

当前测试结果为 168 项通过、4 项硬件/平台跳过。发布测试不会启动研磨器或释放热水；
电子秤进入、读数和退出已在固件 `V12.0D.500` 上完成真机验证。

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
