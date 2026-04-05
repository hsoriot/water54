# Codex Workflow Automation

用 YAML 定义多步 Codex 工作流，按每一步的结构化 JSON 输出决定是否继续、走向哪个分支，也支持有界循环和显式并行块。

## 能力

- `codex exec` 非交互驱动
- 每一步都通过 JSON Schema 约束最终输出
- 输出 JSON 至少包含两个字段：`success` 和 `next`
- `success=false` 默认终止流程，也可在 YAML 里定义 `on_failure`
- `next` 支持分支路由，脚本解析 JSON 后决定下一步
- `max_steps` 和 `max_visits` 支持有界循环
- `parallel` + `join` 支持显式并行块
- 每次运行都会落到独立 run 目录，保存 prompt、schema、stdout、stderr、step output 和 `run_manifest.json`

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 运行

```bash
.venv/bin/codex-workflow run examples/branching-workflow.yaml
```

也可以覆盖变量：

```bash
.venv/bin/codex-workflow run examples/branching-workflow.yaml --var task_text=请修复这个问题
```

循环示例：

```bash
.venv/bin/codex-workflow run examples/loop-workflow.yaml
```

并行示例：

```bash
.venv/bin/codex-workflow run examples/parallel-workflow.yaml
```

## YAML 示例

```yaml
name: branch-demo
workdir: /abs/path/to/project
run_root: .runs
start_at: classify

codex:
  bin: codex
  approval: never
  sandbox: danger-full-access

steps:
  classify:
    prompt: |
      判断是否需要修复。
      如果需要修复，输出 success=true, next="fix"。
      否则输出 success=true, next="finish"。
    output_file: classify.json
    branches:
      fix: fix_step
      finish: finish_step

  fix_step:
    prompt: |
      执行修复。
      输出 success=true, next="__end__"。

  finish_step:
    prompt: |
      结束流程。
      输出 success=true, next="__end__"。
```

## Step 语义

- `prompt` 或 `prompt_file`: 注入给 Codex 的提示词
- `output_file`: 该步骤最终 JSON 输出文件名
- `branches`: 将输出里的 `next` 映射为下一个 step id
- `on_success`: 不使用 `branches` 时的固定成功跳转
- `on_failure`: 失败恢复分支；未设置时默认终止
- `max_visits`: 单个 step 最多允许执行多少次，适合循环控制
- `schema`: 自定义该步骤输出 Schema；未设置时默认要求 `success:boolean` 和 `next:string`
- `parallel`: 并行子步骤列表。当前 step 自身不会调用 Codex，而是并行执行这些子步骤
- `join`: 并行子步骤全部成功后要进入的下一步

## 循环

默认允许流程回到之前的 step，但必须受边界控制：

- `max_steps`: 整个 workflow 最多执行多少步，默认 `50`
- `max_visits`: 某个 step 最多允许被访问多少次

示例：

```yaml
max_steps: 6

steps:
  review:
    prompt: |
      第 {{ current_step.attempt }} 次执行。
      未满足条件时输出 next="retry"。
      满足条件时输出 next="done"。
    max_visits: 3
    branches:
      retry: review
      done: finish
```

同一个 step 被再次执行时，会写到新的目录，例如 `review__02/`。

## 并行

并行块是显式定义的：

```yaml
steps:
  fanout:
    parallel:
      - analyst_a
      - analyst_b
    join: merge

  analyst_a:
    prompt: "..."

  analyst_b:
    prompt: "..."

  merge:
    prompt: |
      读取 {{ steps.analyst_a.output.xxx }} 和 {{ steps.analyst_b.output.xxx }}
```

说明：

- `fanout` 自身是调度节点，不直接调用 Codex
- `analyst_a` 和 `analyst_b` 会并行运行
- 只有全部成功时才进入 `join`
- 子步骤自己的 `next` 目前只用于自身输出记录，不参与并行块路由

## Run 目录

每次运行都会创建：

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

如果循环重复访问同一个 step，目录会变成：

```text
review/
review__02/
review__03/
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```
