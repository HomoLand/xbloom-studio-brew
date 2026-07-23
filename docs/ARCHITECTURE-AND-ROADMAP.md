# xBloom Studio 平台：需求书、架构设计与开发路线

> 状态：已确认 v1.0（开发基线）；实现进度跟踪见 §5
> 最后更新：2026-07-23
> 适用范围：`xbloom-studio-brew`（core + skill）与 `xbloom-studio-web`（backend + frontend）两个仓库
> 当前发布：GitHub Release **v1.3.0**（core wheel + knowledge zip + skill zip）

---

## 0. 文档说明

本文件是动手实现前的基线，统一三件事：**需求书**（做什么、为谁做）、**架构设计**（怎么拆模块、数据怎么流）、**开发路线**（分几期、每期做什么、怎么算完成）。

约定：
- 面向用户（唯一开发者/使用者）书写，默认中文。
- 代码级文档（`SKILL.md`、`references/`）保持英文，供 Agent 消费；本文件是规划文档，不进 Agent 上下文。
- 文中 `FR` = 功能需求，`NFR` = 非功能需求，`ADR` = 架构决策记录。

---

## 1. 背景与目标

### 1.1 现状

- **core**（`packages/core`）：BLE 协议、配方校验、私有目录、历史、路径工具，以及 bridge 守护进程（单实例 BLE owner + loopback RPC）。目标形态是可独立安装的 Python 包，并提供自己的 bridge 可执行入口。
- **skill**（`skills/xbloom-studio-brew`）：`SKILL.md` + `references/`（配方设计知识）+ `scripts/xbloom.py`（CLI）+ `assets/`（模板）。依赖 core。
- **web**（`xbloom-studio-web`）：`backend`（FastAPI HTTP + MCP server + 拉起 bridge）+ `frontend`（React SPA）。依赖 core。
- bridge 已改为独立进程，与 backend 解耦，后端重启不中断冲煮。

### 1.2 两个核心需求

1. **Agent 路径**：Hermes 这类 Agent 用打包好的 Skill，根据用户输入的豆子**编撰配方 → 冲煮 → 监控进度**。Hermes 以 **Agent Skill 形态**消费（读 `SKILL.md`，把 CLI 当 shell 命令跑）。
2. **人类路径**：人类通过 **WebUI 获得类官方 App 的体验**。核心交互是**拍照识豆**（豆袋封面 + 可能的官方冲配单）或**文字提问**（"某某豆怎么冲"）→ AI 生成配方 → 一键冲煮 → 实时监控；以及浏览/操作现成配方。

### 1.3 目标

- Skill 足够标准化、自包含，任何 Agent Skill host 装上即用。
- WebUI 提供拍照→配方→冲煮的顺滑体验，等价"App + AI"；桌面默认仅本机访问，移动端通过显式开启的配对 LAN 模式访问。
- 两条路径**共用一份版本化配方设计知识和一套操作层**，杜绝逻辑复制与漂移。
- core、knowledge、Skill 与 Web 通过版本化产物衔接；发布安装不依赖 sibling checkout 或仓库相对路径。
- bridge 进程常驻；BLE 连接按需建立、在一次硬件工作流内复用，并在工作流结束后立即释放给手机等其他客户端。
- Web、Skill、MCP 共享事务化 catalog/历史与同一个活动 workflow，跨进程写入不丢数据。
- 客户端退出、backend 重启或轮询停止都不影响 bridge 独立监控终态、记录历史和释放 BLE。

---

## 2. 需求书

### 2.1 用户角色

| 角色 | 描述 | 主要入口 |
|---|---|---|
| Agent（Hermes 等） | 读 SKILL.md、跑 CLI 的自动化 agent | Skill CLI |
| 人类（浏览器） | 拍照/提问、浏览、冲煮、看进度 | WebUI |
| 人类（终端，次要） | 直接跑 CLI 调试/高级操作 | Skill CLI |
| MCP host（可选） | Cursor/Claude Desktop 等 | web 的 MCP server |

### 2.2 功能需求

#### Agent 路径（Need 1）

- **FR-A1** Agent 能读取 Skill 的设计知识（`SKILL.md` + `references/`），据用户描述的豆子产出合法 xBloom 配方 YAML。
- **FR-A2** Agent 能用 CLI 校验配方（`validate` / `tea-validate`）。
- **FR-A3** Agent 能用 CLI 加载、冲煮（含安全确认短语）、暂停/恢复/取消。
- **FR-A4** Agent 能用 CLI 轮询实时遥测/事件监控进度。
- **FR-A5** Agent 产出的配方可存入共享 catalog，供人类在 WebUI 复用。
- **FR-A6** Skill 自包含：一次安装即含设计知识 + CLI + 模板，无需额外拷贝。
- **FR-A7** 首个需要机器的 Skill 命令能发现或安全启动兼容 bridge；后续命令复用同一 workflow 与 BLE 连接。

#### 人类路径（Need 2）

- **FR-H1** WebUI 支持桌面上传或手机拍摄豆袋照片（含可选官方冲配单），后端用 LLM 提取豆信息与官方参数。
- **FR-H2** WebUI 支持文字提问（"某豆怎么冲"），后端用 LLM 生成配方。
- **FR-H3** 生成的配方经 schema + core 校验，展示参数（剂量/注水/温度/pours）、evidence 与简短设计依据。
- **FR-H4** 用户可将生成配方存入 catalog、编辑、或一键冲煮。
- **FR-H5** WebUI 能浏览模板与私有 catalog（含 Agent 存入的配方）。
- **FR-H6** WebUI 能加载/开始/暂停/恢复/停止冲煮，带安全确认短语。
- **FR-H7** WebUI 实时展示遥测（活动/阶段/水量/杯重/进度）与事件流。
- **FR-H8** WebUI 展示历史记录。
- **FR-H9** 手机访问采用显式开启的 LAN 模式：首次配对后才能调用 API；默认 loopback 模式不暴露到局域网。

