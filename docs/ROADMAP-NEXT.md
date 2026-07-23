# 下一阶段路线图：博采众家 + 智能家居

> 状态：**已确认 v1.0**（2026-07-23）；实施中  
> 最后更新：2026-07-23  
> 前置：Phase 0–C 代码已合主干；A11 真机核心路径已验收；Web 真机/LAN 延后  
> 相关基线：`docs/ARCHITECTURE-AND-ROADMAP.md`、`docs/BLE-LIFECYCLE.md`  
> **已确认选择**：顺序 `D → E → F`；**只要 Home Assistant，不要 Homebridge/Siri**；自动化默认关；配方进 Home 需 opt-in。  
> **进度**：D 知识+检查表已落地；E beans/preferences schema+CLI 已落地；**F HA thin client v0.1 已实现**（RPC/传感器/冲煮服务；真机 HA 待用户侧验证）。

---

## 0. 目标

在**不削弱**现有确定性边界（core 校验、bridge 唯一 BLE 所有者、workflow 幂等、安全门禁）的前提下：

1. **博采众家之长**：吸收社区在配方知识、Agent 推理工作流、拍照识豆与口味迭代上的精华。  
2. **智能家居入口**：用 **Home Assistant** 作为**薄客户端**，复用本机 `xbloom-bridge`，而不是再实现一套 BLE 栈。**不做 Homebridge / Siri / HomeKit**（用户已确认只要 HA）。

**非目标（本阶段不做）：**

- 用 Node / HA / Homebridge 替换 `xbloom-studio-core` bridge。  
- 远程托管 MCP、对话收集邮箱密码。  
- 公网暴露 bridge 或绕过 loopback 令牌。  
- 支持 xBloom J15 / Original 等非 Studio 机型（`brAzzi64` 仅作文档参考）。

---

## 1. 现状锚点（已完成，不再重复建设）

| 能力 | 状态 |
|------|------|
| core 1.2.0 + SQLite 状态 + protocol v3 | 已发布 / main |
| workflow 级 BLE + 终态释放 + App 可重连 | A11 真机核心路径已过 |
| Agent Skill + 云 catalog / history | 可用 |
| Web design / LAN / typed bridge | 代码在；真机 Web 延后 |
| 上游协议 / 配方 Skill | 已基于 `Janczykkkko/xbloom-ble`、`ryunana/xbloom-studio-recipe-skill` |

---

## 2. 开源地图与取舍摘要

| 来源 | 吸收什么 | 不吸收什么 |
|------|----------|------------|
| **Saievo/xbloom-CoT-Brew** | 453 条统计规律、模板 A–G、振动/堵滤、瑰夏分流、冰冲重构、强制 CoT、豆库+口味 | 云 MCP 架构、明文密码登录 |
| **denull0/xbloom-agent** · **AML225/xbloom-ai-mcp** | 云配方字段/工具形状（对照 catalog） | 远程 Supabase MCP |
| **hgstrm/pourpilot** · **Ahmad9077/xbloom-bean-to-bloom** | 拍照识袋 UX、证据来源展示、口味 dial-in | 云 Worker 替代本地 design |
| **makentor/XbloomAutoChef** | 多剂量候选（如 150/240 ml） | 仅云推送路径 |
| **cryptofishbug/xbloom-recipe-cli** | share-link 导入细节 | 独立 auth 文件体系 |
| **Alshekhi/xbloom-studio** · **saya6k/hacs-xbloom** | HA 实体/服务/自动化模型；on-demand BLE 释放语义 | 其内嵌 BLE 实现 |
| **aziz66/homebridge-xbloom** | 每配方 = HomeKit 开关；Siri 话术 | 独立 BLE 插件栈 |
| **Mel0day/xbloom-ai-brew** | dose 必述、轻量例子 | Node bridge |
| **Janczykkkko** · **ryunana** | 持续 diff 上游 | — |
| **brAzzi64/xbloom-ble** | PROTOCOL.md 写法 | J15 协议混入 Studio |
| 保修/撞名仓库 | — | 全部忽略 |

详细扫描结论可随实现迭代补进本节或独立 `OPENSOURCE-SURVEY.md`。

---

## 3. 架构原则（智能家居专用 ADR）

### ADR-H1：Home 客户端只做 thin client

```text
Siri / HomeKit          Home Assistant
       │                       │
       └───────┬───────────────┘
               ▼
     home-adapter（本机，loopback）
               │  TypedBridgeClient / JSON-line RPC
               ▼
     xbloom-bridge（唯一 BLE 所有者，state.db）
               ▼
          xBloom Studio
```

- 所有物理动作仍要求：owner gate（daemon 启动时捕获）+ 确认短语（或 HA/Siri 侧等价「确认实体」策略，见下）。  
- **禁止** HA/Homebridge 直连 Bleak 绕过 bridge。  
- 与 Skill/Web/MCP **共享**同一 daemon、同一 `workflow_id` 契约。

