# Sample Run Notes

一次典型运行后，你应该能看到：

- `.runs/<timestamp>-my-workflow/run_manifest.json`
- `.runs/<timestamp>-my-workflow/agent_a/prompt.txt`
- `.runs/<timestamp>-my-workflow/agent_a/output.json`
- `.runs/<timestamp>-my-workflow/agent_b/prompt.txt`
- `.runs/<timestamp>-my-workflow/agent_b/output.json`

如果流程中断：

1. 查看 `run_manifest.json`
2. 查看出错 step 的 `stderr.log`
3. 查看 `control.yaml`
4. 查看 `memory/` 和 `shared/handoff.md`