#### 共享/衔接

- **FR-S1** Agent 与 WebUI 共用同一 bridge 守护进程与同一 catalog/历史。
- **FR-S2** 配方设计知识只有一份人工维护来源；Skill 直接携带，Web 使用同源构建的版本化 knowledge bundle。
- **FR-S3** 每次 `load` 生成不可变 recipe snapshot 与唯一 `workflow_id`；后续状态变更绑定该 workflow，避免旧客户端误操作新任务。
- **FR-S4** 所有客户端可观察活动 workflow；除紧急停止外，变更命令须携带匹配的 `workflow_id` 与幂等 request ID。
- **FR-S5** bridge 自主处理机器事件和确认终态，不依赖 Web/Skill 持续在线或继续轮询。

### 2.3 非功能需求

- **NFR-安全**：涉及加热/电机的动作遵循既有安全模型（owner gate + 每次调用确认短语 + 固件校验）。这套逻辑只在 bridge 中实现，所有接口透传，不各自造；控制结果不确定时禁止自动重试。
- **NFR-隐私**：WebUI 的 AI 设计需要 LLM。用户已确认接受**公网 LLM API**（豆信息及所选图片会发给配置的模型服务）；界面须明确告知数据去向，模型端点保持可配置，并提供可选的"仅文字"路径。原始图片默认仅用于当次请求，不进入 catalog/历史。
- **NFR-性能/连接**：不需要硬件的操作不连接 BLE；一次冲配从加载、开始、暂停/恢复到监控结束只建立一次连接；确认完成、取消或停止后立即释放 BLE。首次操作建联延迟可接受（秒级），空闲超时仅作为异常遗留连接的兜底。
- **NFR-单实例**：同一 OS 用户与状态根目录只允许一个 bridge 实例；daemon 使用生命周期级系统锁保证原子单实例，客户端请求在实例内串行化。项目当前明确只支持一个 xBloom Studio 设备。
- **NFR-一致性**：catalog、历史、workflow 与幂等请求使用 SQLite/WAL 事务存储；多进程并发写不丢更新，schema 支持迁移和备份。
- **NFR-恢复**：backend/Skill 退出不影响活动工作流；bridge 或 BLE 异常后从持久状态恢复，只查询和对账机器状态，不重复发送可能已生效的启动命令。
- **NFR-兼容**：bridge 暴露实例 ID、core 版本、RPC 协议版本与配置指纹；客户端连接前校验兼容范围，不兼容时仅可在 bridge 空闲后执行受控升级/重启。
- **NFR-可移植**：状态目录、runtime 与开发期资源路径可配置；发布运行依赖版本化 core/knowledge 产物，不硬编码仓库布局。
- **NFR-网络**：默认 loopback 模式只监听 `127.0.0.1`；显式 LAN 模式需要配对认证、受限 CORS 与会话过期，不提供公网暴露或端口映射能力。
- **NFR-可观测**：状态与日志可解释当前 workflow、连接作用域、最近终态/断连原因、版本与恢复状态；日志轮转且不记录密钥、确认短语或原始图片。

---

## 3. 架构设计

### 3.1 核心洞察：推荐智能 vs 确定性边界

配方**推荐**（豆子信息→参数建议）需要模型结合 `SKILL.md` + `references/` 中的知识推理。Hermes 使用宿主模型；Web backend 调用配置的 LLM。两边消费同一版本的 knowledge，但不共享模型会话。

推荐结果之外仍有明确的确定性边界：输入提取、单位归一、结构化 schema、配方校验、catalog revision、workflow snapshot、安全门和 BLE 操作都由代码实现。模型永远不能绕过 core 校验直接控制机器。

```
Knowledge（单一来源、版本化）
  ├─ Hermes 宿主模型 ─┐
  └─ Web LLM adapter ─┴→ structured recipe candidate
                              ↓ schema + core validation
                         immutable recipe snapshot
                              ↓
Core + Bridge：加载 / 冲煮 / 监控 / 终态 / 历史 / BLE 释放
```

**同一份设计知识，两种模型入口；同一套确定性边界，三个操作接口。**

### 3.2 模块与发布边界

```
xbloom-studio-brew（源码与发布源）
  packages/core/                  xbloom-studio-core wheel
    xbloom_ble/                   protocol/client/telemetry/bridge
    recipe/safety/storage/paths   确定性领域逻辑
    console entry                xbloom-bridge

  skills/xbloom-studio-brew/
    SKILL.md + references/        knowledge 唯一人工维护来源
    assets/                       模板
    scripts/xbloom.py             Agent/终端 CLI 薄适配

  release build
    ├─ self-contained Skill bundle
    └─ versioned knowledge bundle + manifest/hash

xbloom-studio-web（独立应用）
  backend/
    HTTP + MCP adapters
    design/                       provider adapter + schema + validation
    auth/                         loopback/LAN 配对会话
  frontend/                       React 人类体验
  pins: core version + knowledge version

共享运行时状态
  state.db (SQLite/WAL)           catalog/history/workflow/idempotency
  bridge.json + daemon lock       本机发现、认证与原子单实例
```

**职责边界**：
- **Knowledge 放**：配方设计原则、schema 说明、茶/咖啡知识和模板；人工只维护一份，构建时生成带版本与 hash 的发布包。
- **Skill 放**：Agent 使用说明与 CLI；所有连接型命令调用 bridge RPC，不拥有 BLE 或复制安全逻辑。
- **Web 放**：人类界面、图片/文字输入、LLM provider 编排、HTTP/MCP 和 LAN 配对认证。
- **Core 放**：协议、配方对象与校验、安全、事务存储 API、workflow 状态机和 bridge daemon。
- **Bridge 独占**：BLE 连接、机器事件、物理动作、终态确认和 workflow 恢复；客户端退出不转移这些职责。