### ADR-H2：安全在自动化场景下的折中

自动化（闹钟、presence）无法每轮人工念确认短语。采用分级：

| 模式 | 行为 |
|------|------|
| **interactive（默认）** | 与 CLI 相同：每次 brew 需确认实体/短语或 UI 点确认 |
| **automation-armed** | 用户在 HA/Homebridge 显式开启「允许自动化启动」+ 部署级 owner gate 已开；每次启动仍写审计事件，且可设时间窗/次数上限 |

两级均不得绕过 firmware 校验与 workflow 幂等。默认关闭 automation-armed。

### ADR-H3：配方暴露方式

- HA：`select`/`button` 或 recipe 实体列表 ← 来自 `state.db` catalog + 本地 revision（只读展示 + 选定后 load/start）。  
- HomeKit：每个**用户标记为 home-export** 的配方 → 一个 Switch/Outlet（参考 homebridge-xbloom）。  
- 不自动导出全部云配方；默认 opt-in，避免误触高风险热冲。

### ADR-H4：连接生命周期

与 Phase A 一致：load 建联 → 工作流内复用 → 确认终态后立即释放 → 手机 App 可连。  
HA 轮询 status **不得**延长 BLE。

---

## 4. 分阶段路线

> 总序：**`D（知识）→ E（偏好闭环）→ F（HA）`**。  
> **Phase G（Homebridge/Siri）已取消。** Web 真机/LAN 见文末延后项。

### Phase D — 配方知识增强（博采 P0）

**目标：** 把社区统计与特例规则收成**可审计、版本化**的 knowledge，而不是换模型玄学。

**任务：**

| ID | 内容 | 来源 |
|----|------|------|
| D1 | 新增 `references/recipe-baselines.md`（或等价）：模板 A–G、烘焙/处理法映射表，标注置信度与样本量 | CoT-Brew |
| D2 | 振动 U 形 + **堵滤风险评分** + 蛋糕滤纸对策 | CoT-Brew |
| D3 | 瑰夏按产地×处理法分流；升温曲线适用条件 | CoT-Brew |
| D4 | 冰冲「从热饮重构」规则与现有 `flash-brew`/`ice_g` schema **对齐表述** | CoT + 现有 skill |
| D5 | `SKILL.md` **强制设计检查表**（出 YAML 前：烘焙×处理、排气、振动/堵滤、是否对照 catalog/history） | CoT |
| D6 | Web design prompt 注入同一 knowledge 版本（bundle hash） | 现有 B 路径 |
| D7 | release：knowledge version bump；manifest/hash 更新 | 现有 release |

**验收：**

- Agent/Web 设计路径可引用 baselines；初杯仍默认保守档，进阶档可选。  
- 所有候选仍过 core 校验；文档写明「统计 ≠ 最优」。  
- knowledge bundle 可复现构建。

**明确不做：** 把 `recipes_v2.json` 整包打进默认 Skill zip（体积/再分发）；可作可选 research pack。

---

### Phase E — 豆库、偏好与口味迭代（博采 P0/P1）

**目标：** 结构化「豆 → 冲 → 评 → 改一档」，跨 Skill/Web 共用。

**任务：**

| ID | 内容 |
|----|------|
| E1 | `state.db`：`beans`、`preferences`（酸/甜/醇/浓度等）、可选 `water_profile` |
| E2 | Skill CLI：`beans` / `preferences` / 扩展 `history note` 与 taste 建议 |
| E3 | 设计前默认读取：最近 history + 同名豆 + preferences |
| E4 | Web：豆袋设计结果可一键关联 bean；History 页评分/笔记 |
| E5 | （可选）多剂量候选：同豆输出 150/225/240 等 size 变体 | AutoChef |

**验收：**

- 同一 `XBLOOM_STATE_DIR` 下 Skill 与 Web 读写一致。  
- 无第二套 `~/.xbloom/*.json` 运行时权威源（迁移可 import）。

---

### Phase F — Home Assistant 集成（你点名要）

**目标：** HACS 可装的自定义集成；本机只连 bridge，不自握 BLE。

