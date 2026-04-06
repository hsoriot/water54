# Codex Workflow Automation

用一份用户主配置 YAML 定义、初始化并运行 Codex agent workflow。

这个项目适合两类场景：

1. 你已经有一套 workflow 文件，想直接运行
2. 你只知道自己要几个 agent、它们怎么协作，想先生成模板包再补内容

当前模型只有一个用户主入口：`workflow.yaml`。

- `codex-workflow init`：根据主配置生成一整套可编辑模板文件
- `codex-workflow run`：直接运行这份主配置
- memory、shared、control、run artifacts 都落在文件系统里，而不是依赖聊天历史

## 当前能力

- `codex exec` 非交互驱动
- step 级 JSON 路由，最小协议为 `success` + `next`
- 显式分支跳转
- 有界循环
- 显式并行 fanout/join
- 每次运行独立 run 目录落盘
- prompt 模板变量渲染
- 一份主配置 YAML 同时用于初始化和运行

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 5 分钟上手

### 1. 准备一份主配置 YAML

最小示例见：
[scaffold-blueprint.yaml](/Users/riot/riot/codex-workflow-automation/examples/scaffold-blueprint.yaml)

它表达的是：

- 有哪些 agent
- 每个 agent 是否有 memory
- 每个 agent 是否使用 shared 文件
- agent 可能返回哪些 `next`
- workflow 从哪开始

一个典型配置如下：

```yaml
name: sample-scaffold
template_type: multi-agent
workdir: /abs/path/to/your/project

control:
  enabled: true

shared:
  files:
    - id: handoff
      path: shared/handoff.md
      purpose: request-and-handoff

agents:
  - id: planner
    role: high-level planning and routing
    uses_memory: true
    uses_shared:
      - handoff
    next_options:
      - executor
      - finish

  - id: executor
    role: implementation and validation
    uses_memory: true
    uses_shared:
      - handoff
    next_options:
      - planner
      - finish

workflow:
  start_at: planner
  max_steps: 12
  run_root: .runs
```

### 2. 生成模板包

```bash
.venv/bin/codex-workflow init examples/scaffold-blueprint.yaml /tmp/my-workflow
```

生成后目录大致是：

```text
my-workflow/
  README.md
  workflow.yaml
  control.yaml
  prompts/
  schemas/
  memory/
  shared/
  examples/
```

### 3. 补全你的业务内容

你通常只需要改这些文件：

- `workflow.yaml`
- `prompts/*.md`
- `schemas/*.json`
- `memory/*.md`
- `shared/*.md`
- `control.yaml`

### 4. 运行

```bash
.venv/bin/codex-workflow run /tmp/my-workflow/workflow.yaml
```

如果你已经有一套 workflow 文件，也可以直接运行：

```bash
.venv/bin/codex-workflow run /abs/path/to/workflow.yaml
```

## 用户主配置怎么理解

可以把主配置理解成一张流程关系表：

- `agents`：有哪些角色
- `shared.files`：有哪些共享文件
- `uses_shared`：每个 agent 读哪份 shared
- `uses_memory`：是否给这个 agent 单独配一份长期记忆
- `next_options`：这个 agent 最终可能把流程送去哪里
- `workflow.start_at`：从哪个 agent 开始
- `workflow.max_steps`：整条 workflow 最多执行多少步

如果用户第一次接触这个项目，最重要的是理解这 6 个字段。

## 目录职责

- `workflow.yaml`
  用户主配置。定义 agent、shared、memory、路由和起点。

- `control.yaml`
  人工干预入口。用于暂停、强制下一步、人工备注。

- `prompts/`
  每个 agent 的工作说明书。告诉 agent 先读什么、做什么、什么时候返回哪个 `next`。

- `schemas/`
  每个 agent 的 JSON 输出契约。prompt 里要求的 JSON 字段必须和这里一致。

- `memory/`
  每个 agent 的长期记忆。适合累计结论、变化原因、失败教训，也适合人工直接修改。

- `shared/`
  多 agent 或 human/agent 共享状态，例如请求、handoff、闭环说明。

- `examples/`
  帮助你理解一次完整 workflow 包应该长什么样。

## JSON 路由协议

默认最小协议：

```json
{
  "success": true,
  "next": "executor"
}
```

原则只有三条：

1. JSON 只负责流程跳转
2. memory/shared/run 文件负责沉淀状态
3. prompt 和 schema 必须严格对齐

## 人工干预

人工介入优先改文件，不要先改运行器代码。

常见入口：

- `memory/*.md`
- `shared/*.md`
- `control.yaml`

下次运行时，agent 会重新读取这些文件，所以这也是推荐的暂停后恢复方式。

## 循环和并行

### 循环

- workflow 级 `max_steps`
- step 级 `max_visits`
- 重复访问的 step 会写入 `step__02/`、`step__03/` 这样的目录

示例：
[loop-workflow.yaml](/Users/riot/riot/codex-workflow-automation/examples/loop-workflow.yaml)

### 并行

- 显式 `parallel`
- 显式 `join`
- 并行子步骤全部成功后进入 join step

示例：
[parallel-workflow.yaml](/Users/riot/riot/codex-workflow-automation/examples/parallel-workflow.yaml)

## 调试和审计

每次运行都会创建独立 run 目录：

```text
.runs/<timestamp>-<workflow-name>/
  run_manifest.json
  <step-id>/
    prompt.txt
    schema.json
    output.json
    stdout.log
    stderr.log
```

排查顺序建议：

1. 看 `run_manifest.json`
2. 看出问题 step 的 `stderr.log`
3. 看该 step 的 `prompt.txt`
4. 看该 step 的 `schema.json`
5. 确认 prompt 和 schema 是否一致

## 内置示例

- 基础分支：
  [branching-workflow.yaml](/Users/riot/riot/codex-workflow-automation/examples/branching-workflow.yaml)

- 循环：
  [loop-workflow.yaml](/Users/riot/riot/codex-workflow-automation/examples/loop-workflow.yaml)

- 并行：
  [parallel-workflow.yaml](/Users/riot/riot/codex-workflow-automation/examples/parallel-workflow.yaml)

- 主配置示例：
  [scaffold-blueprint.yaml](/Users/riot/riot/codex-workflow-automation/examples/scaffold-blueprint.yaml)

- 通用模板包：
  [workflow-template](/Users/riot/riot/codex-workflow-automation/templates/workflow-template)

## 命令

初始化模板包：

```bash
.venv/bin/codex-workflow init examples/scaffold-blueprint.yaml /tmp/my-workflow
```

运行：

```bash
.venv/bin/codex-workflow run /tmp/my-workflow/workflow.yaml
```

覆盖变量：

```bash
.venv/bin/codex-workflow run /tmp/my-workflow/workflow.yaml --var task_text=请修复这个问题
```

## 测试

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

当前这套实现已验证：

- 主配置 YAML 可直接运行
- 主配置 YAML 可生成模板包
- 循环
- 并行
- 模板包生成后可继续被运行器读取