发布环境中 Skill 与 Web 都安装固定版本的 core；Web 另外安装匹配的 knowledge bundle。`XBLOOM_REFERENCES_DIR` / `XBLOOM_ASSETS_DIR` 只作为开发覆盖或显式调试入口，不是正式发布依赖。skill 未安装时，只要 core 与 knowledge 产物存在，Web 的设计与操作功能均可工作。

### 3.3 关键数据流

**场景 A：Agent 设计并冲煮**

```
用户在对话描述豆子
  → Agent 读 SKILL.md + references/ → 生成配方 YAML
  → CLI validate 校验
  →（可选）CLI catalog create → 生成 recipe_id/revision
  → CLI coffee-load（提交已校验配方或 recipe_id/revision）
  → bridge 固化 immutable recipe snapshot，返回 workflow_id，并按需连接机器
  → CLI coffee-start --workflow-id ... + 确认短语 + request_id
  → CLI bridge events --workflow-id ... 轮询监控
  → bridge 独立记录确认终态/历史并立即断开 BLE（不依赖 Agent 继续在线）
```

**场景 B：WebUI 拍照设计并冲煮**

```
人类拍豆袋照片（+可选冲配单）/ 输入文字
  → POST /api/design（图片 + 文字）
  → 后端组装提示词（版本化 knowledge + 豆信息 + 图片）
  → provider adapter 调 vision LLM → 返回结构化 recipe candidate + 设计依据
  → JSON Schema + core 校验 → 展示参数/设计依据
  → 用户编辑后再次校验，选择"存入 catalog"或"一键冲煮"
  → backend 创建 recipe revision；bridge 固化 workflow snapshot 并返回 workflow_id
  → 冲煮走类型化 HTTP 操作 → bridge → 机器
  → 前端轮询遥测/事件展示进度
  → Web 关闭或停止轮询也不影响 bridge
  → 完成/取消/停止确认后 bridge 写历史并立即断开 BLE，手机可重新连接
```

**衔接**：两个场景使用同一个事务 catalog 与 recipe revision 模型；Recipes 页统一展示，人类可冲 Agent 设计的配方，反之亦然。每次冲配引用不可变 snapshot，后续编辑不会改变已经加载或历史记录中的配方。

### 3.4 连接生命周期（Phase A 核心）

问题：旧一次性模式让加载、开始和监控等步骤反复重连；无限持有 BLE 又会阻止官方 App 或手机连接。

方案：**bridge 进程常驻，BLE 连接按硬件工作流持有。** 首个需要机器的 RPC 建立连接；同一工作流后续步骤复用；确认进入终态后立即关闭 BLE session 并断连。bridge 自身不退出，下一次 Web/Skill 操作再按需连接。

连接策略：

| 操作类型 | BLE 生命周期 |
|---|---|
| 设计、校验、catalog、历史 | 不连接 BLE |
| 咖啡/茶配方 | `load` 时连接；贯穿 `start`、暂停/恢复和监控；确认完成/取消/停止后断开 |
| 独立磨豆、出水、称重 | 首个命令连接；该操作确认完成/停止后断开 |
| 设置、probe 等单次机器事务 | 连接、执行并读取结果，然后断开 |
| 显式调试 `connect` | 保持到显式 `disconnect`，无活动时受兜底超时保护 |

状态机：

```
bridge process: running（始终存活）
  BLE: disconnected
    └─(load/首个需要机器的 RPC)→ connecting → connected

  connected + workflow loaded/running/paused:
    ├─ 后续命令携带 workflow_id，复用当前连接
    ├─ loaded：等待 start 或显式 cancel；无时间驱动的自动 cancel/unload/断连
    ├─ confirmed completed/stopped/cancelled
    │    → 事务写入最终事件/历史 → close session → disconnect
    └─ unconfirmed control/cancel / recovery-required
         → 保留恢复记录与连接，禁止重复 start；不自动 release

  connected + no workflow:
    ├─ 单次事务完成 → disconnect
    └─ 遗留空闲连接超过兜底超时 → disconnect
```

关键语义：
- 常驻的是 bridge 进程，不是 BLE 连接。
- 建联、机器通知与终态释放由 bridge 自主执行，不依赖发起请求的 HTTP/CLI/MCP 进程继续存活。
- `status` / `events` 轮询不会延长 BLE 生命周期；它们只观察 bridge 内存中的状态。
- loaded/running/paused 期间绝不因空闲计时器断连。
- loaded 配方保持 workflow 连接，等待 start 或显式 cancel；无时间驱动的自动 cancel、unload、过期或断连。
- 连接持有至确认终态；确认完成/取消/停止后立即断连，不等待空闲超时。
- 未确认的 control/cancel 保持 recovery 且保持连接，不自动 release。
- 兜底超时只处理没有活动工作流的遗留连接；`0` 可关闭兜底，但不影响正常终态断连。
- BLE 释放后 bridge 不后台抢连；只有新的显式硬件请求才重连。若手机已占用机器，返回可重试的 `device_busy_external`，不持续重试或抢占。
- 无法确认机器状态时进入 `recovery-required`，不得把记录静默删除。

收益：一个工作流只承担一次建联成本；Web、Skill、MCP 共享同一连接与状态；工作流结束后立即把机器还给手机。

实现落点：`packages/core/xbloom_ble/bridge.py` 的 `BridgeCore` 增加工作流级连接生命周期；终态处理负责持久化最终状态后断连，空闲计时器只承担异常兜底。`status()` 暴露连接作用域、工作流状态和最近释放原因。

### 3.5 Bridge 身份、配置与升级

`bridge.json` 只承担客户端发现与 loopback token 分发，不承担单实例锁。daemon 启动流程：

