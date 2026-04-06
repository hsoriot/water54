# Workflow Template

这是一个可直接复制的通用 workflow 模板包。

适用场景：
- 单 agent 工作流
- 多 agent 协作工作流
- 需要长期记忆、共享交接和人工干预的任务

## 目录说明

```text
workflow-template/
  README.md
  workflow.yaml
  control.yaml
  prompts/
    agent-a.md
    agent-b.md
  schemas/
    agent-a-output.json
    agent-b-output.json
  memory/
    agent-a.md
    agent-b.md
  shared/
    handoff.md
  examples/
    sample-run-notes.md
```

可以把这个目录整体复制到你的项目里，再按下面的步骤修改。

## 你只需要改哪些文件

第一次使用时，优先改这几个：

1. `workflow.yaml`
2. `prompts/agent-a.md`
3. `prompts/agent-b.md`
4. `schemas/agent-a-output.json`
5. `schemas/agent-b-output.json`
6. `memory/agent-a.md`
7. `memory/agent-b.md`
8. `shared/handoff.md`

## 每个文件是干什么的

- `workflow.yaml`
  定义 step、跳转、循环、并行和 `workdir`

- `control.yaml`
  人工干预入口，例如暂停、强制下一步、人工备注

- `prompts/*.md`
  每个 agent 的行为说明书

- `schemas/*.json`
  每个 agent 最终 JSON 输出契约

- `memory/*.md`
  每个 agent 的长期记忆，可人工修改

- `shared/handoff.md`
  多 agent 共享的交接/请求/闭环文本

## 如何自定义

### 1. 改工作目录

编辑 `workflow.yaml`：

```yaml
workdir: /abs/path/to/your/project
```

### 2. 改 agent 名和跳转

如果你只需要一个 agent，可以删掉 `agent-b`，把 `agent-a` 直接路由到 `__end__`。

如果你需要两个角色，比如 planner / executor，就把：
- `prompts/agent-a.md`
- `prompts/agent-b.md`
- `memory/agent-a.md`
- `memory/agent-b.md`

替换成你的角色含义。

### 3. 改 schema

默认 JSON 输出只要求：
- `success`
- `next`

如果你要额外字段，例如 `summary`、`request_id`，必须同步修改对应 schema。

### 4. 改 memory

把 `memory/*.md` 写成适合你项目的长期记忆骨架。

### 5. 改 shared handoff

如果是多 agent 协作，继续使用 `shared/handoff.md`。
如果是单 agent，也可以保留，作为 human 与 agent 的共享备注文件。

## 如何运行

先安装运行器：

```bash
cd /Users/riot/riot/codex-workflow-automation
python3 -m venv .venv
.venv/bin/pip install -e .
```

再执行：

```bash
/Users/riot/riot/codex-workflow-automation/.venv/bin/codex-workflow run /abs/path/to/workflow-template/workflow.yaml
```

## 如何人工介入

优先改这些文件，而不是改代码：

- 改 `memory/*.md`
  用于修改 agent 长期判断

- 改 `shared/handoff.md`
  用于插入请求、备注、闭环状态

- 改 `control.yaml`
  用于暂停、强制下一步、写人工备注

然后重新运行 workflow。

## 如何调试

每次运行都会在 `run_root` 下生成一个 run 目录，里面有：

- `run_manifest.json`
- 每个 step 的 `prompt.txt`
- `schema.json`
- 输出 JSON
- `stdout.log`
- `stderr.log`

排查顺序建议：

1. 先看 `run_manifest.json`
2. 再看出问题 step 的 `stderr.log`
3. 再看该 step 的 `prompt.txt`
4. 再看对应 schema 是否和 prompt 一致
