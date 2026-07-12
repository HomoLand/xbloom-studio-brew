# xBloom Studio Brew

[English](README.md)

一个可移植的 Agent Skill：根据咖啡豆信息为 xBloom Studio 设计配方，并通过受控的本地
Bluetooth LE 操作机器。

它把离线配方知识、可选的有来源网络检索、确定性配方校验和内置 BLE 控制组合在一起，
可用于 Hermes 及其他兼容 Agent Skills 的客户端。

> [!WARNING]
> 这是非官方社区项目。BLE 协议来自逆向工程，并能控制会释放近沸水的机器。默认设备动作
> 只装载配方；远程启动需要部署级和每次冲煮两层安全确认。

## 核心能力

- 根据豆子和口味目标设计热手冲与冰美式风格闪冲配方。
- 可检索烘焙商、咖啡馆和具名咖啡从业者公开发布的方案。
- 同时列出保守的 Skill 基线和有引用的适配方案，交给用户选择。
- 写入前确定性校验粉量、比例、水量、研磨、温度、流速、RPM 和 BLE opcode。
- 支持扫描、固件探测、配方装载、遥测、取消、A/B/C 预设及受控远程启动。
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
因此联网补全需要宿主 Agent 已配置 Web 搜索工具；本 Skill 不保存搜索凭据，而且缺少网络能力
不会影响离线配方生成或本地 BLE 控制。

详见[网络补全规则](skills/xbloom-studio-brew/references/web-enrichment.md)。

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
```

可执行配方保存为本地 YAML。公开资料的引用放在回复或配套说明中，不塞进机器配方字段。

## 安全模型

- `load` 只发送受控装载帧并停在 `armed`，不会启动冲煮。
- 配方及预设写入前自动检查固件和机器状态。
- 远程启动要求部署者开关、当次物理就绪确认、相同配方哈希与机器，以及五分钟内的 armed 状态。
- 当前已验证固件为 `V12.0D.500`；其他固件必须由部署者显式接受兼容性风险。
- 所有网络来源或模型生成的配方都经过同一个校验器。

完整规则见[设备安全策略](skills/xbloom-studio-brew/references/device-safety.md)。

## 开发与测试

```text
cd skills/xbloom-studio-brew
python scripts/bootstrap.py --dev
```

当前测试结果为 112 项通过、4 项硬件/平台跳过。

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