1. 解析规范化状态根目录并获取生命周期级 `bridge.lock`；获取失败即复用现有实例，绝不再启动第二个进程。
2. 绑定 loopback RPC 后原子写入 `bridge.json`，包含 `instance_id`、PID、端口、token、core version、RPC protocol version、配置指纹和启动时间。
3. 客户端先 `hello`，声明自身版本与支持协议范围；不兼容时禁止发送机器操作。
4. 运行中的 bridge 配置由 daemon 启动快照决定。客户端环境变量不会静默改变现有实例；配置不一致时状态页明确提示。
5. bridge 空闲且无恢复记录时，兼容性管理器可执行受控重启；活动 workflow 中只报告“升级待处理”，不得终止进程。

单实例范围明确为“同一 OS 用户 + 同一规范化状态根目录”。当前产品只支持一个 Studio 设备；如未来支持多设备，应按 device identity 分离 daemon/lock/state，而不是复用当前全局状态机。

### 3.6 Workflow 与多客户端 RPC

`load` 是 workflow 创建边界。成功响应至少返回：

```json
{
  "workflow_id": "uuid",
  "kind": "coffee",
  "recipe_revision_id": "uuid",
  "snapshot_sha256": "...",
  "state": "loaded",
  "source": "skill|web|mcp"
}
```

规则：
- `start`、`pause`、`resume`、`cancel`、实时调节等变更命令必须携带匹配的 `workflow_id` 与唯一 `request_id`。
- bridge 在 SQLite 中记录 request/result；同一 `request_id` 重试返回原结果，不重复写机器。
- 普通客户端不能操作不匹配的 workflow。紧急 `stop` 可不带 workflow ID，但仍需安全确认并记入审计事件。
- `source` / `client_name` 用于可见性与诊断，不作为安全授权；本项目的授权边界仍是本机用户或已配对 Web 会话。
- `status` 可读取当前 workflow；`events` 使用 `(instance_id, sequence)` 游标。daemon 重启或 ring buffer 丢段时返回 `reset_required/gap_detected`，客户端随后从持久事件恢复。
- HTTP 与 MCP 暴露类型化方法，不向浏览器提供可调用任意 bridge method 的通用转发接口。

### 3.7 事务状态与 Recipe Snapshot

状态根目录使用 `state.db`（SQLite/WAL），至少包含：

| 表/领域 | 内容与写入规则 |
|---|---|
| `recipes` / `recipe_revisions` | 稳定 recipe ID、不可变 revision、规范化内容、来源与 provenance |
| `workflows` | workflow ID、recipe snapshot、机器/阶段、owner/source、终态与恢复信息 |
| `workflow_events` | 持久事件序列；bridge 独占写机器事件和最终历史 |
| `idempotency` | request ID、方法、参数 hash、结果与过期时间 |
| `schema_migrations` | 数据库版本与可重复迁移记录 |

写入策略：
- core 提供事务存储 API；CLI、HTTP、MCP 不直接拼 SQL，也不再各自执行 JSON 文件的 load-modify-save。
- WAL + busy timeout 支持 Web/Skill 并发读写；recipe revision 创建和 workflow snapshot 创建必须原子提交。
- `load` 接受规范化 recipe payload 或明确的 `recipe_revision_id`。bridge 在发出任何 BLE 写之前保存不可变 snapshot 与 SHA-256，不依赖调用者临时文件继续存在。
- 源 YAML 路径仅作 provenance；load 之后 immutable `state.db` snapshot 为权威，删除/修改源文件不影响 start。
- 遗留 `armed-state.json` / `tea-loaded-state.json` / `grinder-rest-state.json` 仅为显式 migrate 的 import 输入，runtime 永不读写任何 coffee/tea/grinder JSON；运行时状态只存在于 durable workflows。
- **Grinder SQLite guard（已完成）**：`status.grinder_guard` 状态为 `ready` / `cooldown` / `recovery_required` / `unavailable`；电机写前创建 durable nonterminal grinder workflow；确认 STOP 写入 `stopped_at`/`cooldown_until`/`rest_seconds`（60s）；未确认 STOP 保留 recovery 与 BLE；daemon 重启不自动 BLE，显式 cancel 仅一次 reconnect + STOP；exact 完成的 `grinder.start` 幂等缓存先于 cooldown；多 active 遗留 recovery 迁移整体回滚（备份与原文保留）。**不含** A11 真机验收。
- 每次编辑创建新 revision；已加载 workflow 和历史始终引用当时 snapshot，后续编辑不回写过去。
- provenance 至少记录来源、knowledge version/hash、provider/model（若适用）、父 revision 和创建时间；原始图片默认不保存。
- 最终历史由 bridge 幂等写入一次，避免 CLI/Web 重复记账。
- 提供 schema migration、事务一致性检查和在线备份命令；迁移前自动创建可恢复备份。

### 3.8 AI 设计契约

LLM 接口采用 provider adapter，而不是假设所有服务只换 `BASE_URL` 即兼容：
- `provider` 明确区分 OpenAI-compatible、Anthropic、Gemini 等协议；每个 adapter 声明 vision、structured output 和 token 限制能力。
- 模型输出使用版本化 JSON Schema；YAML 只是 Agent/用户可读的导入导出格式，不作为 Web LLM 的传输协议。
- 后端先做 MIME、文件大小、像素尺寸和解码检查，移除 EXIF；图片在请求结束后删除，除非未来增加用户明确选择的素材库。
- OCR/图片中的文字视为不可信数据，不得覆盖 system/knowledge 指令；模型候选仍须经过 schema 与 core 安全校验。
- 官方冲配单参数作为带来源的 evidence；模型如建议偏离，必须在“设计依据”中逐项说明，最终由用户确认。
- transport 错误可按幂等策略重试；结构化输出修复最多一次，仍非法则向用户返回可编辑候选或明确失败，不循环调用模型。
- catalog provenance 保存 provider、model、knowledge version、prompt template version 与候选 hash，不保存 API key 或原始思维过程。
- 自动化评估使用固定图片/文字 fixtures，检查提取准确性、schema 合法率、安全拦截和关键参数稳定性。