**参考：** [Alshekhi/xbloom-studio](https://github.com/Alshekhi/xbloom-studio)、[saya6k/hacs-xbloom](https://github.com/saya6k/hacs-xbloom) 的**产品面**（实体、服务、自动化），实现上对接我们的 TypedBridgeClient。

**任务：**

| ID | 内容 |
|----|------|
| F1 | 仓库形态：`xbloom-studio-ha`（或 monorepo `integrations/homeassistant`）+ HACS 清单 |
| F2 | Config flow：bridge 发现（`bridge.json` / host+port+token 本机）、state root、automation-armed 开关 |
| F3 | 实体：连接状态、firmware、`active_workflow`、phase、水量/杯重（观察）、grinder_guard、BLE released |
| F4 | 服务：`load_recipe`（revision_id 或 catalog id）、`start`、`pause`、`resume`、`cancel`、`emergency_stop`；均带 `workflow_id`/`request_id` 语义 |
| F5 | Recipe：从 core storage 列可执行热/茶；opt-in「允许 HA 启动」标记 |
| F6 | 安全：默认 interactive 确认 helper；automation-armed 需二次配置 + 审计 log |
| F7 | 文档：与手机 App 共存（终态释放）、owner gate 在 **bridge 进程环境**配置 |
| F8 | 测试：mock TypedBridgeClient / fake bridge；无真 BLE CI |

**验收：**

- HA 完成：选配方 → load →（确认）start → 监控 → 终态 → `connected=false`，App 可连。  
- 并发：Skill load 时 HA start 错误 workflow 被拒。  
- 不安装 bleak 进 HA 集成（仅 HTTP/JSON-line 到 loopback bridge）。

**依赖：** 本机已跑 `xbloom-bridge`（与 Skill/Web 相同）。

---

### Phase G — Siri / HomeKit（Homebridge）— **已取消**

用户确认：**只要 HA，不要 Homebridge/Siri。** 原 G1–G6 不实施。若未来需要，另开需求。

---

### Phase H — 可选加固（不挡 F）

| ID | 内容 | 优先级 |
|----|------|--------|
| H1 | A11 补测 pause/resume | 低 |
| H2 | hardware-validation H00–H08 | 按需 |
| H3 | Web 真机 + LAN 手机拍照闭环 | 中（你已说可后测） |
| H4 | 上游 `Janczykkkko` / `ryunana` 定期 diff | 持续 |
| H5 | pourpilot 式「证据/来源」UI 抛光 | 中 |
| H6 | share-link 导入对照 cryptofishbug | 低 |

---

## 5. 建议里程碑与交付物

| 里程碑 | 交付 | 成功标准 |
|--------|------|----------|
| **M1** | Phase D | knowledge 新版本 + Skill 检查表；release 可构建 |
| **M2** | Phase E | beans/preferences 进 state.db；Skill 可读可写 |
| **M3** | Phase F | HACS 集成 + 文档；假 bridge 测试绿；真机一条 HA 冲煮 |
| **M4** | Phase G | Homebridge 插件 + Siri 话术文档；真机一条 Siri 冲煮 |
| **M5** | 收尾 | 上游 diff 笔记；可选 Web 真机勾选 |

**版本策略（建议）：**

- knowledge-only → knowledge patch / minor（如 1.3.0 knowledge）  
- state schema 迁移 → core minor（1.3.0）  
- HA/HB 独立版本号，pin 兼容 `rpc_protocol` 与 core 版本范围  

---

## 6. 仓库与目录建议

```text
xbloom-studio-brew/          # 已有：core + skill + knowledge 源
  docs/ROADMAP-NEXT.md       # 本文件
  packages/core/             # 可增：home 无关的 storage API（beans 等）
  packages/home-contract/    # 可选：HA/HB 共用的 recipe DTO / 安全枚举（薄）

xbloom-studio-web/           # 已有：人类 UI；E4/H3/H5

xbloom-studio-ha/            # 新建：HACS 集成（Phase F）
homebridge-xbloom-studio/    # 新建：Homebridge 插件（Phase G）
```

命名可调；原则是 **HA/HB 不 vendoring 整份 bridge**，只依赖已安装 core 或纯 RPC 客户端。

---

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 自动化误启动热冲 | 默认 interactive；automation-armed 显式 + 时间窗 + 审计 |
| HA 与 Skill 抢 workflow | workflow_id 绑定；错误 ID 拒绝；status 只读 |
| 社区知识过拟合官方 App 众数 | 文档置信度；保守默认；用户 catalog 优先（已有 M2M 配方路径） |
| Homebridge 无确认短语 | G3 默认「仅 load」或要求 Home 里点两次；与 ADR-H2 对齐 |
| 多集成多 BLE | 强制唯一 bridge；文档写清关 App |

---

## 8. 确认清单（开工前）

请确认或修改：

- [ ] Phase 顺序：`D → E → F → G`（F/G 可在 E 后并行）  
- [ ] HA 与 Siri **都要**，且均为 bridge thin client  
- [ ] 自动化默认关；需要时再开 automation-armed  
- [ ] 配方进 Home 需 **opt-in**，不整库导出  
- [ ] Web 真机仍延后  
- [ ] 首个实现从 **D1–D5 知识 + Skill 检查表** 开始  

---

## 9. 与 v1.0 基线的关系

- 不修改已确认 ADR-1…13 的核心含义；本文件新增 **ADR-H1…H4** 仅约束 Home 入口。  
- 若 Home 安全模型或仓库拆分有变，先改本文再写代码。  

（确认后可将状态改为「已确认 v1.0」，并开 Phase D 任务。）
