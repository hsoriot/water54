# Codex Workflow Automation

用 YAML 定义多步 Codex 工作流，按每一步的结构化 JSON 输出决定是否继续以及走向哪个分支。

## 能力

- `codex exec` 非交互驱动
- 每一步都通过 JSON Schema 约束最终输出
- 输出 JSON 至少包含两个字段：`success` 和 `next`
- `success=false` 默认终止流程，也可在 YAML 里定义 `on_failure`
- `next` 支持分支路由，脚本解析 JSON 后决定下一步
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
- `schema`: 自定义该步骤输出 Schema；未设置时默认要求 `success:boolean` 和 `next:string`

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

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```