### 3.9 网络、安全与隐私

Web 提供两种互斥运行模式：

| 模式 | 监听与访问 |
|---|---|
| `loopback`（默认） | 只监听 `127.0.0.1`；桌面浏览器同源访问 |
| `lan`（显式开启） | 监听指定内网地址；必须启用 HTTPS、配对认证和精确 origin allowlist |

LAN 模式规则：
- 首次配对码/QR 只在本机控制台或已认证页面显示；一次性使用并短时过期。
- 配对成功换取 HttpOnly、SameSite 会话；变更操作同时校验 CSRF token，会话可在本机撤销。
- 不允许 wildcard CORS，不提供公网发现、内网穿透或自动端口映射。非内网监听地址启动失败。
- HTTPS 由 backend 配置证书或受信任本地反向代理提供；未配置安全传输时不得启动 LAN 模式。

物理安全与隐私规则：
- brew/grinder/water 全部走 bridge，强制 owner gate、确认短语、固件校验和 workflow/idempotency 检查。
- WebUI 默认 `vision`；首次图片设计明确说明图片会发给配置的模型服务。`text` 模式可对图片先本地 OCR，仅发送文字。
- 密钥只从进程 secret 环境读取，不写数据库、响应或日志；确认短语与 bridge token 同样不得记录。
- 上传设置严格大小/类型/超时限制；临时图片与 OCR 中间文件请求结束即清理。

### 3.10 配置项

| 变量 | 用途 | 默认 | 阶段 |
|---|---|---|---|
| `XBLOOM_STATE_DIR` | 规范化状态根目录 | `~/.xbloom-studio-brew` | 0 |
| `XBLOOM_SKILL_STATE_DIR` | 旧名称兼容别名，v1 周期内支持 | 无 | 0 |
| `XBLOOM_ADDRESS` | 唯一受支持设备的显式 BLE 地址 | 自动发现 | 现有 |
| `XBLOOM_ENABLE_REMOTE_START` 等 | bridge 物理动作 owner gate | 未设=禁用 | 现有 |
| `XBLOOM_BRIDGE_IDLE_DISCONNECT_S` | 无活动 workflow 时遗留连接兜底；0=关闭兜底，正常终态仍断开 | `300` | A |
| `XBLOOM_ASSETS_DIR` / `XBLOOM_REFERENCES_DIR` | 仅开发/调试覆盖 knowledge 资源 | 无 | 0/B |
| `XBLOOM_LLM_PROVIDER` | LLM adapter 类型 | `openai-compatible` | B |
| `XBLOOM_LLM_BASE_URL` | 本地 CLP OpenAI-compatible 端点 | 无，必须配置 | B |
| `XBLOOM_LLM_MODEL` | 模型名 | `grok-4.5` | B |
| `XBLOOM_LLM_API_KEY` | 模型密钥（secret） | 无 | B |
| `XBLOOM_DESIGN_MODE` | `vision` / `text` | `vision` | B |
| `XBLOOM_WEB_MODE` | `loopback` / `lan` | `loopback` | C |
| `XBLOOM_WEB_HOST` | LAN 模式明确绑定地址 | `127.0.0.1` | C |
| `XBLOOM_WEB_ORIGINS` | LAN 精确允许的 Web origins | 无 | C |
| `XBLOOM_WEB_PUBLIC_ORIGIN` | 已有本地域名的 HTTPS origin | 无；LAN 模式必需 | C |
| `XBLOOM_WEB_TRUSTED_PROXIES` | 允许传递转发头的本地反向代理地址 | 无；LAN 模式必需 | C |
| `XBLOOM_WEB_TLS_CERT/KEY` | backend 直接终止 TLS 时使用；当前部署由已有反向代理终止 | 无 | C |

---

## 4. 关键设计决策（ADR）

- **ADR-1 设计知识单一来源 + 版本化发布**：人工只维护 Skill 中的 `SKILL.md` + `references/` + `assets/`；release build 生成带 manifest/hash 的 knowledge bundle，Web 不手工复制提示词。
- **ADR-2 操作与状态契约归 core+bridge**：物理操作、配方校验、事务存储 API 与 workflow 状态机在 core/bridge；CLI/HTTP/MCP 是类型化薄适配。
- **ADR-3 Hermes 走 CLI**：Need 1 以 Agent Skill 形态满足，CLI + SKILL.md 即标准接口；MCP 非必需。
- **ADR-4 MCP 保留在 web backend**：作为 Cursor/Claude 等 MCP host 的可选奖励，低维护，暂不移动。
- **ADR-5 bridge 常驻 + 工作流级 BLE 连接**：进程常驻；BLE 按需连接、工作流内复用、确认终态后立即断开。
- **ADR-6 Provider adapter + 结构化输出**：默认使用 vision 以保证拍照识豆体验；不同 LLM 协议由 adapter 适配，输出使用版本化 JSON Schema，YAML 仅作导入导出。
- **ADR-7 发布依赖版本化产物**：Skill 自包含，Web 固定 core + knowledge 版本；仓库相对路径与资源环境变量只用于开发覆盖。
- **ADR-8 单一 BLE 所有者**：Web、Skill CLI 与 MCP 的所有主动连接和机器操作都走 bridge。Skill 可保留原有命令名，但实现为 bridge RPC；除被动扫描外，不再保留绕过 bridge 的直连模式。
- **ADR-9 SQLite/WAL 为共享状态源**：catalog、recipe revisions、workflow、事件与幂等结果统一进入事务数据库，不再以共享 JSON 文件作为并发写模型。
- **ADR-10 Workflow ID + 幂等 RPC**：`load` 创建 workflow；后续变更绑定 workflow ID 与 request ID，紧急停止是显式例外。
- **ADR-11 daemon 身份与受控升级**：系统锁保证原子单实例；客户端校验 instance/core/protocol/config，活动 workflow 期间不重启升级。
- **ADR-12 双网络模式**：默认 loopback；手机 WebUI 使用显式 LAN 模式，必须 HTTPS + 配对会话，不提供公网暴露能力。
- **ADR-13 不可变冲配快照**：每次 workflow 在 BLE 写入前固化 recipe snapshot/hash；编辑生成新 revision，不改变已加载任务和历史。

---

## 5. 开发路线

> Phase 0 是所有后续工作的共同前置。Phase A 与 B 在 Phase 0 后可并行；Phase C 依赖 B，并使用 A 的 workflow/bridge 契约完成硬件闭环。

### Phase 0 — 发布、状态与 daemon 基线

**目标**：让两个仓库在没有 sibling checkout 假设的情况下独立安装，并建立后续功能共同依赖的版本、状态和单实例协议。

**任务**：
- 0.1 完善 `xbloom-studio-core` 包元数据、版本策略和 `xbloom-bridge` console entry；开发依赖可 editable，release 依赖必须固定版本与 hash。
  - **已完成**：core 元数据 + console entry；开发 `-e packages/core`；release 用 vendored wheel（`core_wheel_sha256`）+ 单一 universal `requirements-runtime.lock`（`--require-hashes`，排除 core；uv 0.11.28 生成；`tools/update_runtime_lock.py` update/check）。
- 0.2 release build 从唯一 knowledge 源生成 self-contained Skill bundle 与 versioned knowledge bundle，写入 manifest、knowledge version 和内容 hash。
  - **已完成**：`tools/build_release.py` + tag 驱动 `.github/workflows/release.yml`；**GitHub Release v1.3.0** 已发布 core wheel、knowledge zip、skill zip、`release-manifest.json`。
- 0.3 引入 `XBLOOM_STATE_DIR` 与 `state.db`；为 catalog、recipe revisions、workflows、events、idempotency、migrations 建 schema。
  - **已完成（core）**：`xbloom_storage.StateStore` + WAL schema/migrations。
- 0.4 编写一次性迁移：导入现有 `catalog.json`、`brew-history.jsonl` 和恢复记录；迁移前备份，失败可回滚且不删除原文件。
  - **已完成（core）**：`xbloom-state migrate`；遗留 JSON 仅 import-only。
- 0.5 daemon 使用 OS 生命周期锁；`bridge.json` 写 instance/core/protocol/config 信息，实现 `hello` 兼容握手与 stale record 清理。
  - **已完成（core）**。
- 0.6 core 提供受控 bridge 启动/停止/空闲重启 API；Web 不再查找 Skill 的 `xbloom.py` 来启动 daemon。
  - **已完成（core + Web）**：`ensure_bridge_daemon()`；Web 不依赖 Skill 脚本路径。
- 0.7 Skill bootstrap 不在安装 core 前导入 core；分别验证仓库 checkout、仅 Skill 发布包、仅 Web 发布包三种 clean install。
  - **已完成（Skill）**：stdlib-only bootstrap + release integrity；Web pin 已发布 wheel（v1.3.0）。主干合入后做一次 trunk clean-install 再确认。
- 0.8 建立跨平台 CI：Windows、macOS、Linux 执行 build、安装、迁移、单实例竞争和协议兼容测试。
  - **已完成**：`.github/workflows/test.yml` + release workflow；`codex/roadmap-completion` 与 `v1.3.0` tag 上 CI 已通过。

**验收**：仅拿 Skill 发布包即可 bootstrap/doctor/validate；Web 只安装固定 core + knowledge 产物即可启动；两个并发启动者最终只有一个 bridge；旧 JSON/JSONL 数据无损进入 SQLite；不兼容客户端在任何 BLE 写之前被拒绝。

**影响文件**：core `pyproject.toml`、storage/migration 模块、bridge launcher、Skill bootstrap/requirements/runtime lock、Web requirements/startup、release workflow。

### Phase A — 工作流级连接生命周期（core）

**目标**：bridge 进程常驻；一个硬件 workflow 只连接一次，跨客户端安全控制，确认终态后立即释放 BLE，并能从异常中恢复。

**任务**：
- A1 `load` 校验 recipe/revision，在事务中创建 immutable snapshot + `workflow_id`，再调用 `ensure_connected()`；已连接时不重复 scan/connect/open session。
  - **已完成（core）**：`BridgeCore` 拥有 `StateStore`；coffee/tea `load` 在 BLE 写前创建 immutable snapshot + durable workflow 并返回 `workflow_id`；兼容本地 recipe 路径，可选 `recipe_revision_id`；已连接不重复 connect/open_session。
- A2 为所有变更 RPC 增加 workflow ID 与 request ID 校验；实现幂等结果缓存、参数 hash 冲突检测和紧急 stop 例外。
  - **已完成（core）**：`MUTATING_METHODS` 覆盖全部机器变更 RPC（含 `settings.write` / `advanced.write` / `presets.save`）；要求 `request_id` + SQLite preflight/reserve；method/params 冲突检测；完成结果缓存先于可变门；pending 永不重发；紧急 `emergency=true` 例外；`connect`/`disconnect` 不在幂等合同内。读-only `settings.read`/`advanced.read` 不要求 `request_id`。
- A3 咖啡/茶从 `load` 贯穿 `start`、暂停/恢复和机器事件；客户端/HTTP 断开不改变 bridge 监控任务。
  - **已完成（core + Skill + Web/MCP session/client exit）**：同一 daemon 连接在 load→start→pause/resume→events 间复用；HTTP/页面/客户端断开**不得** cancel 或 release daemon 持有的 durable workflow；Skill `monitor` 与 Web/MCP 被动 `status`/`events` 仅观察，永不 mutate BLE、不 cancel、不 release；观察 duration 到期同样不 cancel/release。session/client exit 语义在 core 与 Skill/Web/MCP 入口均已闭环，无 Web/MCP 后续缺口。
- A4 终态处理在一个事务中固化最终事件和历史，再 close session + disconnect；断连失败单独报告，不把已确认终态回滚为 running。
  - **已完成（core）**：`commit_workflow_terminal` 原子固化 state+event(+idempotency)；成功后才 schedule release；持久化失败 → recovery_required 且不 release；断连失败只写 `last_disconnect_error`。
- A5 独立磨豆、出水、称重在各自确认终态后释放；设置/probe 等单次事务返回完整结果后释放。
  - **已完成（core）**：grinder / water / scale 确认终态后 prompt release。**Grinder SQLite guard 完成**：`grinder_guard`（ready/cooldown/recovery_required/unavailable）sole authority；电机写前 durable nonterminal workflow；确认 STOP 写入 cooldown 字段（60s）；未确认 STOP 不 release；重启后 cancel 一次性 reconnect+STOP、无 auto-start；exact 完成幂等先于 cooldown；无 runtime grinder JSON；多 active 迁移回滚。**非** A11 真机验收。`settings.write` / `advanced.write` / `presets.save` 在首写前建 durable one-shot workflow（含 baseline/recipe snapshot），确认成功后 terminal+release；确认 rollback 或 pre-write 失败则 terminal failed + 仅释放 auto-owned；不确定/部分写保持 pending 与连接。`settings.read` / `advanced.read` 无 durable workflow，成功/失败后立即释放 auto-owned，不释放 explicit debug。
- A6 控制结果不确定、BLE 意外断开或 daemon 重启时进入 recovery，对账机器状态且绝不重复 start。loaded 配方等待 start 或显式 cancel，**不**做时间驱动的自动 cancel/unload/断连；保持 workflow 连接。未确认 control/cancel 保持 recovery 且保持连接。
  - **已完成（core）**：`XBloomClient` 经 Bleak `disconnected_callback` 区分意外掉线与 bridge 主动 close/disconnect；意外掉线安全卸除 client 所有权、保留 address、`connected=false`/`connection_scope=null`、不自动重连。有 durable workflow 时保留 activity/`active_workflow_id`，持久化 `ble_disconnected` 并 surface `recovery_required`（loaded coffee 需 fresh armed 对账；loaded tea fail-closed；running/paused/starting/unconfirmed 保持原 phase 并标 recovery，不写回 running）。无 activity 则静默 settle 为 disconnected。显式 RPC `recovery.reconcile`（需匹配 `workflow_id`）仅 connect+query fresh state（generation gate），绝不 load/start/control 写；fresh terminal → 原子 terminalize 再 release；fresh armed/active/paused → 重挂监控并 durable reconcile 成功后才清 recovery；tea loaded 无正标记保持 recovery；connect/query 失败保留所有权。外部占用/GATT busy 归类 `device_busy_external`，单次 connect 不重试不抢占。终态 release 后 expected disconnect 不 race 成 recovery；mutating RPC 飞行中掉线保持 pending；persist 失败内存 fail-closed。无五分钟 loaded 过期、无周期自动重连。
- A7 `XBLOOM_BRIDGE_IDLE_DISCONNECT_S` 只处理无活动 workflow 的遗留连接；`status` / `events` 不续期。释放后不自动抢连外部客户端。
  - **已完成（core）**：默认 300s，`0` 关闭；仅当无 activity、无 active/recovery workflow、`connection_scope` 为 `workflow`/`one-shot` 的遗留链路时兜底断开；loaded/running/paused/recovery/unconfirmed 与 explicit debug 永不超时；`status`/`events` 不创建/重置/延长计时器；正常终态仍即时 prompt release；释放后不自动重连。status 暴露 `idle_disconnect_s` / `idle_orphan_since` / `idle_orphan_deadline` 供运维与测试。
- A8 `status()` / `events()` 暴露 instance ID、workflow、版本、连接作用域、事件游标/gap、恢复状态及最近断连原因。
  - **已完成（core）**：`active_workflow_id`、durable `workflow` summary、`recovery`、versions、connection_scope/release/disconnect 字段；durable events cursor + `gap_detected`；idle orphan 可观测字段。
- A9 Skill、Web 和 MCP 的连接型操作全部收敛到类型化 bridge RPC；仅被动 scan 可直用 BLE discovery。
  - **已完成（core + Skill + Web + MCP；sibling Web commit `63d91a4`）**：`xbloom_ble.bridge_client.TypedBridgeClient` 为 Skill/Web/MCP 共用类型化 RPC 表面（自动 `request_id`、显式 `workflow_id`、ensure daemon、hello/协议兼容）；`client_name` 仅作诊断/可见性标签（非授权；Web MCP adapter 可与 Web 共用如 `xbloom-studio-web`，不保证入口一一对应）；`probe` 为 BridgeCore one-shot 读-only；`BridgeError.category` 跨 JSON-line 往返；Skill CLI 全部主动连接/机器操作走 daemon（仅 `scan`/`doctor --scan` 直连 discovery）；Web typed routes 与 MCP 经同一 typed client 契约 cutover。无五分钟 loaded 过期。**不含**真实硬件 A11。
- A10 单元/集成测试覆盖并发 start、重复 request ID、错误 workflow ID、客户端退出、daemon restart、BLE drop、loaded 保持连接直至 start 或显式 cancel（无时间驱动自动动作）、确认终态后断连、未确认 control/cancel 保持 recovery 与连接，以及 external busy。
  - **已完成**：core fake-client 单元矩阵（错误 workflow ID、重复 request_id 幂等、BLE drop + `recovery.reconcile`、loaded 保持/无时间驱动动作、未确认 control/recovery、确认终态 release、external busy）见 `test_bridge.py` / `test_bridge_client.py` / A6；**真实 JSON-line 传输 + 多 TypedBridgeClient** 集成见 `skills/xbloom-studio-brew/tests/test_a10_transport_integration.py`：跨客户端 handoff（Skill load → 客户端退出 → Web status/events 观察 → MCP start → 确认终态 release，load→start 一次 connect/一次 load/一次 start）；并发同 `request_id` 缓存单次写；并发异 `request_id` 仅一次 start 成功；daemon 进程丢失后同 state root 重建（status/events 不建联；`recovery.reconcile` 仅 connect+query；start 不 re-load）。Phase A 仅剩 A11 真实硬件验收。
- A11 真实硬件测试记录一次完整 load/start/pause/resume/complete 的连接次数；完成后从手机官方 App 连接，再从 Web/Skill 发起下一工作流。
  - **未完成（真机）**：fake/集成测试已覆盖 A1–A10；A11 与 `references/hardware-validation.md`（H00–H08）仍待 supervised 验收。

**实现落点（已落地）**：`packages/core/xbloom_ble/bridge.py` + `bridge_client.py` + `client.py` + `packages/core/xbloom_storage.py`；fake 测试见 `skills/xbloom-studio-brew/tests/test_bridge.py`、`test_bridge_client.py`、`test_a10_transport_integration.py`、`test_client.py` 与 `test_storage.py`。显式 `connect` 保持到显式 `disconnect`；coffee/tea 从 load 保持连接至确认终态或确认 cancel；workflow/one-shot 在确认终态后释放；settings/advanced/presets 写为 durable one-shot；读-only 设置立即释放 auto-owned；遗留 auto-owned 链路可由 idle 兜底；daemon 进程不退出。loaded 无时间驱动的自动 cancel/unload/断连，**无**五分钟 loaded 过期。意外 BLE 掉线进入 recovery；`recovery.reconcile` 仅 connect+query。RPC protocol **v3**（破坏性：变更 RPC 需 `request_id` / workflow-bound 控制需 `workflow_id`）。

**验收**：一次配方冲配只观察到一次 BLE 建联；loaded 配方等待 start 或显式 cancel，无时间驱动的自动 cancel/unload/断连；连接持有至确认终态后释放；发起客户端退出后冲配仍完成；终态与历史持久化后 `connected=false`、bridge 仍 `running=true`；重复/旧请求不产生第二次机器写；未确认控制/取消保持 recovery 且保持连接、不 release；恢复流程不重复 start/load；释放后手机可连接，手机占用时 bridge 不抢占（`device_busy_external`，单次尝试）；下一次显式 Web/Skill 操作可重新连接并完成 workflow。

**影响文件**：core bridge/workflow/storage、Skill CLI、Web bridge client/routes/MCP、对应 unit/integration/hardware tests。

### Phase B — WebUI 设计服务（backend）

**目标**：拍照/文字 → provider adapter → 结构化候选 → schema/core 校验 → recipe revision。

**任务**：
- B1–B10：**已完成（`xbloom-studio-web` feature 分支）** — `backend/design/`、`POST /api/design`、knowledge 校验、provider adapter、revision CRUD、路径边界、mock 合同测试。真 LLM 固定 fixture 准确率评测仍为可选加强项。

**验收（代码层）**：非法输出不进 catalog/bridge；text 模式不送图；并发 revision 稳定；密钥/原图不落库。  
**仍待**：合入 `master`；可选真 provider eval。

### Phase C — WebUI 设计前端（frontend）

**目标**：把 Phase B/A 能力做成桌面和已配对手机均可用的“拍照→配方→冲煮→释放 BLE”闭环。

**任务**：
- C1–C9：**已完成（`xbloom-studio-web` feature 分支）** — loopback/LAN 安全、Design/Dashboard/Pair/Recipes、workflow 监控与恢复 UX、Playwright（fake bridge）。真机/真手机闭环仍待 A11 与 supervised 验收。

**验收（代码层）**：loopback 默认；LAN 需配对；页面关闭不影响 bridge。  
**仍待**：合入 `master`；真人/真机冒烟。

### 合入与收尾（主线剩余）

| 项 | 状态 |
|----|------|
| brew `codex/roadmap-completion` → `main` | 待合入（领先 ~14 commit） |
| web 同名分支 → `master` | 待合入（本地仓库；无 GitHub remote 时本地 merge） |
| A11 真机 workflow 生命周期 | 未做 |
| hardware-validation H00–H08 | 未做 |
| 社区 xBloom 开源取经 | **明确延后** |

---

## 6. 已确认实施选择

- **Phase 顺序**：`0 → (A 与 B 可并行) → C`。
- **发布渠道**：初期使用 GitHub Release 发布 core wheel 与 knowledge bundle；self-contained Skill 发布包携带固定版本的 core wheel。暂不要求 PyPI。**当前：v1.3.0 已发布。**
- **Vision provider**：使用本地 CLP 反代提供的 OpenAI-compatible 接口，主模型为 `grok-4.5`；provider adapter 边界仍保留，便于未来切换。
- **LAN HTTPS**：复用已有本地域名与可信反向代理终止 TLS；Web backend 校验固定 public origin 和 trusted proxy，不自行承担证书签发。

---

（本文件已确认为 v1.0 开发基线；需求或 ADR 变化须先更新本文档，再进入实现。实现进度旁注以本节为准，合入主干后同步更新。）

---

## 7. 下一阶段

Phase 0–C 与 A11 核心真机路径完成后的工作（博采众家之长、豆库/口味闭环、**Home Assistant only**）见：

→ **[`docs/ROADMAP-NEXT.md`](./ROADMAP-NEXT.md)**（Homebridge/Siri 已取消）

**Web 产品线（渐进）**：Chrome 近场 **纯 Web + Web Bluetooth**（可不依赖本机 bridge）见：

→ **[`docs/ADR-WEB-BLUETOOTH.md`](./ADR-WEB-BLUETOOTH.md)**（W0–W4 已在 `xbloom-studio-web` 落地：decode/load/start/cancel、能力可用时默认 `web-bluetooth`；W5 可选延后；真机 supervised 仍建议用户侧验收）
